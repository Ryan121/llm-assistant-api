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
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from .checks import Checks
from .documents import SUFFIXES, describe, extract_text
from .edits import EditError, apply_edit, unified_diff
from .sandbox import HostSandbox, Sandbox
from .workspace import Workspace, WorkspaceError

__all__ = ["DECLINED", "TOOL_SCHEMAS", "ToolBox", "ToolOutcome"]

#: Returned as the tool result when the user says no. Phrased for the model:
#: it needs to carry on sensibly, not treat this as a failure to retry. It
#: covers edits as well as commands, so it says neither.
#:
#: The scope is spelled out because a bare refusal gets over-generalised into a
#: rule that was never stated. In a real session the user declined one
#: ``git checkout templates/index.html``; from then on the model believed it had
#: been "specifically told not to modify the HTML template", said so in four
#: separate turns, and refused to fix the frontend bug the user was asking
#: about - through "you make the change" and "whatever you are doing its not
#: woking". One "no" to one call is not a policy.
DECLINED = (
    "The user declined this one call. That is all it means: it is not a standing "
    "rule, and it puts no file or kind of change off limits. Do not infer a "
    "restriction from it. Ask what they would prefer, or continue without it."
)

#: Cap on grep/list output. Past this the model stops reading anyway, and the
#: tokens come out of the context budget the conversation needs.
_MAX_MATCHES = 100
_MAX_LISTED_FILES = 200

#: A whole-repo diff can be enormous. Truncated rather than refused, because
#: the head of a diff is the useful part and the model can narrow from there.
_MAX_DIFF_CHARS = 20_000

#: How many pre-existing dirty files to name before summarising the rest.
_MAX_NAMED_BASELINE = 10

#: How much to read when a range gives a start but no end.
_DEFAULT_WINDOW = 100


#: Commands that throw away uncommitted work. Matched loosely and on purpose:
#: this only changes the wording of a prompt the user was going to see anyway,
#: so a false positive costs nothing and a miss costs the user their changes.
#: `git stash` is included - it is recoverable in principle, and nobody who
#: did not run it themselves knows to look in the stash.
_DESTRUCTIVE_GIT = re.compile(
    r"\bgit\s+(?:checkout\b|restore\b|reset\b(?!\s+--soft\b)|clean\b|stash\b(?!\s+list\b))"
)


def _discards_work(command: str) -> bool:
    return bool(_DESTRUCTIVE_GIT.search(command))


#: The spellings a model reaches for when it wants part of a file, mapped to
#: (first line, last line). ``begin``/``end`` is what the schema advertises;
#: the rest are aliases, and they are here because the alternative is what the
#: sessions actually did. Measured across six of them: 56 of 87 read_file calls
#: asked for a line range, in five different spellings, and every one was
#: silently ignored and answered with the whole file - 16 windows of 10 to 80
#: lines in one session, each returning 109 kB, four consecutive requests for
#: ten lines among them. A tool that quietly does something else is worse than
#: one that refuses.
_RANGE_ALIASES: tuple[tuple[str, str], ...] = (
    ("begin", "end"),
    ("start_line", "end_line"),
    ("start", "end"),
    ("from_line", "to_line"),
)

#: Spellings that give a start and a count rather than two line numbers.
_LENGTH_ALIASES: tuple[tuple[str, str], ...] = (
    ("offset", "length"),
    ("offset", "limit"),
    ("start", "count"),
)

#: Spellings that pass both numbers in one list, e.g. ``{"range": [10, 40]}``.
_PAIR_ALIASES: tuple[str, ...] = ("range", "lines", "line_range")


def _as_line_number(value: Any, field: str) -> int:
    """A line number from whatever the model sent.

    Numbers arrive as JSON strings about as often as integers - every ranged
    read in the sessions sent ``"1917"`` rather than ``1917`` - so a strict
    reading of the type would reject every real call.
    """
    if isinstance(value, bool):
        raise ValueError(f"read_file: {field} must be a line number.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            raise ValueError(f"read_file: {field} must be a line number, not {value!r}.") from None
    else:
        raise ValueError(f"read_file: {field} must be a line number.")
    if number < 1:
        raise ValueError(f"read_file: {field} must be 1 or more; lines count from 1.")
    return number


def _line_window(arguments: dict[str, Any]) -> tuple[int, int] | None:
    """The range of lines asked for, or None for the whole file."""
    for first_key, last_key in _RANGE_ALIASES:
        if arguments.get(first_key) is None and arguments.get(last_key) is None:
            continue
        first = (
            _as_line_number(arguments[first_key], first_key)
            if arguments.get(first_key) is not None
            else 1
        )
        last = (
            _as_line_number(arguments[last_key], last_key)
            if arguments.get(last_key) is not None
            else first + _DEFAULT_WINDOW - 1
        )
        if last < first:
            raise ValueError(f"read_file: {last_key} ({last}) is before {first_key} ({first}).")
        return first, last

    for first_key, count_key in _LENGTH_ALIASES:
        if arguments.get(first_key) is None or arguments.get(count_key) is None:
            continue
        first = _as_line_number(arguments[first_key], first_key)
        count = _as_line_number(arguments[count_key], count_key)
        return first, first + count - 1

    for key in _PAIR_ALIASES:
        pair = arguments.get(key)
        if isinstance(pair, str):
            # Sent as the text of a list: {"range": "[326, 353]"}.
            try:
                pair = json.loads(pair)
            except json.JSONDecodeError:
                raise ValueError(f"read_file: could not read {key}={arguments[key]!r}.") from None
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            first = _as_line_number(pair[0], f"{key}[0]")
            last = _as_line_number(pair[1], f"{key}[1]")
            if last < first:
                raise ValueError(f"read_file: {key} ends ({last}) before it begins ({first}).")
            return first, last

    return None


@dataclass
class ToolOutcome:
    """What a tool did: text for the model, and a diff for the human."""

    content: str
    #: Set when the tool changed a file, so the CLI can show ground truth.
    diff: str | None = None
    path: str | None = None
    is_error: bool = False
    #: What the checkers said about the file, so the CLI can show it too. The
    #: user should see the same problems the model was handed.
    findings: str | None = None


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
            "description": (
                "Read a UTF-8 text file, whole or a range of lines. You must read a "
                "file before you can edit it. On a large file read the range you "
                "actually need - grep for the line number first, then read around it. "
                "Reading a 100 kB file whole to change six lines in the middle of it "
                "fills the context with the other 99 kB."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace."},
                    "begin": {
                        "type": "integer",
                        "description": (
                            "First line to read, counting from 1. Optional; omit to "
                            "start at the top."
                        ),
                    },
                    "end": {
                        "type": "integer",
                        "description": (
                            "Last line to read, inclusive. Optional; omit to read to "
                            "the end of the file."
                        ),
                    },
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
            "name": "read_document",
            "description": (
                "Inspect a PDF, CSV, TSV or spreadsheet; read_file cannot open these. "
                "Returns its structure: pages or sheets, column positions, the header "
                "and sample rows verbatim. Always call this before writing code that "
                "parses the file. full=true returns the text instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace."},
                    "full": {
                        "type": "boolean",
                        "description": "Return the extracted text instead of the structure.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "Every uncommitted change in the working tree, including files you "
                "created. Read-only. Call it before your final answer: it is the only "
                "way to see your edits together rather than one at a time. It is not a "
                "record of your work alone - it also shows the user's own changes and "
                "anything left by an earlier session, so expect more here than you "
                "remember doing. Use summary=true on a large change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Restrict to one file. Optional; omit for everything.",
                    },
                    "summary": {
                        "type": "boolean",
                        "description": "Per-file line counts only, instead of the patch.",
                    },
                },
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


def _known_arguments() -> dict[str, frozenset[str]]:
    """What each tool will actually read, so anything else can be called out."""
    known = {
        schema["function"]["name"]: frozenset(schema["function"]["parameters"]["properties"])
        for schema in TOOL_SCHEMAS
    }
    # read_file understands the range aliases too, and they are not in the
    # schema on purpose - advertising five spellings would invite a sixth.
    spellings = {key for pair in (*_RANGE_ALIASES, *_LENGTH_ALIASES) for key in pair}
    known["read_file"] = known["read_file"] | spellings | set(_PAIR_ALIASES)
    return known


_KNOWN_ARGUMENTS = _known_arguments()


class ToolBox:
    """Executes tool calls against one workspace."""

    def __init__(
        self,
        workspace: Workspace,
        approver: Approver,
        checks: Checks | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.workspace = workspace
        self.approver = approver
        self.checks = checks or Checks()
        self.sandbox = sandbox or HostSandbox()

    def approval_for(self, name: str, arguments: dict[str, Any]) -> tuple[str, str] | None:
        """What the user must authorise before ``name`` runs, if anything.

        Split out from :meth:`invoke` so a caller running tools off the event
        loop can ask *before* handing the work to a worker thread. A prompt
        blocked on stdin inside a thread cannot be cancelled, and would go on
        reading the user's next keystrokes after the turn it belonged to is
        gone.
        """
        if name == "run":
            command = str(arguments.get("command", "")).strip()
            if not command:
                return None
            # The label says where it will run: approving `rm -rf /` in a
            # container is a different decision from approving it on a laptop.
            label = f"Run a shell command {self.sandbox.label}"
            # And, when it applies, what it would cost. git's undo is the whole
            # reason this tool set can be as free as it is with edits, so a
            # command that discards uncommitted work is the one thing here with
            # no undo behind it. A real session asked for
            # `git checkout templates/index.html` over 1326 lines of the user's
            # own changes, under a prompt that read the same as `wc -l`.
            if _discards_work(command):
                label = (
                    f"{label} - this would DISCARD uncommitted changes, which cannot be recovered"
                )
            return (label, command)
        if name in {"edit_file", "write_file"}:
            return self._edit_approval(name, arguments)
        return None

    def _edit_approval(self, name: str, arguments: dict[str, Any]) -> tuple[str, str] | None:
        """The diff an edit would make, for approval before it lands.

        Built from raw reads rather than ``workspace.read``, which records the
        file as seen. Going through it here would satisfy the read-before-edit
        rule on the model's behalf, by way of a prompt the model never had to
        answer, and the whole point of that rule is that an edit is built from
        what the file actually contains.

        A preview that cannot be built - no such file, ``old_string`` does not
        match, not UTF-8 - returns ``None``. There is nothing to show the user
        and nothing to authorise: the call goes on to :meth:`invoke` and fails
        there, with the message that explains why.
        """
        path = str(arguments.get("path", ""))
        if not path:
            return None
        try:
            target = self.workspace.resolve(path)
            before = target.read_text(encoding="utf-8") if target.is_file() else ""
        except (WorkspaceError, OSError, UnicodeDecodeError):
            return None

        if name == "write_file":
            after = str(arguments.get("content", ""))
            verb = "Overwrite" if before else "Create"
        else:
            if target not in self.workspace.seen:
                return None  # invoke() will refuse it, and say so better
            try:
                after = apply_edit(
                    before,
                    str(arguments.get("old_string", "")),
                    str(arguments.get("new_string", "")),
                    replace_all=bool(arguments.get("replace_all", False)),
                )
            except EditError:
                return None
            verb = "Edit"

        # An edit that changes nothing is not worth a keystroke from the user.
        # It still goes through invoke, which reports it as the no-op it is.
        diff = unified_diff(before, after, path)
        if not diff:
            return None
        return f"{verb} {path}", diff

    def invoke(
        self, name: str, arguments: dict[str, Any], *, approved: bool = False
    ) -> ToolOutcome:
        """Run one tool. ``approved`` means the caller already asked the user."""
        # Only when it is actually going to be used: building an edit's request
        # reads the file and applies the edit to decide what to show, and the
        # normal path through Session has already done exactly that.
        if not approved:
            request = self.approval_for(name, arguments)
            if request is not None and not self.approver(*request):
                return ToolOutcome(DECLINED, is_error=True)

        handler = {
            "list_files": self._list_files,
            "grep": self._grep,
            "read_file": self._read_file,
            "read_document": self._read_document,
            "edit_file": self._edit_file,
            "write_file": self._write_file,
            "git_diff": self._git_diff,
            "run": self._run,
        }.get(name)

        if handler is None:
            return ToolOutcome(f"Unknown tool {name!r}.", is_error=True)

        try:
            return self._noting_unknown(name, arguments, handler(arguments))
        except WorkspaceError as exc:
            return ToolOutcome(str(exc), is_error=True)
        except EditError as exc:
            return ToolOutcome(str(exc), is_error=True)
        except (OSError, ValueError) as exc:
            return ToolOutcome(f"{type(exc).__name__}: {exc}", is_error=True)

    def _noting_unknown(
        self, name: str, arguments: dict[str, Any], outcome: ToolOutcome
    ) -> ToolOutcome:
        """Say when part of the call was not understood.

        The general form of the worst bug in this tool set: an argument the
        tool does not know is dropped, the tool does something else instead,
        and the result looks like a successful answer to the question that was
        asked. ``read_file`` ignoring a line range this way cost six sessions -
        the model asked for ten lines, got 109 kB, and asked again.

        A note rather than an error, because the work the tool did do is still
        worth having, and refusing the call outright would lose it.
        """
        unknown = sorted(set(arguments) - _KNOWN_ARGUMENTS.get(name, frozenset()))
        if not unknown:
            return outcome
        known = ", ".join(sorted(_KNOWN_ARGUMENTS.get(name, frozenset()))) or "nothing"
        outcome.content += (
            f"\n\nNote: {name} does not take {', '.join(unknown)}, so "
            f"{'it was' if len(unknown) == 1 else 'they were'} ignored - what came "
            f"back does not reflect {'it' if len(unknown) == 1 else 'them'}. "
            f"{name} takes: {known}."
        )
        return outcome

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

    def _read_document(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = str(arguments.get("path", ""))
        target = self.workspace.resolve(path)
        if not target.is_file():
            return ToolOutcome(f"{path} does not exist or is not a file.", is_error=True)
        # Deliberately not marked as seen: a digest is not the file's contents,
        # and an edit_file built from one would be built on a summary.
        if arguments.get("full"):
            return ToolOutcome(extract_text(target), path=path)
        return ToolOutcome(describe(target), path=path)

    def _read_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = str(arguments.get("path", ""))
        # The model's instinct on any path is read_file, so the redirect lives
        # here rather than relying on it to notice the other tool.
        if path.lower().endswith(SUFFIXES):
            return ToolOutcome(
                f"{path} is not a text file. Use read_document to see its structure.",
                is_error=True,
            )

        try:
            window = _line_window(arguments)
        except ValueError as exc:
            return ToolOutcome(str(exc), is_error=True)

        content = self.workspace.read(path, partial=window is not None)
        lines = content.splitlines()
        if not lines:
            return ToolOutcome("(empty file)", path=path)

        first, last = 1, len(lines)
        if window is not None:
            first, last = window
            if first > len(lines):
                return ToolOutcome(
                    f"{path} has only {len(lines)} lines, so line {first} is past the end.",
                    is_error=True,
                )
            last = min(last, len(lines))

        # Numbered so the model can cite locations back precisely, and so a
        # ranged read's numbers still refer to the file rather than the window.
        numbered = "\n".join(
            f"{number:>6}\t{lines[number - 1]}" for number in range(first, last + 1)
        )
        if window is None:
            return ToolOutcome(numbered, path=path)
        return ToolOutcome(
            f"{path}, lines {first}-{last} of {len(lines)}:\n{numbered}",
            path=path,
        )

    def _edit_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = str(arguments.get("path", ""))
        target = self.workspace.resolve(path)
        self.workspace.require_seen(target, path)

        # Pre-validation: check that old_string is provided and non-empty
        old_string = arguments.get("old_string", "")
        if not old_string:
            return ToolOutcome(
                f"edit_file: old_string is required and must be non-empty. "
                f"Read {path} first to see its current content.",
                is_error=True,
            )

        # Asked before the read, which clears it: what matters is whether the
        # model had re-read the file before proposing this anchor.
        stale = target in self.workspace.written_since_read
        before = self.workspace.read(path)

        # Pre-validation: check if old_string exists in the file
        if old_string not in before:
            # Provide helpful suggestion
            suggestion = "Use grep to find the current content, then re-read the file."
            if len(old_string) > 100:
                suggestion = (
                    "The old_string is long - ensure it matches exactly, including "
                    "indentation and whitespace. Consider a smaller edit."
                )
            return ToolOutcome(
                f"edit_file: old_string was not found in {path}. {suggestion}",
                is_error=True,
            )

        after = apply_edit(
            before,
            str(old_string),
            str(arguments.get("new_string", "")),
            replace_all=bool(arguments.get("replace_all", False)),
            written_since_read=stale,
        )
        self.workspace.write(path, after)

        diff = unified_diff(before, after, path)
        # The model gets a confirmation, not the whole file back: it already
        # knows what it asked for, and re-sending the file doubles the context.
        return self._checked(f"Edited {path}.", path, diff=diff)

    def _write_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = str(arguments.get("path", ""))
        content = str(arguments.get("content", ""))
        target = self.workspace.resolve(path)
        before = target.read_text(encoding="utf-8") if target.is_file() else ""

        self.workspace.write(path, content)
        verb = "Updated" if before else "Created"
        return self._checked(f"{verb} {path}.", path, diff=unified_diff(before, content, path))

    def _checked(self, confirmation: str, path: str, *, diff: str) -> ToolOutcome:
        """Report the write, plus anything the checkers found in it.

        Reported as a problem rather than an error: the edit did land, and
        telling the model it failed would invite it to apply the same change
        twice.
        """
        findings = self.checks.run(self.workspace.root, path)
        if not findings:
            return ToolOutcome(confirmation, diff=diff, path=path)
        return ToolOutcome(
            f"{confirmation}\n\nProblems found in {path} afterwards:\n{findings}\n"
            "Fix these before moving on.",
            diff=diff,
            path=path,
            findings=findings,
        )

    def _git_diff(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = arguments.get("path")
        only = str(path) if isinstance(path, str) and path else None
        text = self.workspace.diff(
            only,
            stat=bool(arguments.get("summary", False)),
        )
        if not text:
            return ToolOutcome("No uncommitted changes.")
        if len(text) > _MAX_DIFF_CHARS:
            text = (
                text[:_MAX_DIFF_CHARS] + f"\n... diff truncated at {_MAX_DIFF_CHARS} characters. "
                "Call again with summary=true, or with a single path."
            )
        return ToolOutcome(text + self._provenance(only))

    def _provenance(self, only: str | None) -> str:
        """Name the part of the diff that is not the agent's work.

        A diff is a picture of the working tree, not a log of what this session
        did, and the model reads it as the latter. Told nothing, it treats
        changes it does not remember as its own mistakes: in a real session it
        found 1326 changed lines in a template left by the session before,
        called them "not part of my intended changes", and spent three calls
        trying to git checkout the user's work - one of which the user had to
        decline. Saying whose they are costs a line and removes the inference.

        Only files this session has *not* written are named. A file the agent
        has edited carries both its work and whatever was there before, so
        warning it off one it is in the middle of changing would buy the
        opposite failure - and in a repo where everything is uncommitted, which
        is the normal state of the project this came from, an unfiltered note
        names every file it was asked to work on.
        """
        untouched = sorted(
            relative for relative in self.workspace.baseline_dirty if not self._is_ours(relative)
        )
        if only is not None:
            try:
                relative = self.workspace.relative(self.workspace.resolve(only))
            except (WorkspaceError, OSError):
                return ""
            untouched = [relative] if relative in untouched else []
        if not untouched:
            return ""

        shown = ", ".join(untouched[:_MAX_NAMED_BASELINE])
        more = (
            ""
            if len(untouched) <= _MAX_NAMED_BASELINE
            else f", and {len(untouched) - _MAX_NAMED_BASELINE} more"
        )
        return (
            f"\n\nNote: {shown}{more} already had uncommitted changes when this session "
            "started, and you have not edited them since. That part of the diff is the "
            "user's work or an earlier session's, not a mistake of yours - do not "
            "revert it."
        )

    def _is_ours(self, relative: str) -> bool:
        """Has this session written this file?"""
        try:
            return self.workspace.resolve(relative) in self.workspace.written
        except (WorkspaceError, OSError):
            return False

    def _run(self, arguments: dict[str, Any]) -> ToolOutcome:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolOutcome("run needs a 'command'.", is_error=True)

        timeout = arguments.get("timeout_seconds")
        seconds = timeout if isinstance(timeout, int) and 0 < timeout <= 900 else 120

        # Approval - the one thing in this tool set git cannot undo - has
        # already happened in invoke(), or in the caller that passed approved.
        execution = self.sandbox.prepare(command, self.workspace.root)
        try:
            result = subprocess.run(  # noqa: S602, S603
                execution.argv,
                shell=execution.shell,
                cwd=execution.cwd,
                capture_output=True,
                text=True,
                timeout=seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolOutcome(
                f"Command timed out after {seconds}s. "
                f"Consider increasing timeout_seconds (max 900) or running a narrower command.",
                is_error=True,
            )
        except OSError as exc:
            hint = ""
            if "No such file" in str(exc):
                hint = " Check that the command and all its arguments are correct."
            elif "Permission" in str(exc):
                hint = " The command may not be executable or accessible."
            return ToolOutcome(f"Could not run the command: {exc}.{hint}", is_error=True)

        output = (result.stdout + result.stderr).strip()
        if len(output) > 20_000:
            output = output[:20_000] + "\n... output truncated."

        return ToolOutcome(
            f"exit {result.returncode}\n{output or '(no output)'}",
            is_error=result.returncode != 0,
        )
