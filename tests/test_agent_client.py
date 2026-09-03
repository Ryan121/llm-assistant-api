"""Streaming assembly of tool calls.

vLLM sends a tool call in fragments and its parser can end a stream mid-JSON.
Executing a partial call is the failure mode these tests exist to prevent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from llm_assistant_agent.client import ChatClient, TurnError

BASE_URL = "http://gateway.test/v1"


def _sse(events: list[dict[str, Any]]) -> list[str]:
    return [f"data: {json.dumps(event)}\n\n" for event in events] + ["data: [DONE]\n\n"]


def _text_delta(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def _tool_delta(
    index: int = 0,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> dict[str, Any]:
    fragment: dict[str, Any] = {"index": index, "function": {}}
    if call_id is not None:
        fragment["id"] = call_id
    if name is not None:
        fragment["function"]["name"] = name
    if arguments is not None:
        fragment["function"]["arguments"] = arguments
    return {"choices": [{"index": 0, "delta": {"tool_calls": [fragment]}}]}


def _stream(chunks: list[str], status_code: int = 200) -> httpx.MockTransport:
    async def body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk.encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, content=body(), headers={"content-type": "text/event-stream"}
        )

    return httpx.MockTransport(handler)


async def _run(transport: httpx.MockTransport) -> tuple[Any, list[dict[str, Any]], list[str]]:
    client = ChatClient(BASE_URL, "sk-test", "test-model")
    seen: list[str] = []
    async with httpx.AsyncClient(transport=transport) as http:
        turn, raw = await client.turn(http, [], [], on_text=seen.append)
    return turn, raw, seen


async def test_text_is_streamed_and_accumulated() -> None:
    transport = _stream(_sse([_text_delta("Hello "), _text_delta("world")]))

    turn, _, seen = await _run(transport)

    assert turn.content == "Hello world"
    assert seen == ["Hello ", "world"]


async def test_a_tool_call_split_across_deltas_is_reassembled() -> None:
    transport = _stream(
        _sse(
            [
                _tool_delta(call_id="call_1", name="read_file"),
                _tool_delta(arguments='{"path": '),
                _tool_delta(arguments='"src/app.py"}'),
            ]
        )
    )

    turn, raw, _ = await _run(transport)

    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"path": "src/app.py"}
    # The raw form goes back into the transcript so the ids line up.
    assert raw[0]["function"]["arguments"] == '{"path": "src/app.py"}'


async def test_truncated_arguments_are_never_executed() -> None:
    """A stream that dies mid-JSON must not produce a half-applied edit."""
    transport = _stream(
        _sse(
            [
                _tool_delta(call_id="call_1", name="edit_file"),
                _tool_delta(arguments='{"path": "a.py", "old_string": "x'),
            ]
        )
    )

    turn, raw, _ = await _run(transport)

    assert turn.tool_calls == []
    assert raw == []
    assert any("not valid JSON" in problem for problem in turn.malformed)


async def test_a_call_with_no_name_is_rejected() -> None:
    transport = _stream(_sse([_tool_delta(arguments="{}")]))

    turn, _, _ = await _run(transport)

    assert turn.tool_calls == []
    assert any("no function name" in problem for problem in turn.malformed)


async def test_non_object_arguments_are_rejected() -> None:
    transport = _stream(_sse([_tool_delta(call_id="c", name="grep", arguments='"just a string"')]))

    turn, _, _ = await _run(transport)

    assert turn.tool_calls == []
    assert any("not a JSON object" in problem for problem in turn.malformed)


async def test_absent_arguments_default_to_an_empty_object() -> None:
    """A no-argument tool call is legitimate and must not be treated as broken."""
    transport = _stream(_sse([_tool_delta(call_id="c", name="list_files")]))

    turn, _, _ = await _run(transport)

    assert turn.tool_calls[0].arguments == {}


async def test_parallel_tool_calls_are_kept_separate() -> None:
    transport = _stream(
        _sse(
            [
                _tool_delta(index=0, call_id="a", name="read_file", arguments='{"path": "a"}'),
                _tool_delta(index=1, call_id="b", name="read_file", arguments='{"path": "b"}'),
            ]
        )
    )

    turn, _, _ = await _run(transport)

    assert [call.arguments["path"] for call in turn.tool_calls] == ["a", "b"]


async def test_malformed_sse_lines_are_skipped() -> None:
    transport = _stream(["data: not json\n\n", *_sse([_text_delta("ok")])])

    turn, _, _ = await _run(transport)

    assert turn.content == "ok"


async def test_finish_reason_is_captured() -> None:
    transport = _stream(_sse([{"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}]))

    turn, _, _ = await _run(transport)

    assert turn.finish_reason == "length"


# --- errors ----------------------------------------------------------------


async def test_context_overflow_is_explained_with_a_next_step() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "too long", "code": "context_length_exceeded"}},
        )

    with pytest.raises(TurnError, match="/compact"):
        await _run(httpx.MockTransport(handler))


async def test_a_bad_key_is_explained() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "Incorrect API key", "code": "invalid_api_key"}}
        )

    with pytest.raises(TurnError, match="API_KEYS"):
        await _run(httpx.MockTransport(handler))


async def test_an_unreachable_gateway_names_the_url() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(TurnError, match=BASE_URL):
        await _run(httpx.MockTransport(handler))
