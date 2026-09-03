"""End-to-end behaviour of the OpenAI-compatible surface."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from tests.conftest import (
    FIM_MODEL,
    PRIMARY_MODEL,
    FakeUpstream,
    GatewayFactory,
    make_settings,
)

CHAT_URL = "http://vllm.test:8000/v1/chat/completions"
COMPLETIONS_URL = "http://vllm.test:8000/v1/completions"
MODELS_URL = "http://vllm.test:8000/v1/models"
FIM_COMPLETIONS_URL = "http://fim.test:8000/v1/completions"

CHAT_REQUEST = {"model": PRIMARY_MODEL, "messages": [{"role": "user", "content": "hi"}]}
CHAT_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}}],
}


# --- /v1/models -----------------------------------------------------------


async def test_models_lists_the_configured_model_without_calling_upstream(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    body = (await client.get("/v1/models")).json()

    assert body["object"] == "list"
    assert [entry["id"] for entry in body["data"]] == [PRIMARY_MODEL]
    assert upstream.requests == []


async def test_models_can_be_proxied_from_the_model_server(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings(expose_upstream_models=True))
    upstream.json_response("GET", MODELS_URL, {"data": [{"id": "served-by-vllm"}]})

    body = (await client.get("/v1/models")).json()

    assert [entry["id"] for entry in body["data"]] == ["served-by-vllm"]


async def test_models_falls_back_to_local_list_when_upstream_is_down(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings(expose_upstream_models=True))
    upstream.failure("GET", MODELS_URL, httpx.ConnectError("refused"))

    body = (await client.get("/v1/models")).json()

    assert [entry["id"] for entry in body["data"]] == [PRIMARY_MODEL]


# --- blocking chat completions -------------------------------------------


async def test_chat_completion_is_passed_through(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST)

    assert response.status_code == 200
    assert response.json() == CHAT_REPLY
    assert upstream.last_body["messages"] == CHAT_REQUEST["messages"]


async def test_editor_supplied_model_name_is_rewritten(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    await client.post("/v1/chat/completions", json={**CHAT_REQUEST, "model": "gpt-4o"})

    assert upstream.last_body["model"] == PRIMARY_MODEL


async def test_tool_calling_fields_reach_the_model_server_untouched(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)
    tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object"}},
        }
    ]

    await client.post(
        "/v1/chat/completions",
        json={**CHAT_REQUEST, "tools": tools, "tool_choice": "auto", "parallel_tool_calls": True},
    )

    assert upstream.last_body["tools"] == tools
    assert upstream.last_body["tool_choice"] == "auto"
    assert upstream.last_body["parallel_tool_calls"] is True


async def test_replayed_tool_calls_reach_vllm_as_valid_json(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    """vLLM json.loads() the arguments of inbound tool calls and 400s otherwise."""
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": PRIMARY_MODEL,
            "messages": [
                {"role": "user", "content": "read it"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            # A Python dict repr, as some editor clients replay.
                            "function": {"name": "read_file", "arguments": "{'path': 'a.py'}"},
                        }
                    ],
                },
            ],
        },
    )

    sent = upstream.last_body["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(sent) == {"path": "a.py"}


async def test_malformed_content_and_single_tool_call_object_are_repaired(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": PRIMARY_MODEL,
            "messages": [
                {"role": "user", "content": {"text": "read it"}},
                {
                    "role": "assistant",
                    "tool_calls": {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": {"path": "a.py"}},
                    },
                },
            ],
        },
    )

    sent_messages = upstream.last_body["messages"]
    assert sent_messages[0]["content"] == "read it"
    assert isinstance(sent_messages[1]["tool_calls"], list)
    assert json.loads(sent_messages[1]["tool_calls"][0]["function"]["arguments"]) == {
        "path": "a.py"
    }


async def test_invalid_tool_call_arguments_are_left_unchanged(
    client: httpx.AsyncClient, upstream: FakeUpstream, caplog: pytest.LogCaptureFixture
) -> None:
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    payload = {
        "model": PRIMARY_MODEL,
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "a.py", "body": "unterminated',
                        },
                    }
                ],
            }
        ],
    }

    with caplog.at_level(logging.WARNING, logger="llm_assistant_api.toolcalls"):
        await client.post("/v1/chat/completions", json=payload)

    sent = upstream.last_body["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert sent == '{"path": "a.py", "body": "unterminated'
    assert any("could not be repaired" in record.message for record in caplog.records)


async def test_python_literal_tool_call_arguments_are_still_repaired(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": PRIMARY_MODEL,
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": "{'path': 'a.py', 'count': 1}",
                            },
                        }
                    ],
                }
            ],
        },
    )

    sent = upstream.last_body["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(sent) == {"path": "a.py", "count": 1}


async def test_empty_tool_call_arguments_are_repaired_by_default(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": PRIMARY_MODEL,
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "   "},
                        }
                    ],
                }
            ],
        },
    )

    sent = upstream.last_body["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert sent == "{}"


async def test_empty_tool_call_arguments_can_be_left_unchanged(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(
        make_settings(normalize_tool_arguments=False, normalize_empty_tool_arguments=False)
    )
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": PRIMARY_MODEL,
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "   "},
                        }
                    ],
                }
            ],
        },
    )

    sent = upstream.last_body["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert sent == "   "


async def test_normalisation_can_be_turned_off(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings(normalize_tool_arguments=False))
    upstream.json_response("POST", CHAT_URL, CHAT_REPLY)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": PRIMARY_MODEL,
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": "{'path': 'a.py'}"}}
                    ],
                }
            ],
        },
    )

    sent = upstream.last_body["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert sent == "{'path': 'a.py'}"


async def test_upstream_4xx_body_and_status_are_preserved(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response(
        "POST", CHAT_URL, {"error": {"message": "context length exceeded"}}, status_code=400
    )

    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST)

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "context length exceeded"


async def test_unreachable_model_server_becomes_502(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.failure("POST", CHAT_URL, httpx.ConnectError("connection refused"))

    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


async def test_model_server_timeout_becomes_504(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.failure("POST", CHAT_URL, httpx.ReadTimeout("too slow"))

    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST)

    assert response.status_code == 504


async def test_strict_mode_returns_404_for_an_unknown_model(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings(model_alias_fallback=False))

    response = await client.post("/v1/chat/completions", json={**CHAT_REQUEST, "model": "gpt-4o"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert upstream.requests == []


async def test_non_object_body_is_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/chat/completions", json=["nope"])).status_code == 422


# --- streaming ------------------------------------------------------------


async def test_streaming_chat_completion_relays_sse_chunks(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    chunks = [
        'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
        "data: [DONE]\n\n",
    ]
    upstream.sse_response("POST", CHAT_URL, chunks)

    received: list[str] = []
    async with client.stream(
        "POST", "/v1/chat/completions", json={**CHAT_REQUEST, "stream": True}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        async for chunk in response.aiter_text():
            received.append(chunk)

    assert "".join(received) == "".join(chunks)
    assert upstream.last_body["stream"] is True


async def test_streaming_error_arrives_as_json_not_an_empty_200(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, {"error": {"message": "bad request"}}, status_code=400)

    response = await client.post("/v1/chat/completions", json={**CHAT_REQUEST, "stream": True})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "bad request"


async def test_streaming_upstream_failure_becomes_502(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.failure("POST", CHAT_URL, httpx.ConnectError("refused"))

    response = await client.post("/v1/chat/completions", json={**CHAT_REQUEST, "stream": True})

    assert response.status_code == 502


async def test_streaming_upstream_timeout_becomes_504(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.failure("POST", CHAT_URL, httpx.ConnectTimeout("no answer"))

    response = await client.post("/v1/chat/completions", json={**CHAT_REQUEST, "stream": True})

    assert response.status_code == 504


async def test_mid_stream_upstream_death_ends_the_stream_without_a_traceback(
    client: httpx.AsyncClient, upstream: FakeUpstream, caplog: pytest.LogCaptureFixture
) -> None:
    first = 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
    upstream.sse_then_error("POST", CHAT_URL, [first], httpx.ReadError("upstream died"))

    received = ""
    with caplog.at_level(logging.WARNING, logger="llm_assistant_api.proxy"):
        async with client.stream(
            "POST", "/v1/chat/completions", json={**CHAT_REQUEST, "stream": True}
        ) as response:
            assert response.status_code == 200
            async for chunk in response.aiter_text():
                received += chunk

    assert received == first
    assert any("stream aborted by upstream" in record.message for record in caplog.records)


async def test_unknown_path_uses_the_openai_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/embeddings")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "invalid_request_error"


# --- /v1/completions and autocomplete routing -----------------------------


async def test_completions_endpoint_uses_the_primary_model_by_default(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", COMPLETIONS_URL, {"id": "cmpl-1", "choices": []})

    response = await client.post(
        "/v1/completions", json={"model": PRIMARY_MODEL, "prompt": "def add("}
    )

    assert response.status_code == 200
    assert str(upstream.last_request.url) == COMPLETIONS_URL


@pytest.mark.parametrize("alias", [FIM_MODEL, "autocomplete"])
async def test_completions_route_to_the_fim_service_when_enabled(
    gateway_factory: GatewayFactory, upstream: FakeUpstream, alias: str
) -> None:
    client = await gateway_factory(make_settings(autocomplete_enabled=True))
    upstream.json_response("POST", FIM_COMPLETIONS_URL, {"id": "cmpl-1", "choices": []})

    response = await client.post("/v1/completions", json={"model": alias, "prompt": "def add("})

    assert response.status_code == 200
    assert str(upstream.last_request.url) == FIM_COMPLETIONS_URL
    assert upstream.last_body["model"] == FIM_MODEL


async def test_both_models_are_advertised_when_autocomplete_is_enabled(
    gateway_factory: GatewayFactory,
) -> None:
    client = await gateway_factory(make_settings(autocomplete_enabled=True))

    body = (await client.get("/v1/models")).json()

    assert [entry["id"] for entry in body["data"]] == [PRIMARY_MODEL, FIM_MODEL]
