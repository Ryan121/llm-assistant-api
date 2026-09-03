"""Terminal output.

Isolated in one module so the loop never prints, which keeps the loop testable
and means a future VS Code front end can drive the same session code with a
different renderer.
"""

from __future__ import annotations

import shutil
import sys

__all__ = ["Renderer"]


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

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

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
        self._write(f"  {self.red}✗{self.reset} {message}\n")

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
            if line.startswith(("+++", "---")):
                self._write(f"  {self.dim}{line}{self.reset}\n")
            elif line.startswith("+"):
                self._write(f"  {self.green}{line}{self.reset}\n")
            elif line.startswith("-"):
                self._write(f"  {self.red}{line}{self.reset}\n")
            elif line.startswith("@@"):
                self._write(f"  {self.blue}{line}{self.reset}\n")
            else:
                self._write(f"  {line}\n")

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
        for line in detail.splitlines():
            self._write(f"    {line}\n")
        try:
            answer = input("  Proceed? [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}
