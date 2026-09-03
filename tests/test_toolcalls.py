"""Inbound tool-call argument normalisation.

Each malformed shape here was observed causing a real vLLM 400:
    json.decoder.JSONDecodeError: Expecting property name enclosed in
    double quotes: line 1 column 2 (char 1)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from llm_assistant_api.toolcalls import normalize_tool_arguments


def raw_request(arguments: Any) -> dict[str, Any]:
    """A request whose tool call carries ``arguments`` verbatim."""
    return request_with(arguments, name="run_command")


def request_with(arguments: Any, name: str = "read_file") -> dict[str, Any]:
    return {
        "model": "m",
        "messages": [
            {"role": "user", "content": "read it"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "..."},
        ],
    }


def arguments_of(payload: dict[str, Any], index: int = 0) -> Any:
    return payload["messages"][1]["tool_calls"][index]["function"]["arguments"]


# --- the shapes that must be repaired ------------------------------------


def test_python_dict_repr_becomes_json() -> None:
    payload, notes = normalize_tool_arguments(request_with("{'path': 'src/main.py'}"))

    assert json.loads(arguments_of(payload)) == {"path": "src/main.py"}
    assert notes == ["read_file: python-style literal converted to JSON"]


def test_python_repr_with_nested_structures_and_none() -> None:
    raw = "{'path': 'a.py', 'lines': [1, 2], 'flag': True, 'opt': None}"

    payload, notes = normalize_tool_arguments(request_with(raw))

    assert json.loads(arguments_of(payload)) == {
        "path": "a.py",
        "lines": [1, 2],
        "flag": True,
        "opt": None,
    }
    assert notes


def test_already_decoded_object_is_encoded_to_a_string() -> None:
    payload, notes = normalize_tool_arguments(request_with({"path": "src/main.py"}))

    assert arguments_of(payload) == '{"path": "src/main.py"}'
    assert notes == ["read_file: object encoded to a JSON string"]


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_empty_arguments_become_an_empty_object(empty: str) -> None:
    payload, notes = normalize_tool_arguments(request_with(empty))

    assert arguments_of(payload) == "{}"
    assert notes == ["read_file: empty arguments replaced with {}"]


# --- the shapes that must be left exactly as they are --------------------


def test_a_wrong_guess_is_discarded_rather_than_forwarded() -> None:
    raw = '{"cmd": "echo "hi", done"}'

    payload, notes = normalize_tool_arguments(raw_request(raw))

    assert arguments_of(payload) == raw
    assert notes == []


def test_an_unrepairable_string_is_logged_with_the_decoder_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = '{"cmd": "echo "hi", done"}'

    with caplog.at_level(logging.WARNING, logger="llm_assistant_api.toolcalls"):
        _, notes = normalize_tool_arguments(raw_request(raw))

    assert notes == []
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "run_command" in message
    assert "delimiter" in message
    assert "echo " in message, "the text around the failure must be in the log"


def test_valid_json_string_is_untouched_and_payload_is_not_copied() -> None:
    original = request_with('{"path": "src/main.py"}')

    payload, notes = normalize_tool_arguments(original)

    assert notes == []
    assert payload is original, "an already-valid payload must not be rebuilt"


def test_unrepairable_string_is_passed_through_for_vllm_to_reject() -> None:
    payload, notes = normalize_tool_arguments(request_with("not json at all {{{"))

    assert arguments_of(payload) == "not json at all {{{"
    assert notes == []


def test_a_bare_scalar_literal_is_not_treated_as_arguments() -> None:
    payload, notes = normalize_tool_arguments(request_with("42"))

    assert arguments_of(payload) == "42"
    assert notes == []


def test_the_callers_dict_is_never_mutated() -> None:
    original = request_with("{'path': 'x'}")

    normalize_tool_arguments(original)

    assert arguments_of(original) == "{'path': 'x'}"


# --- malformed input must not raise --------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": None},
        {"messages": "not a list"},
        {"messages": [None, 42, "text"]},
        {"messages": [{"role": "user", "content": "hi"}]},
        {"messages": [{"role": "assistant", "tool_calls": None}]},
        {"messages": [{"role": "assistant", "tool_calls": [None]}]},
        {"messages": [{"role": "assistant", "tool_calls": [{"function": None}]}]},
        {"messages": [{"role": "assistant", "tool_calls": [{"function": {}}]}]},
        {"messages": [{"role": "assistant", "tool_calls": [{"no_function": 1}]}]},
    ],
)
def test_hostile_shapes_are_returned_unchanged(payload: dict[str, Any]) -> None:
    result, notes = normalize_tool_arguments(payload)

    assert result == payload
    assert notes == []


def test_unnamed_function_still_reports_a_note() -> None:
    payload = {
        "messages": [{"role": "assistant", "tool_calls": [{"function": {"arguments": "{'a': 1}"}}]}]
    }

    _, notes = normalize_tool_arguments(payload)

    assert notes == ["<unnamed>: python-style literal converted to JSON"]


def test_multiple_calls_across_multiple_messages_are_all_handled() -> None:
    payload = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "a", "arguments": "{'x': 1}"}},
                    {"function": {"name": "b", "arguments": '{"y": 2}'}},
                ],
            },
            {"role": "assistant", "tool_calls": [{"function": {"name": "c", "arguments": ""}}]},
        ]
    }

    result, notes = normalize_tool_arguments(payload)

    calls = result["messages"][0]["tool_calls"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}
    assert calls[1]["function"]["arguments"] == '{"y": 2}'
    assert result["messages"][1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert len(notes) == 2


def test_a_bare_set_literal_is_not_treated_as_arguments() -> None:
    payload, notes = normalize_tool_arguments(request_with("{1, 2}"))

    assert arguments_of(payload) == "{1, 2}"
    assert notes == []


def test_literal_that_parses_but_cannot_be_json_encoded_is_left_alone() -> None:
    raw = "{'tags': {1, 2}}"

    payload, notes = normalize_tool_arguments(request_with(raw))

    assert arguments_of(payload) == raw
    assert notes == []


@pytest.mark.parametrize("value", [42, None, True, 3.5])
def test_non_string_non_object_arguments_are_left_alone(value: Any) -> None:
    payload, notes = normalize_tool_arguments(request_with(value))

    assert arguments_of(payload) == value
    assert notes == []


def test_unencodable_object_is_left_alone() -> None:
    payload, notes = normalize_tool_arguments(request_with({"tags": {1, 2}}))

    assert arguments_of(payload) == {"tags": {1, 2}}
    assert notes == []


def test_literal_eval_cannot_execute_code() -> None:
    hostile = "__import__('os').system('touch /tmp/pwned')"

    payload, notes = normalize_tool_arguments(request_with(hostile))

    assert arguments_of(payload) == hostile
    assert notes == []
