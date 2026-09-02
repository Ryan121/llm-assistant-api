"""Normalise inbound tool-call arguments so vLLM will accept them.

The OpenAI schema says ``tool_calls[].function.arguments`` is a *string*
containing JSON. Editor extensions replaying conversation history routinely
break that in four ways:

* a Python dict repr -- ``{'path': 'src/main.py'}`` (single quotes)
* an already-decoded object -- ``{"path": "src/main.py"}`` as a real dict
* an empty string for a no-argument call
* unescaped characters inside a string value -- a bare ``"`` in a shell
  command, a raw newline in a patch body, a Windows path's ``\\U``

vLLM calls ``json.loads`` on the value in ``_postprocess_messages`` and returns
400 for all four, which surfaces in the editor as a dead assistant. Most other
OpenAI-compatible servers are lenient, so the client authors never notice.
Worse, the bad message stays in the editor's history, so every retry replays it
and fails at the same offset forever.

Repairs are logged, never silent, and a valid JSON string is passed through
untouched -- so this can only turn a failing request into a working one. A
string we cannot repair is passed through for vLLM to reject, but is logged at
WARNING with the decoder's own message and the text around the failure, because
the 400 that follows says nothing about which call was at fault.
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
_ESCAPED = "unescaped characters inside a string value escaped"

#: Characters that may legally follow a string's closing quote in JSON.
_AFTER_STRING = frozenset(",}]:")

#: Characters that form a valid two-character JSON escape after a backslash.
_VALID_ESCAPES = frozenset('"\\/bfnrtu')

#: Control characters JSON spells with a short escape rather than \uXXXX.
_CONTROL_ESCAPES = {
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

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


def _escape_string_bodies(raw: str) -> str:
    """Escape characters that are only illegal *inside* a JSON string.

    Walks the text tracking whether we are inside a string literal. A ``"`` is
    treated as the closing quote only when the next non-space character could
    legally follow one; otherwise the model meant a literal quote and we escape
    it. Raw control characters and backslashes that do not begin a valid escape
    are fixed the same way. The result is a guess, so the caller must parse it
    before trusting it.
    """
    out: list[str] = []
    in_string = False
    index = 0
    length = len(raw)

    while index < length:
        char = raw[index]

        if not in_string:
            out.append(char)
            in_string = char == '"'
            index += 1
            continue

        if char == "\\":
            following = raw[index + 1 : index + 2]
            if following in _VALID_ESCAPES:
                out.append(char)
                out.append(following)
                index += 2
            else:  # a lone backslash, e.g. a Windows path
                out.append("\\\\")
                index += 1
            continue

        if char == '"':
            lookahead = index + 1
            while lookahead < length and raw[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead >= length or raw[lookahead] in _AFTER_STRING:
                out.append(char)
                in_string = False
            else:
                out.append('\\"')
            index += 1
            continue

        if char < " ":
            out.append(_CONTROL_ESCAPES.get(char) or f"\\u{ord(char):04x}")
            index += 1
            continue

        out.append(char)
        index += 1

    return "".join(out)


def _repair_escapes(raw: str) -> str | None:
    """Re-escape a string's contents, or ``None`` if that does not yield JSON.

    The rewrite only counts as a repair when it parses to an object or array,
    so a heuristic that guessed wrong is discarded rather than forwarded.
    """
    candidate = _escape_string_bodies(raw)
    if candidate == raw:
        return None

    try:
        value = json.loads(candidate)
    except ValueError:
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


def _normalize_value(arguments: Any, name: str) -> tuple[Any, str | None]:
    """Return ``(value, reason_it_changed)``; reason is ``None`` if untouched."""
    if isinstance(arguments, str):
        if not arguments.strip():
            return "{}", _EMPTY
        try:
            json.loads(arguments)
        except ValueError as error:
            # Escapes are tried first: Python's escape set is wider than JSON's
            # (\a, \v, \0, \xNN), so literal_eval would happily turn the
            # `app.py` in a Windows path into a bell character.
            repaired = _repair_escapes(arguments)
            if repaired is not None:
                return repaired, _ESCAPED

            repaired = _repair_literal(arguments)
            if repaired is not None:
                return repaired, _REPAIRED

            # vLLM's 400 names neither the tool nor the text, so say both here
            # or the next occurrence is just as opaque as this one.
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

        name = function.get("name") or "<unnamed>"
        value, reason = _normalize_value(function["arguments"], name)
        if reason is None:
            rebuilt.append(call)
            continue

        rebuilt.append({**call, "function": {**function, "arguments": value}})
        notes.append(f"{name}: {reason}")

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
