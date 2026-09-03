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

from .. import __version__
from ..config import Settings
from ..deps import get_http_client, get_settings
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


@router.get("/metrics", summary="Get application metrics")
async def metrics() -> dict[str, Any]:
    """Return application performance metrics."""
    return {
        "summary": metrics_collector.get_summary(),
        "request_stats": metrics_collector.get_request_stats(),
    }
