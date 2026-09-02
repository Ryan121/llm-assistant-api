"""Normalise inbound tool-call arguments so vLLM will accept them.

The OpenAI schema says ``tool_calls[].function.arguments`` is a *string*
containing JSON. Editor extensions replaying conversation history routinely
break that in three ways:

* a Python dict repr -- ``{'path': 'src/main.py'}`` (single quotes)
* an already-decoded object -- ``{"path": "src/main.py"}`` as a real dict
* an empty string for a no-argument call

vLLM calls ``json.loads`` on the value in ``_postprocess_messages`` and returns
400 for all three, which surfaces in the editor as a dead assistant. Most other
OpenAI-compatible servers are lenient, so the client authors never notice.

Repairs are logged, never silent, and a valid JSON string is passed through
untouched -- so this can only turn a failing request into a working one.
"""

from __future__ import annotations

import ast
import json
from typing import Any

__all__ = ["normalize_tool_arguments"]

#: Why a particular argument value was rewritten. Used for log lines only.
_ENCODED = "object encoded to a JSON string"
_REPAIRED = "python-style literal converted to JSON"
_EMPTY = "empty arguments replaced with {}"


def _repair_literal(raw: str) -> str | None:
    """Convert a Python-literal string to JSON, or ``None`` if it is not one.

    ``ast.literal_eval`` only evaluates literals -- no names, calls or
    operators -- so this cannot execute attacker-supplied code.
    """
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None

    if not isinstance(value, dict | list):
        return None

    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


def _normalize_value(arguments: Any) -> tuple[Any, str | None]:
    """Return ``(value, reason_it_changed)``; reason is ``None`` if untouched."""
    if isinstance(arguments, str):
        if not arguments.strip():
            return "{}", _EMPTY
        try:
            json.loads(arguments)
        except ValueError:
            repaired = _repair_literal(arguments)
            return (arguments, None) if repaired is None else (repaired, _REPAIRED)
        return arguments, None

    if isinstance(arguments, dict | list):
        try:
            return json.dumps(arguments), _ENCODED
        except (TypeError, ValueError):
            return arguments, None

    return arguments, None


def _normalize_message(message: Any) -> tuple[Any, list[str]]:
    """Copy-on-write: the original dict is returned when nothing changes."""
    if not isinstance(message, dict):
        return message, []

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return message, []

    notes: list[str] = []
    rebuilt: list[Any] = []

    for call in tool_calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or "arguments" not in function:
            rebuilt.append(call)
            continue

        value, reason = _normalize_value(function["arguments"])
        if reason is None:
            rebuilt.append(call)
            continue

        rebuilt.append({**call, "function": {**function, "arguments": value}})
        notes.append(f"{function.get('name') or '<unnamed>'}: {reason}")

    if not notes:
        return message, []
    return {**message, "tool_calls": rebuilt}, notes


def normalize_tool_arguments(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(payload, notes)`` with every tool-call argument JSON-encoded.

    ``notes`` is empty when nothing needed changing, in which case the original
    payload object is returned unchanged.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, []

    all_notes: list[str] = []
    rebuilt: list[Any] = []

    for message in messages:
        normalized, notes = _normalize_message(message)
        rebuilt.append(normalized)
        all_notes.extend(notes)

    if not all_notes:
        return payload, []
    return {**payload, "messages": rebuilt}, all_notes
