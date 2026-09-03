"""Liveness, readiness and build-identity endpoints.

``/healthz`` answers as long as the process is up; ``/readyz`` only answers
once the model server behind it can serve traffic. Docker Compose gates the
API container on the former and the deployment scripts wait on the latter,
because a 60 GB model download can keep vLLM busy for many minutes.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import PlainTextResponse

from .. import __version__
from ..config import Settings
from ..deps import get_http_client, get_settings
from ..errors import UpstreamError
from ..metrics import metrics_collector
from ..proxy import probe_upstream

router = APIRouter(tags=["operations"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (checks the model server)")
async def readyz(
    response: Response,
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    upstreams: dict[str, bool] = {
        settings.model_id: await probe_upstream(client, settings.upstream_base_url)
    }
    if settings.autocomplete_enabled:
        upstreams[settings.autocomplete_model_id] = await probe_upstream(
            client, settings.autocomplete_base_url
        )

    ready = all(upstreams.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if ready else "loading", "upstreams": upstreams}


@router.get("/version", summary="Gateway build and served model")
async def version(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return {
        "name": settings.app_name,
        "version": __version__,
        "models": settings.served_models(),
        "authenticated": bool(settings.api_key_set),
    }


@router.get("/metrics", summary="Gateway metrics (JSON, or Prometheus with ?format=prometheus)")
async def metrics(response: Response, format: str = "json") -> Any:
    """Return gateway performance metrics.

    JSON by default because the common case is a human running ``curl``;
    ``?format=prometheus`` for a scraper.
    """
    if format == "prometheus":
        return PlainTextResponse(
            metrics_collector.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return {
        "summary": metrics_collector.get_summary(),
        "request_stats": metrics_collector.get_request_stats(),
    }


@router.get("/metrics/upstream", summary="Proxy the model server's own Prometheus metrics")
async def upstream_metrics(
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> Response:
    """Relay vLLM's ``/metrics``.

    The two numbers that decide almost every tuning question live here and
    nowhere else: KV-cache utilisation (are you out of room, or out of
    compute?) and preemption count (is the scheduler thrashing?). vLLM serves
    them at the server root, one level above ``/v1``.
    """
    root = settings.upstream_base_url.rstrip("/").removesuffix("/v1")
    try:
        upstream = await client.get(f"{root}/metrics", timeout=10.0)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Cannot reach model server metrics at {root}: {exc}") from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/plain"),
    )
