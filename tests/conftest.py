"""Shared fixtures.

Settings come from ``IsolatedSettings``, which ignores ``.env`` entirely, so a
developer's local configuration can never change a test outcome.

The upstream vLLM server is replaced with an ``httpx.MockTransport`` installed
on the app's own client. That exercises the real routing, header and streaming
code paths in ``proxy.py`` while keeping the tests hermetic.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic_settings import SettingsConfigDict

from llm_assistant_api.config import Settings
from llm_assistant_api.main import create_app

UPSTREAM = "http://vllm.test:8000/v1"
AUTOCOMPLETE_UPSTREAM = "http://fim.test:8000/v1"
PRIMARY_MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
FIM_MODEL = "Qwen/Qwen2.5-Coder-1.5B"

Handler = Callable[[httpx.Request], httpx.Response]


class IsolatedSettings(Settings):
    """``Settings`` that never reads a ``.env`` from the developer's machine."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=(),
    )


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "upstream_base_url": UPSTREAM,
        "autocomplete_base_url": AUTOCOMPLETE_UPSTREAM,
        "model_id": PRIMARY_MODEL,
        "autocomplete_model_id": FIM_MODEL,
        "api_keys": "",
        "log_level": "WARNING",
        "normalize_empty_tool_arguments": True,
    }
    defaults.update(overrides)
    return IsolatedSettings(**defaults)


@dataclass
class FakeUpstream:
    """Records what the gateway sent and replies with canned responses."""

    routes: dict[tuple[str, str], Handler] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def on(self, method: str, url: str, handler: Handler) -> None:
        self.routes[(method.upper(), url)] = handler

    def json_response(
        self, method: str, url: str, payload: dict[str, Any], status_code: int = 200
    ) -> None:
        self.on(method, url, lambda _req: httpx.Response(status_code, json=payload))

    def sse_response(
        self, method: str, url: str, chunks: list[str], status_code: int = 200
    ) -> None:
        async def body() -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk.encode()

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                content=body(),
                headers={"content-type": "text/event-stream"},
            )

        self.on(method, url, handler)

    def sse_then_error(self, method: str, url: str, chunks: list[str], exc: Exception) -> None:
        """Stream a few chunks and then die, as vLLM does on a mid-run OOM."""

        async def body() -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk.encode()
            raise exc

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            )

        self.on(method, url, handler)

    def failure(self, method: str, url: str, exc: Exception) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            raise exc

        self.on(method, url, handler)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = request.content
        if raw:
            try:
                self.bodies.append(json.loads(raw))
            except json.JSONDecodeError:  # pragma: no cover - defensive
                self.bodies.append({})

        handler = self.routes.get((request.method, str(request.url)))
        if handler is None:
            return httpx.Response(
                599, json={"error": {"message": f"unrouted {request.method} {request.url}"}}
            )
        return handler(request)

    @property
    def last_body(self) -> dict[str, Any]:
        return self.bodies[-1]

    @property
    def last_request(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


GatewayFactory = Callable[[Settings], Awaitable[httpx.AsyncClient]]


@pytest.fixture
async def gateway_factory(upstream: FakeUpstream) -> AsyncIterator[GatewayFactory]:
    """Build gateways with arbitrary settings, all wired to the fake upstream."""
    apps: list[FastAPI] = []
    clients: list[httpx.AsyncClient] = []

    async def factory(settings: Settings) -> httpx.AsyncClient:
        application = create_app(settings)
        # Swap the real client for one that answers from FakeUpstream.
        await application.state.http.aclose()
        application.state.http = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handle),
            timeout=httpx.Timeout(5.0),
        )
        apps.append(application)

        gateway = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://gateway.test"
        )
        clients.append(gateway)
        return gateway

    try:
        yield factory
    finally:
        for gateway in clients:
            await gateway.aclose()
        for application in apps:
            await application.state.http.aclose()


@pytest.fixture
async def client(gateway_factory: GatewayFactory, settings: Settings) -> httpx.AsyncClient:
    return await gateway_factory(settings)
