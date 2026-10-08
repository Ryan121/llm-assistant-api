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
from ..proxy import get_circuit_breaker, probe_upstream

router = APIRouter(tags=["operations"])


@router.post("/admin/circuit-breaker/reset", summary="Manually reset the circuit breaker(s)")
async def reset_circuit_breaker_endpoint(
    base_url: str | None = None,
) -> dict[str, Any]:
    """Reset circuit breaker(s) to closed state.

    Use this when you've confirmed upstream is healthy and want to
    restore traffic without restarting the gateway.

    Args:
        base_url: Optional. If provided, reset only this upstream.
                  Otherwise reset all circuit breakers.
    """
    from ..proxy import reset_circuit_breaker

    reset_circuit_breaker(base_url)
    if base_url:
        return {
            "status": "circuit_breaker_reset",
            "base_url": base_url,
            "state": "closed",
        }
    return {"status": "all_circuit_breakers_reset", "state": "closed"}


@router.get("/admin/circuit-breakers", summary="Get status of all circuit breakers")
async def get_circuit_breakers() -> dict[str, dict[str, Any]]:
    """Get the status of all circuit breakers.

    Returns a dict mapping upstream base URLs to their circuit breaker status.
    """
    from ..proxy import get_all_circuit_breakers

    circuits = get_all_circuit_breakers()
    return {
        base_url: {
            "state": circuit.state,
            "failures_in_window": circuit.failure_count,
            "failure_window": circuit._failure_window,
            "failure_threshold": circuit._failure_threshold,
            "open_timeout": circuit._open_timeout,
        }
        for base_url, circuit in circuits.items()
    }


def _get_rate_limiter(settings: Settings = Depends(get_settings)) -> Any:
    """Get rate limiter from app state."""
    # Import here to avoid circular dependency
    from ..rate_limiting import get_rate_limiter

    return get_rate_limiter()


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


@router.get("/readyz/detailed", summary="Detailed readiness with rate limiter and circuit status")
async def readyz_detailed(
    response: Response,
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
    rate_limiter: Any = Depends(_get_rate_limiter),
) -> dict[str, Any]:
    """Extended readiness check including rate limiter and circuit breaker status.

    Returns 503 if:
    - Any upstream is unavailable
    - Rate limiter is in a degraded state (Redis connection lost, etc.)
    - Circuit breaker is open for primary upstream
    """
    from ..main import _shutting_down

    upstreams: dict[str, bool] = {
        settings.model_id: await probe_upstream(client, settings.upstream_base_url)
    }
    if settings.autocomplete_enabled:
        upstreams[settings.autocomplete_model_id] = await probe_upstream(
            client, settings.autocomplete_base_url
        )

    # Check circuit breaker status for each upstream (per-upstream circuit breakers)
    circuit_breakers: dict[str, dict[str, Any]] = {}
    all_circuits_healthy = True

    for model_id, base_url in [
        (settings.model_id, settings.upstream_base_url),
    ]:
        if settings.autocomplete_enabled and model_id == settings.model_id:
            continue
        if settings.autocomplete_enabled:
            circuit_breakers[settings.autocomplete_model_id] = _get_circuit_status(
                settings.autocomplete_base_url
            )
            if circuit_breakers[settings.autocomplete_model_id]["state"] == "open":
                all_circuits_healthy = False

        circuit_breakers[model_id] = _get_circuit_status(base_url)
        if circuit_breakers[model_id]["state"] == "open":
            all_circuits_healthy = False

    # Check rate limiter health
    rate_limiter_healthy = True
    rate_limiter_status = "ok"
    if hasattr(rate_limiter, "_redis_client") and rate_limiter._redis_client is not None:
        # Redis-backed limiter: check connection
        try:
            import redis.asyncio as redis

            await rate_limiter._redis_client.ping()
        except (redis.RedisError, Exception):
            rate_limiter_healthy = False
            rate_limiter_status = "redis_connection_lost"

    ready = all(upstreams.values()) and rate_limiter_healthy and all_circuits_healthy
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "degraded",
        "upstreams": upstreams,
        "rate_limiter": rate_limiter_status,
        "circuit_breakers": circuit_breakers,
        "shutting_down": _shutting_down,
    }


def _get_circuit_status(base_url: str) -> dict[str, Any]:
    """Get circuit breaker status for a specific upstream."""
    circuit = get_circuit_breaker(base_url)
    return {
        "state": circuit.state,
        "failures_in_window": circuit.failure_count,
    }
