from __future__ import annotations

import httpx
import pytest

from tests.conftest import FakeUpstream, GatewayFactory, make_settings

CHAT_URL = "http://vllm.test:8000/v1/chat/completions"
CHAT_REQUEST = {"model": "anything", "messages": [{"role": "user", "content": "hi"}]}


async def test_open_gateway_allows_anonymous_calls(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, {"id": "x"})

    assert (await client.post("/v1/chat/completions", json=CHAT_REQUEST)).status_code == 200


async def test_missing_token_is_rejected_once_keys_are_configured(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings(api_keys="primary-key"))

    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert upstream.requests == [], "an unauthenticated request must not reach the GPU"


async def test_wrong_token_is_rejected(gateway_factory: GatewayFactory) -> None:
    client = await gateway_factory(make_settings(api_keys="primary-key"))

    response = await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers={"authorization": "Bearer nope"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("token", ["primary-key", "rotation-key"])
async def test_any_configured_key_is_accepted(
    gateway_factory: GatewayFactory, upstream: FakeUpstream, token: str
) -> None:
    client = await gateway_factory(make_settings(api_keys="primary-key,rotation-key"))
    upstream.json_response("POST", CHAT_URL, {"id": "x"})

    response = await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers={"authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


async def test_health_endpoints_stay_unauthenticated(gateway_factory: GatewayFactory) -> None:
    client = await gateway_factory(make_settings(api_keys="primary-key"))

    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/version")).json()["authenticated"] is True


async def test_client_token_is_never_forwarded_to_the_model_server(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(
        make_settings(api_keys="client-key", upstream_api_key="upstream-key")
    )
    upstream.json_response("POST", CHAT_URL, {"id": "x"})

    await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers={"authorization": "Bearer client-key"},
    )

    assert upstream.last_request.headers["authorization"] == "Bearer upstream-key"
