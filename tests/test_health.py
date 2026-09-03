from __future__ import annotations

import httpx
import pytest

from llm_assistant_api import __version__
from tests.conftest import (
    FIM_MODEL,
    PRIMARY_MODEL,
    FakeUpstream,
    GatewayFactory,
    make_settings,
)

HEALTH_URL = "http://vllm.test:8999/health"
FIM_HEALTH_URL = "http://fim.test:8999/health"


async def test_healthz_does_not_touch_the_model_server(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert upstream.requests == []


async def test_version_reports_build_and_served_models(client: httpx.AsyncClient) -> None:
    body = (await client.get("/version")).json()

    assert body["version"] == __version__
    assert body["models"] == [PRIMARY_MODEL]
    assert body["authenticated"] is False


async def test_readyz_is_ready_when_upstream_health_answers(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("GET", HEALTH_URL, {})

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "upstreams": {PRIMARY_MODEL: True}}


async def test_readyz_is_503_while_the_model_is_still_loading(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.failure("GET", HEALTH_URL, httpx.ConnectError("connection refused"))

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "loading"


@pytest.mark.parametrize("fim_healthy", [True, False])
async def test_readyz_covers_the_autocomplete_upstream_when_enabled(
    gateway_factory: GatewayFactory, upstream: FakeUpstream, fim_healthy: bool
) -> None:
    client = await gateway_factory(make_settings(autocomplete_enabled=True))
    upstream.json_response("GET", HEALTH_URL, {})
    if fim_healthy:
        upstream.json_response("GET", FIM_HEALTH_URL, {})
    else:
        upstream.failure("GET", FIM_HEALTH_URL, httpx.ConnectError("refused"))

    response = await client.get("/readyz")

    assert response.json()["upstreams"] == {PRIMARY_MODEL: True, FIM_MODEL: fim_healthy}
    assert response.status_code == (200 if fim_healthy else 503)


async def test_root_advertises_the_openai_base_path(client: httpx.AsyncClient) -> None:
    body = (await client.get("/")).json()

    assert body["openai_base_url"] == "/v1"
    assert body["model"] == PRIMARY_MODEL


async def test_supplied_request_id_is_echoed_and_one_is_generated_otherwise(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/healthz", headers={"x-request-id": "abc123"})).headers[
        "x-request-id"
    ] == "abc123"
    assert (await client.get("/healthz")).headers["x-request-id"]
