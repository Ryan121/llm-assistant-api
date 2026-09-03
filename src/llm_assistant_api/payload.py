"""Inbound payload normalisation for OpenAI-compatible requests.

The gateway is intentionally forgiving about a few malformed shapes that show
up in editor-agent traffic:

* ``messages[*].tool_calls`` replayed as a single object instead of a list
* ``messages[*].content`` wrapped in a small dict such as ``{"text": "..."}``
* nested tool-call arguments that are not valid JSON strings

Each repair is conservative and logged by the caller so the operator can see
what was changed without the request failing downstream in vLLM.
"""

from __future__ import annotations

import json
from typing import Any

from .toolcalls import normalize_tool_arguments as _normalize_tool_arguments

__all__ = ["normalize_inbound_payload"]


def _message_label(message: Any) -> str:
    if isinstance(message, dict):
        role = message.get("role")
        if isinstance(role, str) and role:
            return role
    return "<message>"


def _normalize_message_content(message: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    content = message.get("content")
    if isinstance(content, (str, list)) or content is None:
        return message, []

    label = _message_label(message)

    # Common malformed forms from agent clients: the text is wrapped in a tiny
    # object instead of being sent as a raw string.
    if isinstance(content, dict):
        if set(content) == {"text"} or set(content) == {"content"}:
            value = content.get("text", content.get("content"))
            if isinstance(value, (str, list)) or value is None:
                return {**message, "content": value}, [
                    f"{label}: content object unwrapped to its text value"
                ]

        try:
            encoded = json.dumps(content)
        except (TypeError, ValueError):
            encoded = str(content)
        return {**message, "content": encoded}, [
            f"{label}: content object converted to a JSON string"
        ]

    return {**message, "content": str(content)}, [
        f"{label}: {type(content).__name__} content coerced to a string"
    ]


def _normalize_tool_calls(message: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) or tool_calls is None:
        return message, []

    label = _message_label(message)
    if isinstance(tool_calls, dict):
        return {**message, "tool_calls": [tool_calls]}, [
            f"{label}: single tool_calls object wrapped in a list"
        ]

    if isinstance(tool_calls, tuple):
        return {**message, "tool_calls": list(tool_calls)}, [
            f"{label}: tool_calls tuple converted to a list"
        ]

    return message, []


def _normalize_message(message: Any) -> tuple[Any, list[str]]:
    if not isinstance(message, dict):
        return message, []

    notes: list[str] = []
    current = message

    current, content_notes = _normalize_message_content(current)
    notes.extend(content_notes)

    current, tool_notes = _normalize_tool_calls(current)
    notes.extend(tool_notes)

    return current, notes


def normalize_inbound_payload(
    payload: dict[str, Any],
    *,
    normalize_tool_arguments: bool = True,
    normalize_empty_tool_arguments: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Return a best-effort, copy-on-write repair of an inbound request body."""
    current = payload
    notes: list[str] = []

    messages = current.get("messages")
    if isinstance(messages, list):
        rebuilt: list[Any] = []
        changed = False

        for message in messages:
            normalized, message_notes = _normalize_message(message)
            rebuilt.append(normalized)
            notes.extend(message_notes)
            changed |= normalized is not message

        if changed:
            current = {**current, "messages": rebuilt}

    if normalize_tool_arguments:
        current, tool_notes = _normalize_tool_arguments(
            current, normalize_empty=normalize_empty_tool_arguments
        )
        notes.extend(tool_notes)

    return current, notes
