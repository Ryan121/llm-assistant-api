"""Application factory.

``create_app`` takes an optional ``Settings`` so tests can build an isolated
app without touching the process environment. The shared ``httpx.AsyncClient``
is created here rather than in the lifespan handler so that an app used via
``ASGITransport`` (which does not run lifespan events) still works.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings
from .errors import (
    ContextOverflowError,
    ModelNotFoundError,
    RouteDisabledError,
    UpstreamError,
    error_body,
    error_response,
)
from .logging_config import configure_logging
from .metrics import metrics_collector
from .routes import health, openai_compat, retrieval

log = logging.getLogger(__name__)

_DESCRIPTION = """
OpenAI-compatible gateway for a locally hosted coding assistant.

Point any OpenAI-compatible VS Code extension (Continue, Cline, Roo Code,
Copilot BYOK) at `/v1` and authenticate with a bearer token from `API_KEYS`.
"""


def _build_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.request_timeout_seconds,
            connect=settings.connect_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=settings.max_connections,
            max_keepalive_connections=settings.max_connections,
        ),
        follow_redirects=False,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    configure_logging(resolved.log_level)
    client = _build_client(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "%s v%s ready: model=%s upstream=%s",
            resolved.app_name,
            __version__,
            resolved.model_id,
            resolved.upstream_base_url,
        )
        if not resolved.api_key_set:
            log.warning(
                "API_KEYS is empty - every caller that can reach this port may use the model"
            )
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="LLM Assistant API",
        description=_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.http = client

    if resolved.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()

        # Track request metrics
        endpoint = request.url.path
        model = request.query_params.get("model") or "unknown"
        streamed = request.query_params.get("stream", "false").lower() == "true"

        metrics = metrics_collector.start_request(request_id, endpoint, model, streamed)

        def complete(status_code: int) -> None:
            metrics.finish(status_code)
            metrics_collector.finish_request(request_id, status_code)
            elapsed_ms = (time.perf_counter() - started) * 1000
            ttft = f" ttft={metrics.ttft_ms:.0f}ms" if metrics.ttft_ms is not None else ""
            log.info(
                "%s %s -> %d in %.0fms%s rid=%s",
                request.method,
                request.url.path,
                status_code,
                elapsed_ms,
                ttft,
                request_id,
            )

        try:
            response = await call_next(request)
        except Exception:
            # Ensure cleanup even on exceptions
            complete(500)
            raise

        response.headers["x-request-id"] = request_id

        # Stopping the clock here would time the response *headers*. For a
        # streamed completion those arrive before the model has produced a
        # single token, so the interesting numbers - time to first token, and
        # how long the answer actually took - both need the body.
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:  # pragma: no cover - defensive
            complete(response.status_code)
            return response

        async def timed() -> AsyncIterator[bytes]:
            try:
                async for chunk in body_iterator:
                    metrics.first_byte()
                    yield chunk
            finally:
                # A client that disconnects mid-stream still gets recorded,
                # which is how abandoned autocompletes become visible.
                complete(response.status_code)

        response.body_iterator = timed()  # type: ignore[attr-defined]
        return response

    # Registered on Starlette's base class so router-level 404s get the same
    # envelope as our own aborts; fastapi.HTTPException is a subclass of it.
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Keep the OpenAI error envelope instead of FastAPI's {"detail": ...}.
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            body = exc.detail
        else:
            body = error_body(str(exc.detail), "invalid_request_error")
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.exception_handler(UpstreamError)
    async def upstream_error_handler(_: Request, exc: UpstreamError) -> JSONResponse:
        log.warning("upstream error: %s", exc.message)
        return error_response(exc.status_code, exc.message, "api_error", "upstream_unavailable")

    @app.exception_handler(ModelNotFoundError)
    async def model_not_found_handler(_: Request, exc: ModelNotFoundError) -> JSONResponse:
        return error_response(
            404,
            f"Model {exc.model!r} is not served here. Available: {', '.join(exc.available)}.",
            "invalid_request_error",
            "model_not_found",
        )

    @app.exception_handler(ContextOverflowError)
    async def context_overflow_handler(_: Request, exc: ContextOverflowError) -> JSONResponse:
        # 400 with OpenAI's own code, because that is the one agent clients
        # recognise and respond to by compacting their transcript.
        return error_response(
            400,
            f"This request needs roughly {exc.estimated_tokens} tokens but the "
            f"context budget is {exc.budget}. Shorten the conversation, drop "
            f"attached files, or raise CONTEXT_GUARD_TOKENS and the engine's "
            f"MAX_MODEL_LEN together.",
            "invalid_request_error",
            "context_length_exceeded",
        )

    @app.exception_handler(RouteDisabledError)
    async def route_disabled_handler(_: Request, exc: RouteDisabledError) -> JSONResponse:
        return error_response(
            404,
            f"{exc.route} is not enabled on this deployment. "
            f"Set {exc.setting}=true in .env and start the matching service.",
            "invalid_request_error",
            "route_disabled",
        )

    app.include_router(health.router)
    app.include_router(openai_compat.router)
    app.include_router(retrieval.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": resolved.app_name,
            "version": __version__,
            "model": resolved.model_id,
            "openai_base_url": "/v1",
            "docs": "/docs",
        }

    return app


app = create_app()
