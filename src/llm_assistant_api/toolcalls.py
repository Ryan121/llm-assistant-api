"""Normalise inbound tool-call arguments conservatively.

The OpenAI schema says ``tool_calls[].function.arguments`` is a *string*
containing JSON. Some editor integrations replay conversation history with
slightly malformed tool calls, but we should only repair the highest-confidence
cases:

* already-decoded ``dict``/``list`` objects
* Python-style ``dict``/``list`` reprs that safely round-trip through
  ``ast.literal_eval``
* optional empty-string compatibility for no-argument calls

Anything else is left unchanged and logged so upstream validation can reject it
rather than us guessing at the user's intent.
"""

from __future__ import annotations

import ast
import json
import logging
from typing import Any

__all__ = ["normalize_tool_arguments"]

log = logging.getLogger(__name__)

#: Why a particular argument value was rewritten. Used for log lines only.
_ENCODED = "object encoded to a JSON string"
_REPAIRED = "python-style literal converted to JSON"
_EMPTY = "empty arguments replaced with {}"

#: How much text either side of the failure to put in the log line.
_PREVIEW_RADIUS = 60


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


def _describe_failure(raw: str, error: ValueError) -> str:
    """A log line that says what broke and shows the text around it."""
    position = getattr(error, "pos", 0)
    start = max(0, position - _PREVIEW_RADIUS)
    end = min(len(raw), position + _PREVIEW_RADIUS)
    return (
        f"{error} (length={len(raw)}); before={raw[start:position]!r} after={raw[position:end]!r}"
    )


def _normalize_value(
    arguments: Any, name: str, *, normalize_empty: bool = True
) -> tuple[Any, str | None]:
    """Return ``(value, reason_it_changed)``; reason is ``None`` if untouched."""
    if isinstance(arguments, str):
        if not arguments.strip():
            if normalize_empty:
                return "{}", _EMPTY
            return arguments, None
        try:
            json.loads(arguments)
        except ValueError as error:
            repaired = _repair_literal(arguments)
            if repaired is not None:
                return repaired, _REPAIRED

            log.warning(
                "tool-call arguments for %s are not valid JSON and could not be "
                "repaired; vLLM will reject this request - %s",
                name,
                _describe_failure(arguments, error),
            )
            return arguments, None
        return arguments, None

    if isinstance(arguments, dict | list):
        try:
            return json.dumps(arguments), _ENCODED
        except (TypeError, ValueError):
            return arguments, None

    return arguments, None


def _normalize_message(message: Any, *, normalize_empty: bool = True) -> tuple[Any, list[str]]:
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

        name = function.get("name") or "<unnamed>"
        value, reason = _normalize_value(
            function["arguments"], name, normalize_empty=normalize_empty
        )
        if reason is None:
            rebuilt.append(call)
            continue

        rebuilt.append({**call, "function": {**function, "arguments": value}})
        notes.append(f"{name}: {reason}")

    if not notes:
        return message, []
    return {**message, "tool_calls": rebuilt}, notes


def normalize_tool_arguments(
    payload: dict[str, Any], *, normalize_empty: bool = True
) -> tuple[dict[str, Any], list[str]]:
    """Return ``(payload, notes)`` with conservative tool-call repairs.

    ``notes`` is empty when nothing needed changing, in which case the original
    payload object is returned unchanged.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, []

    all_notes: list[str] = []
    rebuilt: list[Any] = []

    for message in messages:
        normalized, notes = _normalize_message(message, normalize_empty=normalize_empty)
        rebuilt.append(normalized)
        all_notes.extend(notes)

    if not all_notes:
        return payload, []
    return {**payload, "messages": rebuilt}, all_notes
