from __future__ import annotations

import logging

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


async def test_rejection_says_how_many_keys_are_configured(
    gateway_factory: GatewayFactory,
) -> None:
    """A bare "wrong key" is undiagnosable; the count tells you which side to check."""
    client = await gateway_factory(make_settings(api_keys="one,two"))

    body = (await client.post("/v1/chat/completions", json=CHAT_REQUEST)).json()

    assert "2 key(s) configured" in body["error"]["message"]
    assert "make vscode-config" in body["error"]["message"]


async def test_non_ascii_token_is_a_401_not_a_500(gateway_factory: GatewayFactory) -> None:
    """hmac.compare_digest raises TypeError on non-ASCII; that must not be a 500.

    The header is sent as raw latin-1 bytes because HTTP headers are
    byte-oriented and httpx refuses to encode a non-ASCII str. Starlette then
    decodes it as latin-1, so the token reaches us non-ASCII.
    """
    client = await gateway_factory(make_settings(api_keys="primary-key"))

    response = await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers=[(b"authorization", "Bearer clé-privée".encode("latin-1"))],
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_rejection_logs_a_fingerprint_and_never_the_whole_token(
    gateway_factory: GatewayFactory, caplog: pytest.LogCaptureFixture
) -> None:
    client = await gateway_factory(make_settings(api_keys="the-real-key"))
    presented = "wrong-key-0123456789abcdef"

    with caplog.at_level(logging.WARNING, logger="llm_assistant_api.deps"):
        await client.post(
            "/v1/chat/completions",
            json=CHAT_REQUEST,
            headers={"authorization": f"Bearer {presented}"},
        )

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "wrong-ke" in logged, "a prefix is needed to compare the two sides"
    assert f"length {len(presented)}" in logged
    assert presented not in logged, "the full token must never reach the log"


async def test_missing_token_is_logged_distinctly_from_a_wrong_one(
    gateway_factory: GatewayFactory, caplog: pytest.LogCaptureFixture
) -> None:
    client = await gateway_factory(make_settings(api_keys="the-real-key"))

    with caplog.at_level(logging.WARNING, logger="llm_assistant_api.deps"):
        await client.post("/v1/chat/completions", json=CHAT_REQUEST)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "no bearer token" in logged


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
