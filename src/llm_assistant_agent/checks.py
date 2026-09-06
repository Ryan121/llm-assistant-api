"""Checks run against a file the moment the agent changes it.

The system prompt asks the model to run the tests after editing. It often does
not, and when it does, a whole test run is a blunt instrument for "you left an
unused import behind". So the cheap checks are run for it, on the one file it
just touched, and the findings come back as part of the tool result.

That timing is the whole point. A diagnostic attached to the edit is repaired
inside the same turn, while the model still has the file in front of it; the
same diagnostic surfacing later - from a test run, or from the user - costs a
re-read and several more turns, if it is noticed at all.

Kept to checks that are fast and local. A file-scoped ``ruff`` invocation is
tens of milliseconds; a project-wide ``mypy`` is seconds and needs the whole
tree to be consistent, which mid-refactor it is not. Slow or whole-project
checks belong in the ``run`` tool, where the model asks for them deliberately.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Check", "Checks", "detect_checks"]

#: Diagnostics are terse by nature; past this something is badly wrong and the
#: model does not need every line of it to know that.
_MAX_OUTPUT = 4_000

#: Generous for a file-scoped linter, short enough that a checker which hangs
#: does not take the turn down with it.
_TIMEOUT = 30.0


@dataclass(frozen=True)
class Check:
    """One external checker.

    ``command`` is a shell template containing ``{path}``, which is substituted
    with the changed file's path, shell-quoted. An empty ``suffixes`` runs it
    against every file.
    """

    command: str
    suffixes: tuple[str, ...] = ()

    def applies_to(self, path: str) -> bool:
        return not self.suffixes or path.endswith(self.suffixes)

    def rendered(self, path: str) -> str:
        return self.command.replace("{path}", shlex.quote(path))


@dataclass
class Checks:
    """The checks configured for one session."""

    checks: list[Check] = field(default_factory=list)
    timeout: float = _TIMEOUT

    def run(self, root: Path, path: str) -> str:
        """Problems found in ``path``, or an empty string if it looks fine."""
        # A file that does not parse makes every other checker report the same
        # thing in its own dialect, so it is reported alone.
        syntax = _python_syntax_error(root, path)
        if syntax:
            return syntax

        findings: list[str] = []
        for check in self.checks:
            if not check.applies_to(path):
                continue
            finding = self._run_one(check, root, path)
            if finding:
                findings.append(finding)
        return "\n".join(findings)

    def _run_one(self, check: Check, root: Path, path: str) -> str:
        try:
            result = subprocess.run(  # noqa: S602
                check.rendered(path),
                shell=True,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"({check.command.split()[0]} timed out after {self.timeout:.0f}s)"
        except OSError as exc:  # pragma: no cover - the shell itself failing
            return f"({check.command.split()[0]} could not run: {exc})"

        if result.returncode == 0:
            return ""
        # A non-zero exit with nothing to say is a checker that does not
        # understand the file, not a problem with the file.
        output = (result.stdout + result.stderr).strip()
        return _truncate(output) if output else ""


def _python_syntax_error(root: Path, path: str) -> str:
    """The one check that needs no tooling installed, and matters most."""
    if not path.endswith(".py"):
        return ""
    try:
        source = (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    try:
        compile(source, path, "exec")
    except SyntaxError as exc:
        where = f"{path}:{exc.lineno}" if exc.lineno else path
        return f"{where}: SyntaxError: {exc.msg}"
    except ValueError as exc:  # null bytes, and similar
        return f"{path}: {exc}"
    return ""


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + "\n... more diagnostics not shown."


def detect_checks() -> list[Check]:
    """What is worth running here, based on what is actually installed.

    Detection rather than configuration so the feature is on by default: a
    check the user has to switch on is a check that stays off.
    """
    found: list[Check] = []
    if shutil.which("ruff"):
        # --force-exclude so a path the project excludes stays excluded even
        # though it is being named explicitly.
        found.append(
            Check("ruff check --no-cache --force-exclude --output-format=concise {path}", (".py",))
        )
    return found
