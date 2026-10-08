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

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from .checks import Checks
from .client import AssistantTurn, ChatClient, ToolCall, TurnError
from .render import InputUnavailableError, Progress, Renderer
from .sandbox import HostSandbox, Sandbox
from .tools import DECLINED, TOOL_SCHEMAS, ToolBox, ToolOutcome
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
- Never write code that parses a file you have not looked at. Call
  read_document on the actual PDF, CSV or spreadsheet first and write the
  parser against what it shows you. Code written from a guess at the format
  runs, returns nothing, and looks finished.
- Editing a file may report problems found in it afterwards. Those are real -
  fix them before moving on, rather than leaving the file worse than you found
  it.
- Before your final answer, call git_diff and read your whole change. Edits
  that were each right on their own can still be wrong together: a leftover
  import, a helper you stopped using, an edit landed in the wrong place.

The user approves each edit as a diff before it lands, so keep them small and
self-explanatory. Never run git commit, git push, or any other command that
rewrites history or publishes work; the user does that themselves.

Never throw uncommitted work away either - no git checkout, restore, reset,
stash or clean of a file you did not create in this session. The working tree
holds the user's own changes and whatever an earlier session left behind, so a
diff will show more than you remember doing. That is normal and not yours to
undo: an uncommitted change is the only copy there is.

Be brief. Explain what you changed and why, not what you are about to do."""

#: Stop a loop that is going nowhere. Generous enough for a real multi-file
#: change, small enough that a stuck model does not run for an hour.
_MAX_STEPS = 40

#: A step cap alone does not catch the loop that actually happens, which is
#: the same failing call over and over - in one real session, the identical
#: 132-line edit three times running, then "I'm still getting the same error".
#: Told plainly on the second, abandoned on the fourth, because a fourth
#: identical call has no more chance than the third and the user is better off
#: with the turn back.
_REPEAT_WARN = 2
_REPEAT_ABORT = 4

#: Compact when the transcript passes this share of the context window.
_COMPACT_AT = 0.75

_CHARS_PER_TOKEN = 3.5


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Same cheap heuristic the gateway's context guard uses."""
    characters = sum(len(json.dumps(message, default=str)) for message in messages)
    return int(characters / _CHARS_PER_TOKEN)


#: What the spinner says while a tool runs. Only the slow ones are worth
#: naming; the rest finish inside the spinner's own start-up delay.
_WORKING_LABELS = {
    "run": "running",
    "grep": "searching",
    "list_files": "listing files",
    "read_document": "reading document",
    "git_diff": "diffing",
}


def _working_label(name: str) -> str:
    # Edits are the slow ones only because of the checkers that follow them,
    # which is what the user is actually waiting for.
    if name in {"edit_file", "write_file"}:
        return "checking"
    return _WORKING_LABELS.get(name, "working")


class _Stream:
    """Sends one turn's streamed text to the renderer, and runs the spinner.

    An object rather than a closure because the loop it belongs to runs many
    turns, and a closure over a per-iteration spinner is the classic late
    binding bug waiting to happen.

    The spinner has to stop and start again within a single turn. Qwen3-Coder
    narrates before it acts - in a real session, 97 turns in 130 carried prose
    *and* tool calls - so a spinner that simply stopped at the first token
    would disappear just before the longest silence of the turn: the arguments
    of a whole-file write, streaming one fragment at a time with nothing
    printed. Resuming on the first tool fragment covers that.
    """

    def __init__(self, renderer: Renderer, waiting: Progress) -> None:
        self._renderer = renderer
        self._waiting = waiting
        self.wrote = False
        #: Prose has been closed off with a newline and the spinner restarted
        #: below it, so the caller must not end the line a second time.
        self.resumed = False

    def __call__(self, chunk: str) -> None:
        # Before the first character, not after: the spinner and the reply
        # share a line, and whoever writes second wins it.
        self._waiting.stop()
        self.wrote = True
        self._renderer.text_delta(chunk)

    def tool_delta(self) -> None:
        if not self.wrote or self.resumed:
            return  # never wrote, so the spinner is still up, or already back
        # The prose owns the line it stopped on; the spinner needs its own.
        self._renderer.end_text()
        self.resumed = True
        self._waiting.start()


def _summarise_arguments(name: str, arguments: dict[str, Any]) -> str:
    """One line describing a tool call, for the transcript the user watches."""
    if name in {"read_file", "edit_file", "write_file"}:
        return str(arguments.get("path", ""))
    if name == "read_document":
        return str(arguments.get("path", "")) + (" (full text)" if arguments.get("full") else "")
    if name == "grep":
        pattern = str(arguments.get("pattern", ""))
        glob = arguments.get("glob")
        return f"{pattern}" + (f"  in {glob}" if glob else "")
    if name == "list_files":
        return str(arguments.get("pattern", "") or "(all)")
    if name == "run":
        return str(arguments.get("command", ""))
    if name == "git_diff":
        path = arguments.get("path")
        return str(path) if path else ("summary" if arguments.get("summary") else "(all)")
    return ""


@dataclass
class Session:
    """One conversation against one workspace."""

    client: ChatClient
    workspace: Workspace
    renderer: Renderer
    context_window: int = 0
    auto_approve: bool = False
    checks: Checks = field(default_factory=Checks)
    sandbox: Sandbox = field(default_factory=HostSandbox)
    #: What is sent to the model. Compaction rewrites this.
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: Everything that was actually said, in order, never compacted. Compaction
    #: is a way of fitting a conversation into a context window; it is not a
    #: decision to forget what happened, and persisting the compacted list
    #: destroyed 23 turns of a real session before this existed. Save this.
    history: list[dict[str, Any]] = field(default_factory=list, init=False)
    _rules_sent: bool = field(default=False, init=False)
    _reported_broken: list[bool] = field(default_factory=list, init=False)
    #: How many times each tool call has failed, keyed by name and arguments.
    _failures: Counter[str] = field(default_factory=Counter, init=False)

    def __post_init__(self) -> None:
        # The rules open the first user turn rather than sitting in a system
        # message. A long system message displaces Qwen3-Coder's tool-call
        # format exemplar: the model then emits <function=...> without the
        # opening <tool_call>, and vLLM's parser - correctly - streams the
        # malformed call through as plain text, so no tool ever runs. A short
        # system message is fine; this one is not, and telling the model to
        # emit the wrapper does not fix it.
        self._rules_sent = bool(self.messages)
        # A resumed session starts with the whole of what it was given; from
        # here the two diverge only when compaction trims what is sent.
        self.history = list(self.messages)

    def _say(self, message: dict[str, Any]) -> None:
        """Add a message to both the working set and the archive."""
        self.messages.append(message)
        self.history.append(message)

    # --- approval ---------------------------------------------------------

    def approve(self, description: str, detail: str) -> bool:
        if self.auto_approve:
            # A one-line note for a command; for an edit the detail is a diff,
            # which is shown properly once the edit has landed.
            self.renderer.note(description if "\n" in detail else f"{description}: {detail}")
            return True
        return self.renderer.approval(description, detail)

    # --- the loop ---------------------------------------------------------

    async def ask(self, http: httpx.AsyncClient, user_message: str) -> None:
        """Run one user message to completion.

        Cancellable. On cancellation the transcript is left in a state the
        gateway will still accept, so the session can carry on, and the
        ``CancelledError`` is re-raised for the caller to report.
        """
        try:
            await self._ask(http, user_message)
        except asyncio.CancelledError:
            closed = self.repair_after_cancel()
            if closed:
                self.renderer.note(
                    f"closed {closed} unfinished tool call{'s' if closed != 1 else ''}"
                )
            raise

    async def _ask(self, http: httpx.AsyncClient, user_message: str) -> None:
        if not self._rules_sent:
            user_message = f"{SYSTEM_PROMPT}\n\n---\n\n{user_message}"
            self._rules_sent = True
        self._say({"role": "user", "content": user_message})
        toolbox = ToolBox(self.workspace, self.approve, self.checks, self.sandbox)

        for _ in range(_MAX_STEPS):
            self._compact_if_needed()

            # The wait before the first token is the longest silence in a turn
            # - prefill over a full context window is tens of seconds - and it
            # looks exactly like a hang.
            waiting = self.renderer.working("thinking")
            stream = _Stream(self.renderer, waiting)

            try:
                async with waiting:
                    turn, raw_calls = await self.client.turn(
                        http,
                        self.messages,
                        TOOL_SCHEMAS,
                        on_text=stream,
                        on_tool_delta=stream.tool_delta,
                    )
            except TurnError as exc:
                self.renderer.error(str(exc))
                return

            if stream.wrote and not stream.resumed:
                self.renderer.end_text()

            self._say(turn.as_message(raw_calls))

            for problem in turn.malformed:
                self.renderer.tool_error(problem)

            if not turn.tool_calls:
                if turn.malformed:
                    # Nothing ran, but the model thinks it called something.
                    # Tell it so, rather than leaving the turn dangling.
                    self._say(
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

            if not await self._execute(toolbox, turn):
                self._finish_turn()
                return

        self.renderer.warn(f"Stopped after {_MAX_STEPS} steps without a final answer.")
        self._finish_turn()

    def _repetition(self, call: ToolCall, outcome: ToolOutcome) -> tuple[str, bool]:
        """Note a repeated failure, and say what to tell the model about it.

        Returns the text to append to the tool result and whether the turn
        should be abandoned. Keyed on the arguments as well as the name, so a
        model working steadily through several files is not accused of
        looping, while one resending a byte-identical failing call is.
        """
        if not outcome.is_error:
            return "", False
        signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
        self._failures[signature] += 1
        count = self._failures[signature]
        if count < _REPEAT_WARN:
            return "", False
        if count >= _REPEAT_ABORT:
            return (
                f"\n\nThis identical call has now failed {count} times. Stopping here.",
                True,
            )
        return (
            f"\n\nYou have made this exact call {count} times and it has failed the same "
            "way each time; it will not behave differently on the next. Do something "
            "else: read the file as it is now, try a smaller change, or say what is "
            "blocking you.",
            False,
        )

    async def _execute(self, toolbox: ToolBox, turn: AssistantTurn) -> bool:
        """Run this turn's tool calls. False means the turn should stop."""
        for call in turn.tool_calls:
            self.renderer.tool_call(call.name, _summarise_arguments(call.name, call.arguments))

            # Asked here, on the event loop, rather than inside the worker
            # thread below: a prompt blocked on stdin in a thread cannot be
            # cancelled, and would carry on reading after the turn is gone.
            request = toolbox.approval_for(call.name, call.arguments)
            seen_diff = False
            if request is not None:
                try:
                    approved = self.approve(*request)
                except InputUnavailableError as exc:
                    # Nothing can be authorised from here, and answering "no"
                    # to each remaining call in turn would only fill the
                    # transcript with refusals the user never made.
                    self.renderer.error(f"cannot ask for approval: {exc}")
                    self._record(call, "The user could not be asked, so this did not run.")
                    return False
                if not approved:
                    self.renderer.tool_error("declined by the user")
                    self._record(call, DECLINED)
                    continue
                # The user has just read this edit in the prompt. Printing the
                # same diff again the moment they approve it is noise, and
                # noise is what stops approvals being read.
                seen_diff = not self.auto_approve and "\n" in request[1]

            # In a thread so that a long command - a test suite, a build - does
            # not hold the event loop, which is what makes Ctrl-C during one
            # do nothing at all. The spinner is the other half of that: the
            # loop stays responsive, and now it also looks it.
            async with self.renderer.working(_working_label(call.name)):
                outcome = await asyncio.to_thread(
                    toolbox.invoke, call.name, call.arguments, approved=True
                )
            self._report(call, outcome, repeat_diff=not seen_diff)
            escalation, give_up = self._repetition(call, outcome)
            self._record(call, outcome.content + escalation)
            if give_up:
                self.renderer.warn(
                    f"{call.name} failed the same way {_REPEAT_ABORT} times running - "
                    "stopping so you can take a look."
                )
                return False
        return True

    def _record(self, call: ToolCall, content: str) -> None:
        self._say({"role": "tool", "tool_call_id": call.id, "content": content})

    def _report(self, call: ToolCall, outcome: ToolOutcome, *, repeat_diff: bool = True) -> None:
        if outcome.is_error:
            # The whole message. The renderer decides how much of it fits;
            # taking the first line here threw away every part that said what
            # to do about it, including the repeat warning.
            self.renderer.tool_error(outcome.content)
            return
        if outcome.diff and repeat_diff:
            self.renderer.diff(outcome.diff)
        elif call.name == "run":
            for line in outcome.content.splitlines()[:20]:
                self.renderer.note(line)
        # Shown as well as sent, so the user sees what the model was told to fix
        # rather than only the retry it prompts.
        for line in (outcome.findings or "").splitlines():
            self.renderer.tool_error(line)
        # A checker that turned out not to run is the user's problem to fix,
        # not the model's to be told about.
        while len(self._reported_broken) < len(self.checks.broken):
            self.renderer.warn(self.checks.broken[len(self._reported_broken)])
            self._reported_broken.append(True)

    # --- cancellation -----------------------------------------------------

    def repair_after_cancel(self) -> int:
        """Answer any tool call the cancelled turn left open.

        A cancel can land between the assistant message that asked for N tool
        calls and the N results answering it. That transcript is not merely
        untidy - an assistant message carrying ``tool_calls`` must be followed
        by a result for every ``tool_call_id``, so the *next* request would be
        rejected outright. Keeping the history and then being unable to use it
        is the worst of both, hence closing the gap here.

        Idempotent, and returns how many results it had to invent.
        """
        open_calls = self._unanswered_tool_calls()
        for call_id in open_calls:
            self._say(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    # Written for the model: it should know the tool did not
                    # run, rather than assume an empty result means no output.
                    "content": "The user interrupted this turn before the tool ran.",
                }
            )
        return len(open_calls)

    def _unanswered_tool_calls(self) -> list[str]:
        """Ids requested by the most recent assistant turn but never answered."""
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls") or []
            if not calls:
                return []
            answered = {
                later.get("tool_call_id")
                for later in self.messages[index + 1 :]
                if later.get("role") == "tool"
            }
            return [
                str(call["id"])
                for call in calls
                if isinstance(call, dict) and call.get("id") and call["id"] not in answered
            ]
        return []

    def _finish_turn(self) -> None:
        stat = self.workspace.diff_stat() if self.workspace.is_git_repo else ""
        self.renderer.turn_summary(stat, _estimate_tokens(self.messages), self.context_window)

    # --- context ----------------------------------------------------------

    def _compact_if_needed(self) -> None:
        """Drop the middle of the transcript when it gets close to the window.

        The system prompt sets the rules and the recent turns hold the current
        task; it is the long tail of old file reads in between that can go.

        Preserves:
        - System prompt (first message)
        - Last 6 messages (recent context)
        - Tool results for files that are still being edited (written_since_read)
        - User messages that mention files currently being worked on
        """
        if self.context_window <= 0:
            return
        if _estimate_tokens(self.messages) < self.context_window * _COMPACT_AT:
            return
        if len(self.messages) <= 8:
            return

        head = self.messages[:1]
        tail_count = 6

        # Identify files currently being edited (written but not re-read)
        files_being_edited = {
            self.workspace.relative(p) for p in self.workspace.written_since_read
        }

        # Build tail, preserving tool results for files being edited
        tail: list[dict[str, Any]] = []
        for msg in reversed(self.messages[-tail_count:]):
            # Keep tool results for files being edited even if they're older
            if msg.get("role") == "tool" and files_being_edited:
                tool_id = msg.get("tool_call_id")
                # Check if this tool call was for a file being edited
                for prev_msg in self.messages:
                    if prev_msg.get("role") == "assistant":
                        for call in prev_msg.get("tool_calls") or []:
                            if call.get("id") == tool_id:
                                func = call.get("function", {})
                                if func.get("name") in {"read_file", "edit_file", "write_file"}:
                                    file_path = func.get("arguments", {}).get("path", "")
                                    if file_path in files_being_edited:
                                        tail.insert(0, msg)
                                        break

            tail.insert(0, msg)

        # Ensure tool results have their assistant message
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]

        dropped = len(self.messages) - len(head) - len(tail)
        if dropped <= 0:
            return

        # Build a summary of what was dropped, mentioning files that were read
        dropped_files: list[str] = []
        for msg in self.messages[1 : len(self.messages) - len(tail)]:
            if msg.get("role") == "tool":
                tool_id = msg.get("tool_call_id")
                for prev_msg in self.messages:
                    if prev_msg.get("role") == "assistant":
                        for call in prev_msg.get("tool_calls") or []:
                            if call.get("id") == tool_id:
                                func = call.get("function", {})
                                if func.get("name") == "read_file":
                                    file_path = func.get("arguments", {}).get("path", "")
                                    if file_path and file_path not in files_being_edited:
                                        dropped_files.append(file_path)

        summary_parts = [
            f"[{dropped} earlier messages were dropped to stay within the context window."
        ]
        if dropped_files:
            unique_files = list(dict.fromkeys(dropped_files))[:5]  # Limit to 5 files
            summary_parts.append(f" Previously read: {', '.join(unique_files)}")
            if len(dropped_files) > 5:
                summary_parts.append(f" and {len(dropped_files) - 5} more.")
            else:
                summary_parts.append(".")
        summary_parts.append(" Re-read any file you need rather than relying on memory.]")

        self.messages = [
            *head,
            {"role": "user", "content": "".join(summary_parts)},
            *tail,
        ]
        self.renderer.note(f"compacted transcript ({dropped} messages dropped)")
