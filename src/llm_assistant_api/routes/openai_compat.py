"""The OpenAI-compatible surface that VS Code extensions talk to.

Continue, Cline, Roo Code, Kilo Code and Copilot's BYOK provider all speak this
dialect, so implementing it is what makes the deployment editor-agnostic.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, Request, Response, HTTPException
from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..deps import get_http_client, get_settings, require_api_key
from ..proxy import forward, list_upstream_models, prepare_payload, resolve_target

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"], dependencies=[Depends(require_api_key)])


class ModelsResponse(BaseModel):
    object: str
    data: list[dict[str, Any]]


@router.get("/models", summary="List the models this gateway serves")
async def models(
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    if settings.expose_upstream_models:
        target = resolve_target(settings, None)
        upstream = await list_upstream_models(client, target)
        if upstream:
            return {"object": "list", "data": upstream}

    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": settings.app_name,
            }
            for model in settings.served_models()
        ],
    }


async def _handle(
    request: Request,
    path: str,
    payload: dict[str, Any],
    settings: Settings,
    client: httpx.AsyncClient,
) -> Response:
    try:
        # Validate payload structure before processing
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid payload format")
        
        # Validate required fields
        if "model" in payload and not isinstance(payload["model"], str):
            raise HTTPException(status_code=400, detail="Invalid model field")
            
        requested_model = payload.get("model")
        target = resolve_target(settings, requested_model if isinstance(requested_model, str) else None)
        prepared = prepare_payload(payload, target, settings)
        streaming = bool(prepared.get("stream"))

        log.info(
            "forward %s model=%s->%s stream=%s rid=%s",
            path,
            requested_model,
            target.model_id,
            streaming,
            getattr(request.state, "request_id", "-"),
        )
        return await forward(client, path, prepared, target, stream=streaming)
    except ValidationError as e:
        log.error("Payload validation error: %s", str(e))
        raise HTTPException(status_code=400, detail=f"Invalid payload: {str(e)}")
    except Exception as e:
        log.error("Error handling request to %s: %s", path, str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/chat/completions", summary="Chat completions (streaming and blocking)")
async def chat_completions(
    request: Request,
    payload: dict[str, Any] = Body(...),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> Response:
    return await _handle(request, "chat/completions", payload, settings, client)


@router.post("/completions", summary="Text completions (used for inline autocomplete)")
async def completions(
    request: Request,
    payload: dict[str, Any] = Body(...),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> Response:
    return await _handle(request, "completions", payload, settings, client)
