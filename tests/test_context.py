"""The pre-flight context guard.

The estimator is a heuristic, so these tests assert on behaviour at the
boundaries - does an obviously-too-large request get rejected, does a normal
one pass, is the reply shaped so a client can act on it - rather than on exact
token counts, which would just be a change detector.
"""

from __future__ import annotations

import httpx
import pytest

from llm_assistant_api.context import estimate_payload_tokens, guard_context
from llm_assistant_api.errors import ContextOverflowError

from .conftest import UPSTREAM, FakeUpstream, GatewayFactory, make_settings

CHARS_PER_TOKEN = 3.5
CHAT_URL = f"{UPSTREAM}/chat/completions"


def test_estimate_counts_message_content() -> None:
    payload = {"messages": [{"role": "user", "content": "x" * 3500}]}

    estimate = estimate_payload_tokens(payload, chars_per_token=CHARS_PER_TOKEN)

    assert 1000 <= estimate <= 1010


def test_estimate_counts_tool_schemas() -> None:
    """Tool definitions are resent every turn and can outweigh the chat."""
    bare = {"messages": [{"role": "user", "content": "hi"}]}
    with_tools = {
        **bare,
        "tools": [{"type": "function", "function": {"name": "edit_file", "parameters": {}}}],
    }

    assert estimate_payload_tokens(
        with_tools, chars_per_token=CHARS_PER_TOKEN
    ) > estimate_payload_tokens(bare, chars_per_token=CHARS_PER_TOKEN)


def test_estimate_counts_tool_calls_in_history() -> None:
    """An agent transcript is mostly tool calls, not prose."""
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "' + "x" * 700 + '"}',
                        },
                    }
                ],
            }
        ]
    }

    assert estimate_payload_tokens(payload, chars_per_token=CHARS_PER_TOKEN) > 190


def test_estimate_handles_structured_content_parts() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "y" * 700}]},
        ]
    }

    assert estimate_payload_tokens(payload, chars_per_token=CHARS_PER_TOKEN) >= 200


def test_estimate_handles_bare_prompt_for_completions() -> None:
    payload = {"prompt": "z" * 700}

    assert estimate_payload_tokens(payload, chars_per_token=CHARS_PER_TOKEN) >= 200


def test_guard_is_disabled_when_no_budget_is_configured() -> None:
    payload = {"messages": [{"role": "user", "content": "x" * 100_000}]}

    # Returns the estimate rather than raising: 0 means "operator has not told
    # us the engine's window".
    assert guard_context(payload, budget=0, chars_per_token=CHARS_PER_TOKEN) > 0


def test_guard_allows_a_request_that_fits() -> None:
    payload = {"messages": [{"role": "user", "content": "x" * 350}]}

    assert guard_context(payload, budget=1000, chars_per_token=CHARS_PER_TOKEN) <= 1000


def test_guard_rejects_an_over_long_conversation() -> None:
    payload = {"messages": [{"role": "user", "content": "x" * 100_000}]}

    with pytest.raises(ContextOverflowError) as excinfo:
        guard_context(payload, budget=1000, chars_per_token=CHARS_PER_TOKEN)

    assert excinfo.value.budget == 1000
    assert excinfo.value.estimated_tokens > 1000


def test_guard_counts_requested_output_against_the_same_window() -> None:
    """A prompt that only fits because it asked for no output does not fit."""
    payload = {"messages": [{"role": "user", "content": "x" * 350}], "max_tokens": 5000}

    with pytest.raises(ContextOverflowError):
        guard_context(payload, budget=1000, chars_per_token=CHARS_PER_TOKEN)


# --- through the gateway ---------------------------------------------------


async def test_overflow_returns_a_recoverable_400(gateway_factory: GatewayFactory) -> None:
    """Agent clients compact their transcript on context_length_exceeded."""
    client = await gateway_factory(make_settings(context_guard_tokens=1000))

    response = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x" * 100_000}]},
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "context_length_exceeded"
    assert "CONTEXT_GUARD_TOKENS" in error["message"]


async def test_request_within_budget_still_reaches_the_upstream(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings(context_guard_tokens=100_000))
    upstream.json_response("POST", CHAT_URL, {"id": "chat-1", "choices": []})

    response = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == httpx.codes.OK
