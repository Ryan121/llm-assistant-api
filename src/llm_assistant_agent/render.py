"""Terminal output.

Isolated in one module so the loop never prints, which keeps the loop testable
and means a future VS Code front end can drive the same session code with a
different renderer.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import shutil
import sys
import time
from types import TracebackType

__all__ = ["InputUnavailableError", "Progress", "Renderer"]


class InputUnavailableError(Exception):
    """There is no one to ask.

    Raised rather than returning "no", because a refusal nobody made is worse
    than a stop: it reads as a decision in the transcript, the model apologises
    and tries something else, and every later approval refuses the same way.
    """


#: How much of a diff to show above an approval prompt. Enough to judge a
#: normal edit whole; short enough that the question stays on screen.
_MAX_APPROVAL_LINES = 40

#: How much of a failed tool call to show. The guidance in these runs to about
#: eight lines; past this it is a stack trace or a truncated diff.
_MAX_ERROR_LINES = 12

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
#: Fast enough to read as motion, slow enough not to burn a core drawing it.
_SPINNER_INTERVAL = 0.08
#: A wait shorter than this needs no reassurance, and a spinner that flashes up
#: for one frame between tool calls is worse than none.
_SPINNER_DELAY = 0.4
#: Past this, say how long it has been. "Waiting" and "hung" look identical
#: without a number, which is the thing that makes people reach for Ctrl-C.
_ELAPSED_AFTER = 3.0


def _looks_like_diff(lines: list[str]) -> bool:
    """Should this detail be coloured as a patch?

    Asked rather than assumed, because the same prompt shows shell commands,
    where a leading ``-`` is a flag and colouring it as a deletion would be
    actively misleading.
    """
    return any(line.startswith("@@") for line in lines)


class Progress:
    """A spinner on its own line, for the stretches where nothing is printed.

    Waiting on a model is the long part of a turn and, until the first token
    arrives, it is indistinguishable from a hang. Prefill on a full context
    window is tens of seconds, and a test suite behind ``run`` can be minutes.

    Whoever is about to print stops it first, so the animation never
    interleaves with real output: :meth:`stop` erases the line there and then
    rather than asking the animating task to do it later. That is safe because
    both run on the one event-loop thread - the task can only take control at
    an ``await``, so it cannot be halfway through a write when ``stop`` runs.

    Idempotent, and a no-op when stdout is not a terminal: escape sequences in
    a log file or a captured test stream are noise at best.
    """

    def __init__(self, renderer: Renderer, label: str) -> None:
        self._renderer = renderer
        self._label = label
        self._task: asyncio.Task[None] | None = None
        self._drawn = False
        self._started = 0.0

    def start(self) -> None:
        if self._task is not None or not self._renderer.animate:
            return
        self._started = time.monotonic()
        self._task = asyncio.ensure_future(self._spin())

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        self._erase()

    async def _spin(self) -> None:
        # Nothing is cleaned up on the way out: the stop() that cancels this
        # has already erased the line, and erasing again from here would clear
        # whatever it has printed in the meantime.
        await asyncio.sleep(_SPINNER_DELAY)
        for tick in itertools.count():
            self._draw(_SPINNER[tick % len(_SPINNER)])
            await asyncio.sleep(_SPINNER_INTERVAL)

    def _draw(self, frame: str) -> None:
        renderer = self._renderer
        elapsed = time.monotonic() - self._started
        since = f" {elapsed:.0f}s" if elapsed >= _ELAPSED_AFTER else ""
        renderer._write(
            f"\r\033[2K  {renderer.blue}{frame}{renderer.reset} "
            f"{renderer.dim}{self._label}{since}{renderer.reset}"
        )
        self._drawn = True

    def _erase(self) -> None:
        if self._drawn:
            self._renderer._write("\r\033[2K")
            self._drawn = False

    async def __aenter__(self) -> Progress:
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Enough on its own: the task is parked in ``asyncio.sleep`` between
        # frames, so cancelling it means that sleep raises and the loop unwinds
        # without ever reaching the next _draw.
        self.stop()


class Renderer:
    """Writes the session to a terminal, with colour when one is attached."""

    def __init__(self, *, colour: bool | None = None, show_diffs: bool = True) -> None:
        enabled = sys.stdout.isatty() if colour is None else colour
        self.reset = "\033[0m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.red = "\033[31m" if enabled else ""
        self.green = "\033[32m" if enabled else ""
        self.yellow = "\033[33m" if enabled else ""
        self.blue = "\033[34m" if enabled else ""
        self.show_diffs = show_diffs
        # Independent of colour: --no-colour is about readability, not about
        # wanting the terminal to look frozen. Both need a real terminal - a
        # redraw written to a pipe is just control characters in a log.
        self.animate = sys.stdout.isatty()

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def working(self, label: str) -> Progress:
        """A spinner for a stretch with no output. Use it as ``async with``."""
        return Progress(self, label)

    # --- streaming --------------------------------------------------------

    def text_delta(self, chunk: str) -> None:
        """Model prose, as it arrives."""
        self._write(chunk)

    def end_text(self) -> None:
        self._write("\n")

    # --- structure --------------------------------------------------------

    def prompt(self) -> str:
        return input(f"{self.blue}›{self.reset} ")

    def tool_call(self, name: str, summary: str) -> None:
        self._write(f"{self.blue}⏺{self.reset} {self.bold}{name}{self.reset}  {summary}\n")

    def tool_error(self, message: str) -> None:
        """A failed tool call, in full.

        The whole message, not its first line. These messages are written to
        say what to do differently, and that part is never on the first line:
        the user watching the session was shown "old_string was not found in
        the file. It matches the file from line 1917 for 61 lines, then differs
        only in indentation, on line 1978:" and then nothing - the colon
        promising an explanation that had been cut off. The lines after it said
        which indentation, how long the anchor was, and that the same call had
        already failed twice. Truncated to keep a pathological error from
        scrolling the session away.
        """
        lines = message.splitlines() or [""]
        self._write(f"  {self.red}✗{self.reset} {lines[0]}\n")
        for line in lines[1:_MAX_ERROR_LINES]:
            self._write(f"    {self.dim}{line.strip()}{self.reset}\n")
        if len(lines) > _MAX_ERROR_LINES:
            self._write(
                f"    {self.dim}... {len(lines) - _MAX_ERROR_LINES} more lines{self.reset}\n"
            )

    def note(self, message: str) -> None:
        self._write(f"  {self.dim}{message}{self.reset}\n")

    def warn(self, message: str) -> None:
        self._write(f"{self.yellow}!{self.reset} {message}\n")

    def error(self, message: str) -> None:
        self._write(f"{self.red}error:{self.reset} {message}\n")

    def rule(self) -> None:
        width = min(shutil.get_terminal_size((80, 20)).columns, 80)
        self._write(f"{self.dim}{'─' * width}{self.reset}\n")

    # --- diffs ------------------------------------------------------------

    def diff(self, text: str) -> None:
        """Colourised unified diff, computed from the file, not from the model."""
        if not self.show_diffs or not text:
            return
        for line in text.splitlines():
            self._write(f"  {self._diff_line(line)}\n")

    def _diff_line(self, line: str) -> str:
        if line.startswith(("+++", "---")):
            return f"{self.dim}{line}{self.reset}"
        if line.startswith("+"):
            return f"{self.green}{line}{self.reset}"
        if line.startswith("-"):
            return f"{self.red}{line}{self.reset}"
        if line.startswith("@@"):
            return f"{self.blue}{line}{self.reset}"
        return line

    # --- session footer ---------------------------------------------------

    def turn_summary(self, diff_stat: str, used_tokens: int, budget: int) -> None:
        if diff_stat:
            self._write("\n")
            for line in diff_stat.splitlines():
                self._write(f"  {self.dim}{line.strip()}{self.reset}\n")
        if budget > 0:
            share = used_tokens / budget
            colour = self.yellow if share > 0.75 else self.dim
            self._write(f"  {colour}ctx ~{used_tokens // 1000}k / {budget // 1000}k{self.reset}\n")

    def approval(self, description: str, detail: str) -> bool:
        """Ask before doing the one thing git cannot undo."""
        self._write(f"\n{self.yellow}?{self.reset} {self.bold}{description}{self.reset}\n")
        lines = detail.splitlines()
        # A long diff scrolls the question off the screen, and a question you
        # cannot see is one you answer by reflex. The whole patch is available
        # afterwards from git_diff; what is needed here is enough to judge.
        shown = lines[:_MAX_APPROVAL_LINES]
        colourise = _looks_like_diff(lines)
        for line in shown:
            self._write(f"    {self._diff_line(line) if colourise else line}\n")
        if len(lines) > len(shown):
            self._write(f"    {self.dim}... {len(lines) - len(shown)} more lines{self.reset}\n")
        self._discard_pending_input()
        try:
            answer = input("  Proceed? [y/N] ").strip().lower()
        except EOFError as exc:
            raise InputUnavailableError("stdin is closed, so nothing can be approved") from exc
        return answer in {"y", "yes"}

    def _discard_pending_input(self) -> None:
        """Throw away anything typed before the question was on screen.

        A turn is mostly waiting, and people type into the gap: a stray Enter,
        the first characters of the next instruction. Those keystrokes sit in
        the terminal's buffer, and the very next ``input`` reads them. A bare
        Enter reads as "no", so an approval the user never saw is refused and
        the transcript records that they declined it - which is what happened
        in a real session, seven calls in a row including ``wc -l``.

        Only what was typed *before* the prompt is dropped. Anything typed
        after it is a real answer to a question that was actually visible.
        """
        if not sys.stdin.isatty():
            return
        with contextlib.suppress(Exception):  # no termios on Windows; not fatal
            import termios

            termios.tcflush(sys.stdin, termios.TCIFLUSH)
