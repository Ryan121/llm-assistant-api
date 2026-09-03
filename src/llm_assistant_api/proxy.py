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
from .errors import ModelNotFoundError, UpstreamError
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
    """Where a request should go, and under which name."""

    __slots__ = ("base_url", "model_id", "api_key")

    def __init__(self, base_url: str, model_id: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key = api_key

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
    """
    if requested_model and requested_model in settings.autocomplete_aliases:
        return Target(
            settings.autocomplete_base_url,
            settings.autocomplete_model_id,
            settings.upstream_api_key,
        )

    if requested_model and requested_model != settings.model_id:
        if not settings.model_alias_fallback:
            raise ModelNotFoundError(requested_model, settings.served_models())
        log.debug("aliasing requested model %r to %r", requested_model, settings.model_id)

    return Target(settings.upstream_base_url, settings.model_id, settings.upstream_api_key)


def prepare_payload(payload: dict[str, Any], target: Target, settings: Settings) -> dict[str, Any]:
    """Normalise the request body before it reaches vLLM."""
    prepared = dict(payload)
    prepared["model"] = target.model_id

    prepared, notes = normalize_inbound_payload(
        prepared, normalize_tool_arguments=settings.normalize_tool_arguments
    )
    for note in notes:
        log.info("normalised inbound payload - %s", note)

    cap = settings.max_tokens_cap
    if cap > 0:
        for field in ("max_tokens", "max_completion_tokens"):
            requested = prepared.get(field)
            if isinstance(requested, int) and requested > cap:
                prepared[field] = cap
        # An unbounded request would otherwise be free to run to the context
        # limit and hold a KV-cache slot for minutes.
        if prepared.get("max_tokens") is None and prepared.get("max_completion_tokens") is None:
            prepared["max_tokens"] = cap

    return prepared


def _passthrough_headers(upstream: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}


async def forward(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    target: Target,
    *,
    stream: bool,
) -> Response:
    """Send ``payload`` to ``target`` and adapt the reply for our caller."""
    if stream:
        return await _forward_stream(client, path, payload, target)
    return await _forward_json(client, path, payload, target)


async def _forward_json(
    client: httpx.AsyncClient, path: str, payload: dict[str, Any], target: Target
) -> Response:
    try:
        upstream = await client.post(target.url(path), json=payload, headers=target.headers())
    except httpx.TimeoutException as exc:
        raise UpstreamError(f"Upstream timed out: {exc}", status_code=504) from exc
    except httpx.HTTPError as exc:
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
    request = client.build_request("POST", target.url(path), json=payload, headers=target.headers())
    try:
        upstream = await client.send(request, stream=True)
    except httpx.TimeoutException as exc:
        raise UpstreamError(f"Upstream timed out: {exc}", status_code=504) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Cannot reach model server at {target.base_url}: {exc}") from exc

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
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
        upstream = await client.get(target.url("models"), headers=target.headers(), timeout=10.0)
        upstream.raise_for_status()
        data = upstream.json().get("data")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.debug("upstream model listing unavailable: %s", exc)
        return None
    return data if isinstance(data, list) else None


async def probe_upstream(client: httpx.AsyncClient, base_url: str) -> bool:
    """Return whether the vLLM health endpoint answers.

    vLLM exposes ``/health`` at the server root, one level above ``/v1``.
    """
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        response = await client.get(f"{root}/health", timeout=5.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200
