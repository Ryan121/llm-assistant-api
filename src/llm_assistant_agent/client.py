"""Streaming client for the gateway's ``/v1/chat/completions``.

The only subtle part is tool-call assembly. OpenAI-style streaming sends a tool
call in fragments - the name in one delta, the JSON arguments a few characters
at a time across many more - and vLLM's parser will occasionally end a stream
mid-argument. So nothing is executed until the stream is complete *and* the
arguments parse. A partial tool call is a failure to report, never something
to act on.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = ["AssistantTurn", "ChatClient", "ToolCall", "TurnError"]


class TurnError(Exception):
    """The gateway could not complete the turn."""


@dataclass
class ToolCall:
    """One assembled, validated tool call."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _PartialToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class AssistantTurn:
    """What the model produced for one turn."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    #: Tool calls that arrived malformed. Reported back so the model can retry.
    malformed: list[str] = field(default_factory=list)

    def as_message(self, raw_calls: list[dict[str, Any]]) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if raw_calls:
            message["tool_calls"] = raw_calls
        return message


class ChatClient:
    """Talks to one gateway."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 900.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def turn(
        self,
        client: httpx.AsyncClient,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        on_text: Callable[[str], None],
    ) -> tuple[AssistantTurn, list[dict[str, Any]]]:
        """Run one streamed turn.

        Returns the assembled turn and the raw tool-call payloads, which have
        to go back into the transcript verbatim so the ids line up.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        turn = AssistantTurn()
        partials: dict[int, _PartialToolCall] = {}

        request = client.build_request(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )

        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise TurnError(f"Cannot reach the gateway at {self.base_url}: {exc}") from exc

        try:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                raise TurnError(_explain_error(response.status_code, body))

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                _consume(event, turn, partials, on_text)
        finally:
            await response.aclose()

        raw_calls = _finalise(turn, partials)
        return turn, raw_calls


def _consume(
    event: dict[str, Any],
    turn: AssistantTurn,
    partials: dict[int, _PartialToolCall],
    on_text: Callable[[str], None],
) -> None:
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return

    choice = choices[0]
    if not isinstance(choice, dict):
        return

    if choice.get("finish_reason"):
        turn.finish_reason = str(choice["finish_reason"])

    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return

    content = delta.get("content")
    if isinstance(content, str) and content:
        turn.content += content
        on_text(content)

    for fragment in delta.get("tool_calls") or []:
        if not isinstance(fragment, dict):
            continue
        index = fragment.get("index", 0)
        if not isinstance(index, int):
            continue
        partial = partials.setdefault(index, _PartialToolCall())
        if isinstance(fragment.get("id"), str):
            partial.id = fragment["id"]
        function = fragment.get("function")
        if isinstance(function, dict):
            if isinstance(function.get("name"), str):
                partial.name += function["name"]
            if isinstance(function.get("arguments"), str):
                partial.arguments += function["arguments"]


#: A tool call the upstream parser did not recognise arrives as ordinary
#: content instead of a tool call, and nothing runs. The model has to emit a
#: wrapper the parser matches, and drops it often enough - Qwen3-Coder omits
#: the opening <tool_call> when the system message is long - that it is worth
#: catching by hand. Without this the failure is silent: the reply is XML the
#: user did not ask for and the agent stops, having done nothing.
_LEAKED_CALL = re.compile(r"<function\s*=\s*([A-Za-z_][A-Za-z0-9_-]*)\s*>")


def _finalise(turn: AssistantTurn, partials: dict[int, _PartialToolCall]) -> list[dict[str, Any]]:
    """Validate assembled calls, keeping only those safe to execute."""
    raw: list[dict[str, Any]] = []

    for index in sorted(partials):
        partial = partials[index]
        if not partial.name:
            turn.malformed.append("a tool call arrived with no function name")
            continue

        text = partial.arguments.strip() or "{}"
        try:
            arguments = json.loads(text)
        except json.JSONDecodeError as exc:
            # Truncated mid-argument: the usual shape of a stream that ended
            # early or hit the token cap.
            turn.malformed.append(
                f"{partial.name}: arguments were not valid JSON ({exc.msg}). "
                "The call was not executed."
            )
            continue

        if not isinstance(arguments, dict):
            turn.malformed.append(f"{partial.name}: arguments were not a JSON object.")
            continue

        call_id = partial.id or f"call_{index}"
        turn.tool_calls.append(ToolCall(id=call_id, name=partial.name, arguments=arguments))
        raw.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": partial.name, "arguments": text},
            }
        )

    # Only when nothing parsed. A turn that made a real call and also happens to
    # quote this syntax - likely while working on this repository - is fine, and
    # second-guessing it would be worse than the failure being caught here.
    if not raw:
        leaked = _LEAKED_CALL.search(turn.content)
        # A real emission carries an argument block or the wrapper the parser
        # missed. Prose that just names the syntax carries neither, and telling
        # the model to retry a call it never made wastes a turn.
        emitted = "</tool_call>" in turn.content or "<parameter=" in turn.content
        if leaked and emitted and "</function>" in turn.content:
            turn.malformed.append(
                f"{leaked.group(1)}: the call arrived as plain text rather than a "
                "tool call, so it was not executed. Emit it in the exact format you "
                "were given, including both the opening and the closing wrapper tags."
            )

    return raw


def _explain_error(status_code: int, body: str) -> str:
    """Turn a gateway error into something the user can act on."""
    message = body
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            message = str(parsed["error"].get("message", body))
            code = parsed["error"].get("code")
            if code == "context_length_exceeded":
                return f"{message} (Try /compact or starting a new session.)"
            if code == "invalid_api_key":
                return f"{message} (Check API_KEYS in .env.)"
    except json.JSONDecodeError:
        pass
    return f"Gateway returned {status_code}: {message[:400]}"
