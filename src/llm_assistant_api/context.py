"""Pre-flight context-window guard.

An agent conversation grows every turn. When it finally exceeds the engine's
``--max-model-len`` vLLM answers with a 400 whose message is about token ids,
which reaches the user as an agent that simply stopped working. Catching it
here costs one pass over the payload and lets us say *which* budget was blown
and by how much.

The estimate is deliberately a heuristic. An exact count means shipping the
model's tokeniser, which would add a ~400 MB dependency to a 60 MB image in
order to sharpen a check that only has to catch gross overflow. Characters
divided by ``CHARS_PER_TOKEN`` is within ~15% on source code, and the margin in
``context_guard_margin`` absorbs the error.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import ContextOverflowError

__all__ = ["estimate_payload_tokens", "guard_context"]

#: Per-message framing the chat template adds (role markers, delimiters).
_MESSAGE_OVERHEAD_TOKENS = 4


def _text_of(content: Any) -> str:
    """Flatten a message ``content`` to the text that will be tokenised."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        # OpenAI content parts: [{"type": "text", "text": "..."}, ...]
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                value = part.get("text")
                parts.append(value if isinstance(value, str) else json.dumps(part))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)

    if content is None:
        return ""

    return json.dumps(content, default=str)


def _tool_call_text(message: dict[str, Any]) -> str:
    """Tool calls and their results are prompt tokens too, and often the bulk."""
    chunks: list[str] = []

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, dict):
                chunks.append(json.dumps(call, default=str))

    return "".join(chunks)


def estimate_payload_tokens(payload: dict[str, Any], *, chars_per_token: float) -> int:
    """Rough prompt-token count for a chat or completion body."""
    characters = 0
    messages = payload.get("messages")

    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                characters += len(str(message))
                continue
            characters += len(_text_of(message.get("content")))
            characters += len(_tool_call_text(message))
    else:
        # /v1/completions sends a bare prompt instead of messages.
        characters += len(_text_of(payload.get("prompt")))

    # Tool schemas are resent in full on every turn of an agent loop, and for a
    # rich tool set they can outweigh the conversation.
    tools = payload.get("tools")
    if tools is not None:
        characters += len(json.dumps(tools, default=str))

    message_count = len(messages) if isinstance(messages, list) else 1
    return int(characters / chars_per_token) + message_count * _MESSAGE_OVERHEAD_TOKENS


def _reserved_completion_tokens(payload: dict[str, Any]) -> int:
    for field in ("max_tokens", "max_completion_tokens"):
        value = payload.get(field)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def guard_context(payload: dict[str, Any], *, budget: int, chars_per_token: float) -> int:
    """Raise ``ContextOverflowError`` if the request cannot fit. Returns the estimate.

    ``budget`` of 0 disables the check, which is the default: the gateway does
    not know the engine's ``--max-model-len`` unless the operator tells it.
    """
    prompt_tokens = estimate_payload_tokens(payload, chars_per_token=chars_per_token)
    if budget <= 0:
        return prompt_tokens

    # vLLM budgets prompt + generation against the same window, so a request
    # that only fits because it asked for no output does not really fit.
    required = prompt_tokens + _reserved_completion_tokens(payload)
    if required > budget:
        raise ContextOverflowError(estimated_tokens=required, budget=budget)

    return prompt_tokens
