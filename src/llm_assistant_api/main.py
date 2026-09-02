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
from .errors import ModelNotFoundError, UpstreamError, error_body, error_response
from .logging_config import configure_logging
from .routes import health, openai_compat

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
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id
        log.info(
            "%s %s -> %d in %.0fms rid=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
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

    app.include_router(health.router)
    app.include_router(openai_compat.router)

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
