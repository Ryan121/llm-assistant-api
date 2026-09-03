"""The tools the model is given, and what they do.

Kept deliberately small. Every tool costs prompt tokens on *every* turn of an
agent loop and adds another way for the model to go wrong, so the set is the
minimum that can carry out a real code change: look around, read, change, and
check the change.

Each tool returns a string that goes straight back to the model as a tool
result, and a failure is a normal return value rather than an exception. An
agent recovers from "old_string was not found, re-read the file" far better
than from a stack trace.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from .edits import EditError, apply_edit, unified_diff
from .workspace import Workspace, WorkspaceError

__all__ = ["TOOL_SCHEMAS", "ToolBox", "ToolOutcome"]

#: Cap on grep/list output. Past this the model stops reading anyway, and the
#: tokens come out of the context budget the conversation needs.
_MAX_MATCHES = 100
_MAX_LISTED_FILES = 200


@dataclass
class ToolOutcome:
    """What a tool did: text for the model, and a diff for the human."""

    content: str
    #: Set when the tool changed a file, so the CLI can show ground truth.
    diff: str | None = None
    path: str | None = None
    is_error: bool = False


class Approver(Protocol):
    """Asks the user to authorise something git cannot undo."""

    def __call__(self, description: str, detail: str) -> bool: ...


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files in the workspace, optionally filtered by a glob such as "
                "'src/**/*.py'. Use this first to orient yourself in an unfamiliar repo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern relative to the workspace root.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search file contents with a regular expression and return matching "
                "lines with their line numbers. Much cheaper than reading whole files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "glob": {
                        "type": "string",
                        "description": "Restrict the search, e.g. '*.py'. Optional.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": ("Read a UTF-8 text file. You must read a file before you can edit it."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact snippet in a file. old_string must be copied "
                "verbatim from the file, including indentation, and must appear "
                "exactly once unless replace_all is true. Prefer several small "
                "edits over one large one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to replace, with enough context to be unique.",
                    },
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence. Defaults to false.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file, or overwrite one completely. For changing part "
                "of an existing file use edit_file instead - it is cheaper and safer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": (
                "Run a shell command in the workspace and return its output. Use it "
                "to check your work - run the tests, the linter, the type checker. "
                "The user is asked to approve each command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "description": "Default 120."},
                },
                "required": ["command"],
            },
        },
    },
]


class ToolBox:
    """Executes tool calls against one workspace."""

    def __init__(self, workspace: Workspace, approver: Approver) -> None:
        self.workspace = workspace
        self.approver = approver

    def invoke(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        handler = {
            "list_files": self._list_files,
            "grep": self._grep,
            "read_file": self._read_file,
            "edit_file": self._edit_file,
            "write_file": self._write_file,
            "run": self._run,
        }.get(name)

        if handler is None:
            return ToolOutcome(f"Unknown tool {name!r}.", is_error=True)

        try:
            return handler(arguments)
        except WorkspaceError as exc:
            return ToolOutcome(str(exc), is_error=True)
        except EditError as exc:
            return ToolOutcome(str(exc), is_error=True)
        except (OSError, ValueError) as exc:
            return ToolOutcome(f"{type(exc).__name__}: {exc}", is_error=True)

    # --- individual tools -------------------------------------------------

    def _list_files(self, arguments: dict[str, Any]) -> ToolOutcome:
        pattern = arguments.get("pattern")
        paths = [self.workspace.relative(p) for p in self.workspace.walk()]
        if isinstance(pattern, str) and pattern:
            paths = [p for p in paths if fnmatch.fnmatch(p, pattern)]

        if not paths:
            return ToolOutcome("No files matched.")

        shown = paths[:_MAX_LISTED_FILES]
        body = "\n".join(shown)
        if len(paths) > len(shown):
            body += f"\n... and {len(paths) - len(shown)} more. Narrow the pattern."
        return ToolOutcome(body)

    def _grep(self, arguments: dict[str, Any]) -> ToolOutcome:
        raw = arguments.get("pattern")
        if not isinstance(raw, str) or not raw:
            return ToolOutcome("grep needs a 'pattern'.", is_error=True)
        try:
            expression = re.compile(raw)
        except re.error as exc:
            return ToolOutcome(f"Invalid regular expression: {exc}", is_error=True)

        glob = arguments.get("glob")
        matches: list[str] = []

        for path in self.workspace.walk():
            relative = self.workspace.relative(path)
            if isinstance(glob, str) and glob and not fnmatch.fnmatch(relative, glob):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if expression.search(line):
                    matches.append(f"{relative}:{number}: {line.strip()[:200]}")
                    if len(matches) >= _MAX_MATCHES:
                        matches.append(f"... stopped at {_MAX_MATCHES} matches.")
                        return ToolOutcome("\n".join(matches))

        return ToolOutcome("\n".join(matches) if matches else "No matches.")

    def _read_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = str(arguments.get("path", ""))
        content = self.workspace.read(path)
        # Numbered so the model can cite locations back precisely.
        numbered = "\n".join(
            f"{number:>6}\t{line}" for number, line in enumerate(content.splitlines(), start=1)
        )
        return ToolOutcome(numbered or "(empty file)", path=path)

    def _edit_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = str(arguments.get("path", ""))
        target = self.workspace.resolve(path)
        self.workspace.require_seen(target, path)

        before = self.workspace.read(path)
        after = apply_edit(
            before,
            str(arguments.get("old_string", "")),
            str(arguments.get("new_string", "")),
            replace_all=bool(arguments.get("replace_all", False)),
        )
        self.workspace.write(path, after)

        diff = unified_diff(before, after, path)
        # The model gets a confirmation, not the whole file back: it already
        # knows what it asked for, and re-sending the file doubles the context.
        return ToolOutcome(f"Edited {path}.", diff=diff, path=path)

    def _write_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = str(arguments.get("path", ""))
        content = str(arguments.get("content", ""))
        target = self.workspace.resolve(path)
        before = target.read_text(encoding="utf-8") if target.is_file() else ""

        self.workspace.write(path, content)
        verb = "Updated" if before else "Created"
        return ToolOutcome(f"{verb} {path}.", diff=unified_diff(before, content, path), path=path)

    def _run(self, arguments: dict[str, Any]) -> ToolOutcome:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolOutcome("run needs a 'command'.", is_error=True)

        timeout = arguments.get("timeout_seconds")
        seconds = timeout if isinstance(timeout, int) and 0 < timeout <= 900 else 120

        # The one thing in this tool set git cannot undo.
        if not self.approver("Run a shell command", command):
            return ToolOutcome(
                "The user declined to run that command. Ask what they would prefer, "
                "or continue without it.",
                is_error=True,
            )

        try:
            result = subprocess.run(  # noqa: S602
                command,
                shell=True,
                cwd=self.workspace.root,
                capture_output=True,
                text=True,
                timeout=seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolOutcome(f"Command timed out after {seconds}s.", is_error=True)

        output = (result.stdout + result.stderr).strip()
        if len(output) > 20_000:
            output = output[:20_000] + "\n... output truncated."

        return ToolOutcome(
            f"exit {result.returncode}\n{output or '(no output)'}",
            is_error=result.returncode != 0,
        )
