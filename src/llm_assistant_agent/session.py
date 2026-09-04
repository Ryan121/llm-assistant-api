"""The agent loop.

One turn is: send the transcript, stream the reply, execute whatever tool calls
came back, append their results, and go round again until the model answers
without calling a tool.

Two guards keep a runaway loop from being expensive: a hard cap on iterations
per user message, and transcript compaction when the conversation approaches
the model's context window. Compaction drops the middle of the conversation
rather than the ends, because the system prompt and the last few turns are what
the model is actually working from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from .client import AssistantTurn, ChatClient, ToolCall, TurnError
from .render import Renderer
from .tools import TOOL_SCHEMAS, ToolBox, ToolOutcome
from .workspace import Workspace

__all__ = ["Session", "SYSTEM_PROMPT"]

SYSTEM_PROMPT = """\
You are a coding assistant working directly in a user's repository.

How to work:
- Look before you leap: use grep and list_files to find the relevant code, and
  read a file before editing it.
- Make the smallest change that does the job. Prefer several small edit_file
  calls over one sweeping rewrite.
- When you edit, copy old_string verbatim from what read_file showed you,
  including exact indentation, and include enough surrounding context that it
  appears exactly once in the file.
- After changing code, check it: run the project's tests or type checker with
  the run tool if there is an obvious command for it.
- If a tool returns an error, read it and correct course - the message says
  what to do differently.

Your edits go into the user's working tree uncommitted, so they will review a
diff afterwards. Never run git commit, git push, or any other command that
rewrites history or publishes work; the user does that themselves.

Be brief. Explain what you changed and why, not what you are about to do."""

#: Stop a loop that is going nowhere. Generous enough for a real multi-file
#: change, small enough that a stuck model does not run for an hour.
_MAX_STEPS = 40

#: Compact when the transcript passes this share of the context window.
_COMPACT_AT = 0.75

_CHARS_PER_TOKEN = 3.5


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Same cheap heuristic the gateway's context guard uses."""
    characters = sum(len(json.dumps(message, default=str)) for message in messages)
    return int(characters / _CHARS_PER_TOKEN)


def _summarise_arguments(name: str, arguments: dict[str, Any]) -> str:
    """One line describing a tool call, for the transcript the user watches."""
    if name in {"read_file", "edit_file", "write_file"}:
        return str(arguments.get("path", ""))
    if name == "grep":
        pattern = str(arguments.get("pattern", ""))
        glob = arguments.get("glob")
        return f"{pattern}" + (f"  in {glob}" if glob else "")
    if name == "list_files":
        return str(arguments.get("pattern", "") or "(all)")
    if name == "run":
        return str(arguments.get("command", ""))
    return ""


@dataclass
class Session:
    """One conversation against one workspace."""

    client: ChatClient
    workspace: Workspace
    renderer: Renderer
    context_window: int = 0
    auto_approve: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    _rules_sent: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        # The rules open the first user turn rather than sitting in a system
        # message. A long system message displaces Qwen3-Coder's tool-call
        # format exemplar: the model then emits <function=...> without the
        # opening <tool_call>, and vLLM's parser - correctly - streams the
        # malformed call through as plain text, so no tool ever runs. A short
        # system message is fine; this one is not, and telling the model to
        # emit the wrapper does not fix it.
        self._rules_sent = bool(self.messages)

    # --- approval ---------------------------------------------------------

    def approve(self, description: str, detail: str) -> bool:
        if self.auto_approve:
            self.renderer.note(f"{description}: {detail}")
            return True
        return self.renderer.approval(description, detail)

    # --- the loop ---------------------------------------------------------

    async def ask(self, http: httpx.AsyncClient, user_message: str) -> None:
        """Run one user message to completion."""
        if not self._rules_sent:
            user_message = f"{SYSTEM_PROMPT}\n\n---\n\n{user_message}"
            self._rules_sent = True
        self.messages.append({"role": "user", "content": user_message})
        toolbox = ToolBox(self.workspace, self.approve)

        for _ in range(_MAX_STEPS):
            self._compact_if_needed()

            wrote_text = False

            def on_text(chunk: str) -> None:
                nonlocal wrote_text
                wrote_text = True
                self.renderer.text_delta(chunk)

            try:
                turn, raw_calls = await self.client.turn(
                    http, self.messages, TOOL_SCHEMAS, on_text=on_text
                )
            except TurnError as exc:
                self.renderer.error(str(exc))
                return

            if wrote_text:
                self.renderer.end_text()

            self.messages.append(turn.as_message(raw_calls))

            for problem in turn.malformed:
                self.renderer.tool_error(problem)

            if not turn.tool_calls:
                if turn.malformed:
                    # Nothing ran, but the model thinks it called something.
                    # Tell it so, rather than leaving the turn dangling.
                    self.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your tool call did not arrive intact: "
                                + "; ".join(turn.malformed)
                                + ". Please try again."
                            ),
                        }
                    )
                    continue
                self._finish_turn()
                return

            self._execute(toolbox, turn)

        self.renderer.warn(f"Stopped after {_MAX_STEPS} steps without a final answer.")
        self._finish_turn()

    def _execute(self, toolbox: ToolBox, turn: AssistantTurn) -> None:
        for call in turn.tool_calls:
            self.renderer.tool_call(call.name, _summarise_arguments(call.name, call.arguments))
            outcome = toolbox.invoke(call.name, call.arguments)
            self._report(call, outcome)
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": outcome.content,
                }
            )

    def _report(self, call: ToolCall, outcome: ToolOutcome) -> None:
        if outcome.is_error:
            self.renderer.tool_error(outcome.content.splitlines()[0] if outcome.content else "")
            return
        if outcome.diff:
            self.renderer.diff(outcome.diff)
        elif call.name == "run":
            for line in outcome.content.splitlines()[:20]:
                self.renderer.note(line)

    def _finish_turn(self) -> None:
        stat = self.workspace.diff_stat() if self.workspace.is_git_repo else ""
        self.renderer.turn_summary(stat, _estimate_tokens(self.messages), self.context_window)

    # --- context ----------------------------------------------------------

    def _compact_if_needed(self) -> None:
        """Drop the middle of the transcript when it gets close to the window.

        The system prompt sets the rules and the recent turns hold the current
        task; it is the long tail of old file reads in between that can go.
        """
        if self.context_window <= 0:
            return
        if _estimate_tokens(self.messages) < self.context_window * _COMPACT_AT:
            return
        if len(self.messages) <= 8:
            return

        head = self.messages[:1]
        tail = self.messages[-6:]
        # A tool result must keep the assistant message that requested it, or
        # the ids dangle and vLLM rejects the request.
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]

        dropped = len(self.messages) - len(head) - len(tail)
        if dropped <= 0:
            return

        self.messages = [
            *head,
            {
                "role": "user",
                "content": (
                    f"[{dropped} earlier messages were dropped to stay within the "
                    "context window. Re-read any file you need rather than relying "
                    "on memory of it.]"
                ),
            },
            *tail,
        ]
        self.renderer.note(f"compacted transcript ({dropped} messages dropped)")
