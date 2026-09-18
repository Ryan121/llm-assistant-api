"""Checks run against a file the agent just changed.

The value is in the timing: a diagnostic attached to the edit is fixed inside
the same turn, where the same finding arriving later costs a re-read and a
retry. So what matters here is that findings reach the *tool result*, and that
a checker which is missing, silent or slow never takes the turn down with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from llm_assistant_agent.checks import Check, Checks, detect_checks
from llm_assistant_agent.tools import ToolBox
from llm_assistant_agent.workspace import Workspace

#: A checker that always complains, without depending on anything installed.
#: It names the file, the way a real diagnostic does.
_NOISY = Check(
    f'{sys.executable} -c "import sys; print(sys.argv[1] + \\": made up problem\\"); '
    'sys.exit(1)" {path}'
)
_SILENT_FAILURE = Check(f'{sys.executable} -c "import sys; sys.exit(1)"')

#: A checker that is on PATH but cannot run - a pyenv shim for a version that
#: does not have the tool installed. Fails loudly, says nothing about the file.
_BROKEN = Check(
    f'{sys.executable} -c "import sys; print(\\"pyenv: ruff: command not found\\"); sys.exit(1)"'
)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path)


# --- the built-in syntax check ---------------------------------------------


def test_a_file_that_does_not_parse_is_reported_with_no_tooling(workspace: Workspace) -> None:
    (workspace.root / "broken.py").write_text("def f(\n    return 1\n", encoding="utf-8")

    findings = Checks().run(workspace.root, "broken.py")

    assert "SyntaxError" in findings
    assert "broken.py:1" in findings


def test_a_file_that_parses_reports_nothing(workspace: Workspace) -> None:
    (workspace.root / "fine.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    assert Checks().run(workspace.root, "fine.py") == ""


def test_a_syntax_error_suppresses_the_other_checkers(workspace: Workspace) -> None:
    """Every linter reports the same unparseable file in its own dialect."""
    (workspace.root / "broken.py").write_text("def f(\n", encoding="utf-8")

    findings = Checks([_NOISY]).run(workspace.root, "broken.py")

    assert "SyntaxError" in findings
    assert "made up problem" not in findings


def test_non_python_files_skip_the_syntax_check(workspace: Workspace) -> None:
    (workspace.root / "notes.md").write_text("def f(\n", encoding="utf-8")

    assert Checks().run(workspace.root, "notes.md") == ""


# --- external checkers -----------------------------------------------------


def test_a_checkers_output_is_returned(workspace: Workspace) -> None:
    (workspace.root / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert "made up problem" in Checks([_NOISY]).run(workspace.root, "a.py")


def test_a_checker_that_cannot_run_is_not_reported_as_a_problem_with_the_file(
    workspace: Workspace,
) -> None:
    """The pyenv-shim case. Told "Problems found in main.py: ruff: command not
    found ... fix these", the model rewrites the file over and over trying to
    fix something that was never in it - 45 times, in the session that found
    this."""
    (workspace.root / "a.py").write_text("x = 1\n", encoding="utf-8")
    checks = Checks([_BROKEN])

    assert checks.run(workspace.root, "a.py") == ""

    # Reported to the user instead, once.
    assert len(checks.broken) == 1
    assert "command not found" in checks.broken[0]


def test_a_broken_checker_is_dropped_rather_than_retried_every_edit(
    workspace: Workspace,
) -> None:
    (workspace.root / "a.py").write_text("x = 1\n", encoding="utf-8")
    checks = Checks([_BROKEN])

    for _ in range(5):
        checks.run(workspace.root, "a.py")

    assert checks.checks == []
    assert len(checks.broken) == 1


def test_a_working_checker_is_kept_when_another_is_broken(workspace: Workspace) -> None:
    (workspace.root / "a.py").write_text("x = 1\n", encoding="utf-8")
    checks = Checks([_BROKEN, _NOISY])

    findings = checks.run(workspace.root, "a.py")

    assert "made up problem" in findings
    assert checks.checks == [_NOISY]


def test_detection_runs_the_tool_rather_than_trusting_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutil.which finds a pyenv shim; the shim is what fails."""
    monkeypatch.setattr("shutil.which", lambda _name: "/Users/x/.pyenv/shims/ruff")

    def shim(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 127, "", "pyenv: ruff: command not found")

    monkeypatch.setattr(subprocess, "run", shim)

    assert detect_checks() == []


def test_detection_keeps_a_tool_that_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/ruff")

    def works(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "ruff 0.16.5", "")

    monkeypatch.setattr(subprocess, "run", works)

    assert [c.command.split()[0] for c in detect_checks()] == ["ruff"]


def test_a_failure_with_nothing_to_say_is_ignored(workspace: Workspace) -> None:
    """A non-zero exit and no output means the checker did not understand the
    file, not that the file is wrong."""
    (workspace.root / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert Checks([_SILENT_FAILURE]).run(workspace.root, "a.py") == ""


def test_a_checker_only_runs_against_the_suffixes_it_declares(workspace: Workspace) -> None:
    (workspace.root / "notes.md").write_text("hello\n", encoding="utf-8")
    python_only = Check(_NOISY.command, (".py",))

    assert Checks([python_only]).run(workspace.root, "notes.md") == ""


def test_a_checker_that_hangs_does_not_hang_the_turn(workspace: Workspace) -> None:
    (workspace.root / "a.py").write_text("x = 1\n", encoding="utf-8")
    sleeper = Check(f'{sys.executable} -c "import time; time.sleep(30)"')

    findings = Checks([sleeper], timeout=0.5).run(workspace.root, "a.py")

    assert "timed out" in findings


def test_the_path_is_quoted_before_it_reaches_the_shell(workspace: Workspace) -> None:
    """The path comes from the model; it is inside the workspace, not trusted."""
    awkward = "a file; touch pwned.py.py"
    (workspace.root / awkward).write_text("x = 1\n", encoding="utf-8")
    echoing = Check(f'{sys.executable} -c "import sys; print(sys.argv[1]); sys.exit(1)" {{path}}')

    findings = Checks([echoing]).run(workspace.root, awkward)

    assert awkward in findings
    assert not (workspace.root / "pwned.py").exists()


# --- reaching the model ----------------------------------------------------


def test_findings_are_attached_to_the_edit_that_caused_them(workspace: Workspace) -> None:
    toolbox = ToolBox(workspace, lambda description, detail: True, Checks([_NOISY]))

    outcome = toolbox.invoke("write_file", {"path": "a.py", "content": "x = 1\n"})

    # Reported, but not an error: the write did land, and telling the model it
    # failed would invite it to apply the same change twice.
    assert not outcome.is_error
    assert "Created a.py." in outcome.content
    assert "made up problem" in outcome.content
    assert outcome.findings is not None


def test_a_clean_edit_says_nothing_extra(workspace: Workspace) -> None:
    toolbox = ToolBox(workspace, lambda description, detail: True, Checks())

    outcome = toolbox.invoke("write_file", {"path": "a.py", "content": "x = 1\n"})

    assert outcome.content == "Created a.py."
    assert outcome.findings is None


def test_edits_are_checked_too_not_just_writes(workspace: Workspace) -> None:
    toolbox = ToolBox(workspace, lambda description, detail: True, Checks([_NOISY]))
    (workspace.root / "a.py").write_text("x = 1\n", encoding="utf-8")
    toolbox.invoke("read_file", {"path": "a.py"})

    outcome = toolbox.invoke(
        "edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}
    )

    assert "made up problem" in outcome.content
