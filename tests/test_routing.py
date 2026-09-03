"""Per-route timeouts and scheduling priority.

Both exist for the same reason: on one box, an interactive keystroke and a
fifteen-minute agent run share a queue, and treating them identically makes
the fast thing feel slow.
"""

from __future__ import annotations

from llm_assistant_api.proxy import resolve_target

from .conftest import (
    FIM_MODEL,
    PRIMARY_MODEL,
    UPSTREAM,
    FakeUpstream,
    GatewayFactory,
    make_settings,
)

CHAT_URL = f"{UPSTREAM}/chat/completions"


def test_chat_keeps_the_client_wide_timeout() -> None:
    """A long agent turn must not inherit the autocomplete leash."""
    target = resolve_target(make_settings(), PRIMARY_MODEL)

    assert target.timeout is None


def test_autocomplete_gets_a_short_timeout() -> None:
    settings = make_settings(autocomplete_enabled=True, autocomplete_timeout_seconds=2.0)

    target = resolve_target(settings, FIM_MODEL)

    assert target.timeout == 2.0


def test_no_priority_is_sent_unless_the_engine_is_configured_for_it() -> None:
    """vLLM rejects a non-zero priority unless started with priority scheduling."""
    settings = make_settings(autocomplete_enabled=True)

    assert resolve_target(settings, PRIMARY_MODEL).priority is None
    assert resolve_target(settings, FIM_MODEL).priority is None


def test_autocomplete_outranks_chat_when_priority_routing_is_on() -> None:
    settings = make_settings(autocomplete_enabled=True, priority_routing_enabled=True)

    chat = resolve_target(settings, PRIMARY_MODEL)
    autocomplete = resolve_target(settings, FIM_MODEL)

    assert autocomplete.priority is not None
    assert chat.priority is not None
    # Lower is served earlier in vLLM.
    assert autocomplete.priority < chat.priority


async def test_priority_is_sent_upstream(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings(priority_routing_enabled=True, chat_priority=3))
    upstream.json_response("POST", CHAT_URL, {"id": "c", "choices": []})

    await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})

    assert upstream.last_body["priority"] == 3


async def test_caller_supplied_priority_is_not_overwritten(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings(priority_routing_enabled=True, chat_priority=3))
    upstream.json_response("POST", CHAT_URL, {"id": "c", "choices": []})

    await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x"}], "priority": -5},
    )

    assert upstream.last_body["priority"] == -5
