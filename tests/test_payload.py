"""Tests for lenient inbound payload repair."""

from __future__ import annotations

import json

from llm_assistant_api.payload import normalize_inbound_payload


def test_single_tool_calls_object_and_wrapped_content_are_repaired() -> None:
    payload = {
        "model": "m",
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
    }

    normalized, notes = normalize_inbound_payload(payload)

    assert normalized["messages"][0]["content"] == "read it"
    assert isinstance(normalized["messages"][1]["tool_calls"], list)
    assert json.loads(normalized["messages"][1]["tool_calls"][0]["function"]["arguments"]) == {
        "path": "a.py"
    }
    assert any("content object unwrapped" in note for note in notes)
    assert any("single tool_calls object wrapped" in note for note in notes)
    assert any("object encoded to a JSON string" in note for note in notes)


def test_structure_repairs_still_run_when_tool_argument_normalisation_is_disabled() -> None:
    payload = {
        "model": "m",
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
    }

    normalized, notes = normalize_inbound_payload(payload, normalize_tool_arguments=False)

    assert normalized["messages"][0]["content"] == "read it"
    assert isinstance(normalized["messages"][1]["tool_calls"], list)
    assert normalized["messages"][1]["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}
    assert any("content object unwrapped" in note for note in notes)
    assert any("single tool_calls object wrapped" in note for note in notes)
    assert not any("object encoded to a JSON string" in note for note in notes)
