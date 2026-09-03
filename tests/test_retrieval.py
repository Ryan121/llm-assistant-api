"""``/v1/embeddings`` and ``/v1/rerank``.

Both are opt-in. The interesting cases are the disabled ones: a client that
asks for retrieval on a deployment without it should be told which setting to
turn on, not handed a bare 404.
"""

from __future__ import annotations

import httpx

from .conftest import FakeUpstream, GatewayFactory, make_settings

EMBEDDINGS_UPSTREAM = "http://emb.test:8999/v1"
RERANK_UPSTREAM = "http://rerank.test:8999/v1"
EMBEDDINGS_MODEL = "Qwen/Qwen3-Embedding-0.6B"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


async def test_embeddings_are_forwarded_to_the_embedding_upstream(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(
        make_settings(
            embeddings_enabled=True,
            embeddings_base_url=EMBEDDINGS_UPSTREAM,
            embeddings_model_id=EMBEDDINGS_MODEL,
        )
    )
    upstream.json_response(
        "POST",
        f"{EMBEDDINGS_UPSTREAM}/embeddings",
        {"object": "list", "data": [{"embedding": [0.1, 0.2]}]},
    )

    response = await client.post("/v1/embeddings", json={"input": ["def foo(): pass"]})

    assert response.status_code == httpx.codes.OK
    assert response.json()["data"][0]["embedding"] == [0.1, 0.2]
    # The gateway names the model, so clients need not know what is deployed.
    assert upstream.last_body["model"] == EMBEDDINGS_MODEL


async def test_embeddings_404_with_the_setting_to_change_when_disabled(
    gateway_factory: GatewayFactory,
) -> None:
    client = await gateway_factory(make_settings())

    response = await client.post("/v1/embeddings", json={"input": ["x"]})

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "route_disabled"
    assert "EMBEDDINGS_ENABLED" in error["message"]


async def test_rerank_is_forwarded_to_the_rerank_upstream(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(
        make_settings(
            rerank_enabled=True,
            rerank_base_url=RERANK_UPSTREAM,
            rerank_model_id=RERANK_MODEL,
        )
    )
    upstream.json_response(
        "POST", f"{RERANK_UPSTREAM}/rerank", {"results": [{"index": 0, "relevance_score": 0.9}]}
    )

    response = await client.post(
        "/v1/rerank", json={"query": "how does auth work", "documents": ["a", "b"]}
    )

    assert response.status_code == httpx.codes.OK
    assert upstream.last_body["model"] == RERANK_MODEL


async def test_rerank_404_when_disabled(gateway_factory: GatewayFactory) -> None:
    client = await gateway_factory(make_settings())

    response = await client.post("/v1/rerank", json={"query": "q", "documents": []})

    assert response.status_code == 404
    assert "RERANK_ENABLED" in response.json()["error"]["message"]


async def test_enabled_retrieval_models_are_listed(
    gateway_factory: GatewayFactory,
) -> None:
    """Clients discover retrieval by listing models, as they do for chat."""
    client = await gateway_factory(
        make_settings(
            embeddings_enabled=True,
            embeddings_model_id=EMBEDDINGS_MODEL,
            rerank_enabled=True,
            rerank_model_id=RERANK_MODEL,
        )
    )

    response = await client.get("/v1/models")

    served = {entry["id"] for entry in response.json()["data"]}
    assert EMBEDDINGS_MODEL in served
    assert RERANK_MODEL in served
