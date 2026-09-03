"""Workspace guards and the tool implementations.

The security-relevant assertions are the escape test and the read-before-edit
rule; the rest is about returning errors the model can act on rather than
raising exceptions that abort the turn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_assistant_agent.tools import ToolBox
from llm_assistant_agent.workspace import Workspace, WorkspaceError


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("noise\n", encoding="utf-8")
    return Workspace.open(tmp_path)


@pytest.fixture
def toolbox(workspace: Workspace) -> ToolBox:
    return ToolBox(workspace, approver=lambda description, detail: True)


def _deny(description: str, detail: str) -> bool:
    return False


# --- workspace containment -------------------------------------------------


def test_paths_outside_the_workspace_are_refused(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="outside the workspace"):
        workspace.resolve("../../etc/passwd")


def test_absolute_paths_outside_the_workspace_are_refused(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="outside the workspace"):
        workspace.resolve("/etc/passwd")


def test_paths_inside_the_workspace_are_allowed(workspace: Workspace) -> None:
    assert workspace.resolve("src/app.py").name == "app.py"


def test_walk_skips_noise_directories(workspace: Workspace) -> None:
    found = {workspace.relative(p) for p in workspace.walk()}

    assert "src/app.py" in found
    assert not any(p.startswith("node_modules") for p in found)


def test_reading_a_missing_file_is_an_error(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="does not exist"):
        workspace.read("nope.py")


def test_binary_files_are_refused(workspace: Workspace) -> None:
    (workspace.root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")

    with pytest.raises(WorkspaceError, match="not UTF-8"):
        workspace.read("blob.bin")


def test_oversized_files_are_refused(workspace: Workspace) -> None:
    (workspace.root / "huge.txt").write_text("x" * 600_000, encoding="utf-8")

    with pytest.raises(WorkspaceError, match="grep"):
        workspace.read("huge.txt")


# --- read before edit ------------------------------------------------------


def test_editing_an_unread_file_is_refused(toolbox: ToolBox) -> None:
    """Stops an edit built from a hallucinated recollection of the file."""
    outcome = toolbox.invoke(
        "edit_file",
        {"path": "src/app.py", "old_string": "return 1", "new_string": "return 2"},
    )

    assert outcome.is_error
    assert "has not been read" in outcome.content


def test_editing_after_reading_succeeds(toolbox: ToolBox) -> None:
    toolbox.invoke("read_file", {"path": "src/app.py"})

    outcome = toolbox.invoke(
        "edit_file",
        {"path": "src/app.py", "old_string": "return 1", "new_string": "return 2"},
    )

    assert not outcome.is_error
    assert outcome.diff is not None
    assert (toolbox.workspace.root / "src" / "app.py").read_text() == "def main():\n    return 2\n"


def test_a_failed_edit_leaves_the_file_untouched(toolbox: ToolBox) -> None:
    toolbox.invoke("read_file", {"path": "src/app.py"})
    original = (toolbox.workspace.root / "src" / "app.py").read_text()

    outcome = toolbox.invoke(
        "edit_file",
        {"path": "src/app.py", "old_string": "not present", "new_string": "x"},
    )

    assert outcome.is_error
    assert (toolbox.workspace.root / "src" / "app.py").read_text() == original


# --- individual tools ------------------------------------------------------


def test_read_file_returns_numbered_lines(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("read_file", {"path": "src/app.py"})

    assert "1\tdef main():" in outcome.content


def test_grep_reports_paths_and_line_numbers(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("grep", {"pattern": r"def \w+"})

    assert "src/app.py:1:" in outcome.content


def test_grep_respects_a_glob(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("grep", {"pattern": "demo", "glob": "*.py"})

    assert outcome.content == "No matches."


def test_grep_rejects_a_bad_regex_without_raising(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("grep", {"pattern": "("})

    assert outcome.is_error
    assert "Invalid regular expression" in outcome.content


def test_list_files_filters_by_glob(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("list_files", {"pattern": "src/*.py"})

    assert outcome.content == "src/app.py"


def test_write_file_creates_parent_directories(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("write_file", {"path": "a/b/c.py", "content": "x = 1\n"})

    assert not outcome.is_error
    assert (toolbox.workspace.root / "a" / "b" / "c.py").read_text() == "x = 1\n"


def test_written_files_can_be_edited_without_a_second_read(toolbox: ToolBox) -> None:
    toolbox.invoke("write_file", {"path": "fresh.py", "content": "x = 1\n"})

    outcome = toolbox.invoke(
        "edit_file", {"path": "fresh.py", "old_string": "x = 1", "new_string": "x = 2"}
    )

    assert not outcome.is_error


def test_unknown_tools_are_reported_not_raised(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("launch_missiles", {})

    assert outcome.is_error
    assert "Unknown tool" in outcome.content


# --- shell approval --------------------------------------------------------


def test_shell_commands_require_approval(workspace: Workspace) -> None:
    """The one tool git cannot undo."""
    toolbox = ToolBox(workspace, approver=_deny)

    outcome = toolbox.invoke("run", {"command": "touch should-not-exist"})

    assert outcome.is_error
    assert "declined" in outcome.content
    assert not (workspace.root / "should-not-exist").exists()


def test_approved_commands_run_and_return_output(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("run", {"command": "echo hello"})

    assert not outcome.is_error
    assert "hello" in outcome.content
    assert "exit 0" in outcome.content


def test_a_failing_command_is_an_error_the_model_can_read(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("run", {"command": "exit 3"})

    assert outcome.is_error
    assert "exit 3" in outcome.content


def test_commands_time_out(toolbox: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="sleep", timeout=1)

    monkeypatch.setattr(subprocess, "run", explode)

    outcome = toolbox.invoke("run", {"command": "sleep 999", "timeout_seconds": 1})

    assert outcome.is_error
    assert "timed out" in outcome.content
