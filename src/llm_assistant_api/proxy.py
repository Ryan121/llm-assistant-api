"""Forwarding logic between the gateway and the vLLM upstream(s).

The gateway is intentionally thin: it authenticates, rewrites the model name,
enforces a token ceiling, and streams bytes straight through. Anything it does
not understand is passed to vLLM untouched, so new sampling parameters work
without a gateway release.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Response
from fastapi.responses import StreamingResponse

from .config import Settings
from .context import guard_context
from .errors import ModelNotFoundError, RouteDisabledError, UpstreamError
from .payload import normalize_inbound_payload

log = logging.getLogger(__name__)

#: Response headers that belong to the upstream connection, not to ours.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)


class Target:
    """Where a request should go, under which name, and on what budget."""

    __slots__ = ("base_url", "model_id", "api_key", "timeout", "priority")

    def __init__(
        self,
        base_url: str,
        model_id: str,
        api_key: str,
        *,
        timeout: float | None = None,
        priority: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key = api_key
        #: Per-route read timeout. ``None`` keeps the client-wide default,
        #: which is sized for a long agent turn.
        self.timeout = timeout
        #: vLLM scheduling priority; lower is served earlier. ``None`` omits
        #: the field, which is required unless the engine runs with
        #: ``--scheduling-policy priority``.
        self.priority = priority

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers


def resolve_target(settings: Settings, requested_model: str | None) -> Target:
    """Pick an upstream for ``requested_model``.

    Autocomplete traffic goes to the small FIM model when that service is
    enabled. Everything else goes to the primary model. An unrecognised name
    falls back to the primary model when ``MODEL_ALIAS_FALLBACK`` is on, which
    is what makes editor plugins with hard-coded model names work unchanged.

    Args:
        settings: Application settings
        requested_model: The model name requested by the client

    Returns:
        Target object specifying where to route the request and which model ID to use

    Raises:
        ModelNotFoundError: When model alias fallback is disabled and model is not recognized
    """
    if requested_model and requested_model in settings.autocomplete_aliases:
        log.debug(
            "Routing request for autocomplete model %r to autocomplete upstream", requested_model
        )
        return Target(
            settings.autocomplete_base_url,
            settings.autocomplete_model_id,
            settings.upstream_api_key,
            # A completion the user has already typed past is worthless, so it
            # gets a short leash and jumps the queue ahead of agent traffic.
            timeout=settings.autocomplete_timeout_seconds,
            priority=(
                settings.autocomplete_priority if settings.priority_routing_enabled else None
            ),
        )

    if requested_model and requested_model != settings.model_id:
        if not settings.model_alias_fallback:
            log.warning(
                "Model %r not found in available models: %s",
                requested_model,
                settings.served_models(),
            )
            raise ModelNotFoundError(requested_model, settings.served_models())
        log.debug(
            "Aliasing requested model %r to primary model %r", requested_model, settings.model_id
        )

    log.debug("Routing request to primary model %r", settings.model_id)
    return Target(
        settings.upstream_base_url,
        settings.model_id,
        settings.upstream_api_key,
        priority=settings.chat_priority if settings.priority_routing_enabled else None,
    )


def resolve_embeddings_target(settings: Settings) -> Target:
    """Upstream for ``/v1/embeddings``. Raises when the service is not deployed."""
    if not settings.embeddings_enabled:
        raise RouteDisabledError("/v1/embeddings", "EMBEDDINGS_ENABLED")
    return Target(
        settings.embeddings_base_url,
        settings.embeddings_model_id,
        settings.upstream_api_key,
        timeout=settings.embeddings_timeout_seconds,
    )


def resolve_rerank_target(settings: Settings) -> Target:
    """Upstream for ``/v1/rerank``. Raises when the service is not deployed."""
    if not settings.rerank_enabled:
        raise RouteDisabledError("/v1/rerank", "RERANK_ENABLED")
    return Target(
        settings.rerank_base_url,
        settings.rerank_model_id,
        settings.upstream_api_key,
        timeout=settings.embeddings_timeout_seconds,
    )


def prepare_payload(payload: dict[str, Any], target: Target, settings: Settings) -> dict[str, Any]:
    """Normalise the request body before it reaches vLLM."""
    prepared = dict(payload)
    prepared["model"] = target.model_id

    prepared, notes = normalize_inbound_payload(
        prepared, normalize_tool_arguments=settings.normalize_tool_arguments
    )
    for note in notes:
        log.info("Normalised inbound payload - %s", note)

    cap = settings.max_tokens_cap
    if cap > 0:
        # Apply token cap to both max_tokens and max_completion_tokens fields
        for field in ("max_tokens", "max_completion_tokens"):
            requested = prepared.get(field)
            if isinstance(requested, int) and requested > cap:
                prepared[field] = cap
                log.debug("Applied token cap of %d to %s", cap, field)
        # An unbounded request would otherwise be free to run to the context
        # limit and hold a KV-cache slot for minutes.
        if prepared.get("max_tokens") is None and prepared.get("max_completion_tokens") is None:
            prepared["max_tokens"] = cap
            log.debug("Applied default token cap of %d to max_tokens", cap)

    # Never override a priority the caller set deliberately.
    if target.priority is not None and "priority" not in prepared:
        prepared["priority"] = target.priority

    # Last, so the estimate accounts for the cap we just applied.
    estimated = guard_context(
        prepared,
        budget=settings.context_budget,
        chars_per_token=settings.chars_per_token,
    )
    log.debug("Estimated prompt size: ~%d tokens", estimated)

    return prepared


def _passthrough_headers(upstream: httpx.Response) -> dict[str, str]:
    """Filter out hop-by-hop headers that shouldn't be forwarded to the client."""
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}


def _timeout_kwarg(target: Target) -> dict[str, Any]:
    """``timeout=`` for httpx, or nothing so the client-wide default applies."""
    if target.timeout is None:
        return {}
    return {"timeout": target.timeout}


async def forward(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    target: Target,
    *,
    stream: bool,
) -> Response:
    """Send ``payload`` to ``target`` and adapt the reply for our caller."""
    log.debug("Forwarding request to %s:%s with stream=%s", target.base_url, path, stream)
    if stream:
        return await _forward_stream(client, path, payload, target)
    return await _forward_json(client, path, payload, target)


async def _forward_json(
    client: httpx.AsyncClient, path: str, payload: dict[str, Any], target: Target
) -> Response:
    try:
        log.debug("Making JSON request to %s", target.url(path))
        upstream = await client.post(
            target.url(path),
            json=payload,
            headers=target.headers(),
            **_timeout_kwarg(target),
        )
        log.debug("Received JSON response with status %d", upstream.status_code)
    except httpx.TimeoutException as exc:
        log.error("Upstream timeout when connecting to %s: %s", target.base_url, exc)
        raise UpstreamError(f"Upstream timed out: {exc}", status_code=504) from exc
    except httpx.HTTPError as exc:
        log.error("Cannot reach model server at %s: %s", target.base_url, exc)
        raise UpstreamError(f"Cannot reach model server at {target.base_url}: {exc}") from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


async def _forward_stream(
    client: httpx.AsyncClient, path: str, payload: dict[str, Any], target: Target
) -> Response:
    """Open the upstream stream eagerly so errors become normal JSON replies.

    If we handed the request straight to ``StreamingResponse`` the status code
    would already be committed by the time an upstream 4xx arrived, and the
    editor would see an empty 200.
    """
    log.debug("Opening streaming request to %s", target.url(path))
    request = client.build_request(
        "POST",
        target.url(path),
        json=payload,
        headers=target.headers(),
        **_timeout_kwarg(target),
    )
    try:
        upstream = await client.send(request, stream=True)
        log.debug("Received streaming response with status %d", upstream.status_code)
    except httpx.TimeoutException as exc:
        log.error("Upstream timeout during streaming to %s: %s", target.base_url, exc)
        raise UpstreamError(f"Upstream timed out: {exc}", status_code=504) from exc
    except httpx.HTTPError as exc:
        log.error("Cannot reach model server at %s during streaming: %s", target.base_url, exc)
        raise UpstreamError(f"Cannot reach model server at {target.base_url}: {exc}") from exc

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        log.warning("Upstream returned error status %d: %s", upstream.status_code, body[:200])
        return Response(
            content=body,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:  # mid-stream failure: no status left to set
            log.warning("stream aborted by upstream: %s", exc)
        finally:
            await upstream.aclose()

    headers = _passthrough_headers(upstream)
    headers.setdefault("cache-control", "no-cache")
    headers["x-accel-buffering"] = "no"

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
        headers=headers,
    )


async def list_upstream_models(
    client: httpx.AsyncClient, target: Target
) -> list[dict[str, Any]] | None:
    """Best-effort ``GET /models`` against an upstream. ``None`` on failure."""
    try:
        log.debug("Listing upstream models from %s", target.url("models"))
        upstream = await client.get(target.url("models"), headers=target.headers(), timeout=10.0)
        upstream.raise_for_status()
        data = upstream.json().get("data")
        log.debug("Retrieved %d upstream models", len(data) if data else 0)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.debug("Upstream model listing unavailable: %s", exc)
        return None
    return data if isinstance(data, list) else None


async def probe_upstream(client: httpx.AsyncClient, base_url: str) -> bool:
    """Return whether the vLLM health endpoint answers.

    vLLM exposes ``/health`` at the server root, one level above ``/v1``.
    """
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        log.debug("Probing upstream health at %s/health", root)
        response = await client.get(f"{root}/health", timeout=5.0)
    except httpx.HTTPError as exc:
        log.warning("Failed to probe upstream health at %s/health: %s", root, exc)
        return False
    result = response.status_code == 200
    log.debug("Upstream health probe result: %s", "success" if result else "failure")
    return result
