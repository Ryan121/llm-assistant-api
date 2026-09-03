"""Embeddings and reranking - the retrieval half of an agent's context.

An agent that can only see the files you have open guesses about the rest of
the repository. These two routes are what let a client index the tree and pull
the right few hundred lines into a prompt instead.

Both are off by default and 404 with an actionable message when their upstream
is not deployed, because each costs a model and a slice of VRAM.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, Request, Response

from ..config import Settings
from ..deps import get_http_client, get_settings, require_api_key
from ..proxy import forward, resolve_embeddings_target, resolve_rerank_target

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["retrieval"], dependencies=[Depends(require_api_key)])


@router.post("/embeddings", summary="Embed text for codebase retrieval")
async def embeddings(
    request: Request,
    payload: dict[str, Any] = Body(...),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> Response:
    target = resolve_embeddings_target(settings)
    prepared = {**payload, "model": target.model_id}
    log.info(
        "forward embeddings model=%s rid=%s",
        target.model_id,
        getattr(request.state, "request_id", "-"),
    )
    return await forward(client, "embeddings", prepared, target, stream=False)


@router.post("/rerank", summary="Rerank candidate documents against a query")
async def rerank(
    request: Request,
    payload: dict[str, Any] = Body(...),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> Response:
    target = resolve_rerank_target(settings)
    prepared = {**payload, "model": target.model_id}
    log.info(
        "forward rerank model=%s rid=%s",
        target.model_id,
        getattr(request.state, "request_id", "-"),
    )
    return await forward(client, "rerank", prepared, target, stream=False)
