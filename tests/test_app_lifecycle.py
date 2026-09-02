"""Startup/shutdown behaviour and middleware wiring."""

from __future__ import annotations

import logging

import httpx
import pytest

from llm_assistant_api.main import create_app
from tests.conftest import GatewayFactory, make_settings


async def test_lifespan_closes_the_shared_http_client() -> None:
    app = create_app(make_settings())

    async with app.router.lifespan_context(app):
        assert not app.state.http.is_closed

    assert app.state.http.is_closed


async def test_startup_warns_when_the_gateway_is_unauthenticated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(make_settings(api_keys=""))

    with caplog.at_level(logging.WARNING, logger="llm_assistant_api.main"):
        async with app.router.lifespan_context(app):
            pass

    assert any("API_KEYS is empty" in record.message for record in caplog.records)


async def test_startup_is_quiet_when_keys_are_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(make_settings(api_keys="a-key"))

    with caplog.at_level(logging.WARNING, logger="llm_assistant_api.main"):
        async with app.router.lifespan_context(app):
            pass

    assert not [r for r in caplog.records if "API_KEYS is empty" in r.message]


async def test_cors_is_off_by_default(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"origin": "http://evil.test"})

    assert "access-control-allow-origin" not in response.headers


async def test_cors_allows_only_configured_origins(gateway_factory: GatewayFactory) -> None:
    client = await gateway_factory(make_settings(cors_origins="http://localhost:3000"))

    allowed = await client.get("/healthz", headers={"origin": "http://localhost:3000"})
    denied = await client.get("/healthz", headers={"origin": "http://evil.test"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in denied.headers
