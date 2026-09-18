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


# --- edit approval ---------------------------------------------------------


def test_a_declined_edit_does_not_touch_the_file(workspace: Workspace) -> None:
    """The point of the whole feature: no means the file is unchanged."""
    toolbox = ToolBox(workspace, approver=_deny)
    toolbox.invoke("read_file", {"path": "src/app.py"})
    before = (workspace.root / "src" / "app.py").read_text(encoding="utf-8")

    outcome = toolbox.invoke(
        "edit_file", {"path": "src/app.py", "old_string": "return 1", "new_string": "return 2"}
    )

    assert outcome.is_error
    assert "declined" in outcome.content
    assert (workspace.root / "src" / "app.py").read_text(encoding="utf-8") == before


def test_a_declined_write_does_not_create_the_file(workspace: Workspace) -> None:
    toolbox = ToolBox(workspace, approver=_deny)

    outcome = toolbox.invoke("write_file", {"path": "new.py", "content": "x = 1\n"})

    assert outcome.is_error
    assert not (workspace.root / "new.py").exists()


def test_the_approval_shows_the_diff_the_edit_would_make(toolbox: ToolBox) -> None:
    """What the user is asked to authorise is the change, not just the path.

    A prompt naming only the file is one the user can only answer by trusting
    the model, which is the thing that has been going wrong.
    """
    toolbox.invoke("read_file", {"path": "src/app.py"})

    request = toolbox.approval_for(
        "edit_file", {"path": "src/app.py", "old_string": "return 1", "new_string": "return 2"}
    )

    assert request is not None
    description, diff = request
    assert "src/app.py" in description
    assert "-    return 1" in diff
    assert "+    return 2" in diff


def test_approving_an_edit_does_not_satisfy_read_before_edit(workspace: Workspace) -> None:
    """Building the preview must not record the file as seen.

    ``workspace.read`` marks files seen, so a preview built through it would
    let the model edit a file it never read - the rule would be satisfied by
    the machinery that exists to enforce it.
    """
    asked: list[str] = []

    def approve(description: str, detail: str) -> bool:
        asked.append(description)
        return True

    toolbox = ToolBox(workspace, approver=approve)
    arguments = {"path": "src/app.py", "old_string": "return 1", "new_string": "return 2"}

    assert toolbox.approval_for("edit_file", arguments) is None

    outcome = toolbox.invoke("edit_file", arguments)
    assert outcome.is_error
    assert "has not been read" in outcome.content
    assert asked == []


def test_an_unmatchable_edit_is_not_put_to_the_user(toolbox: ToolBox) -> None:
    """It is going to fail; asking is a question about nothing."""
    toolbox.invoke("read_file", {"path": "src/app.py"})

    request = toolbox.approval_for(
        "edit_file", {"path": "src/app.py", "old_string": "absent", "new_string": "x"}
    )

    assert request is None


def test_an_edit_that_changes_nothing_is_not_put_to_the_user(toolbox: ToolBox) -> None:
    toolbox.invoke("read_file", {"path": "src/app.py"})

    request = toolbox.approval_for(
        "edit_file", {"path": "src/app.py", "old_string": "return 1", "new_string": "return 1"}
    )

    assert request is None


def test_creating_a_file_says_so(toolbox: ToolBox) -> None:
    create = toolbox.approval_for("write_file", {"path": "new.py", "content": "x = 1\n"})
    overwrite = toolbox.approval_for("write_file", {"path": "README.md", "content": "# other\n"})

    assert create is not None and create[0].startswith("Create")
    assert overwrite is not None and overwrite[0].startswith("Overwrite")


def test_reading_tools_are_never_put_to_the_user(toolbox: ToolBox) -> None:
    """Approval fatigue is the failure mode; only writes and commands ask."""
    for name in ("list_files", "grep", "read_file", "read_document", "git_diff"):
        assert toolbox.approval_for(name, {"path": "src/app.py", "pattern": "x"}) is None


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


# --- git_diff --------------------------------------------------------------


@pytest.fixture
def git_toolbox(workspace: Workspace) -> ToolBox:
    """A committed baseline, so later edits show up as a diff."""
    for arguments in (
        ["init", "-q", "."],
        ["config", "user.email", "agent@test"],
        ["config", "user.name", "agent"],
        ["add", "-A"],
        ["commit", "-qm", "baseline"],
    ):
        subprocess.run(  # noqa: S603
            ["git", *arguments],  # noqa: S607
            cwd=workspace.root,
            check=True,
            capture_output=True,
        )
    return ToolBox(workspace, approver=lambda description, detail: True)


def test_git_diff_shows_an_edit(git_toolbox: ToolBox) -> None:
    (git_toolbox.workspace.root / "src" / "app.py").write_text(
        "def main():\n    return 2\n", encoding="utf-8"
    )

    outcome = git_toolbox.invoke("git_diff", {})

    assert "-    return 1" in outcome.content
    assert "+    return 2" in outcome.content


def test_git_diff_includes_files_the_agent_created(git_toolbox: ToolBox) -> None:
    """Plain 'git diff' shows nothing for an untracked file - the agent's own
    new file is exactly what it most needs to review."""
    git_toolbox.invoke("write_file", {"path": "fresh.py", "content": "VALUE = 1\n"})

    outcome = git_toolbox.invoke("git_diff", {})

    assert "fresh.py" in outcome.content
    assert "+VALUE = 1" in outcome.content


def test_git_diff_summary_is_cheaper_than_the_patch(git_toolbox: ToolBox) -> None:
    (git_toolbox.workspace.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    outcome = git_toolbox.invoke("git_diff", {"summary": True})

    assert "src/app.py" in outcome.content
    assert "+x = 1" not in outcome.content


def test_git_diff_can_be_narrowed_to_one_path(git_toolbox: ToolBox) -> None:
    (git_toolbox.workspace.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (git_toolbox.workspace.root / "README.md").write_text("# changed\n", encoding="utf-8")

    outcome = git_toolbox.invoke("git_diff", {"path": "src/app.py"})

    assert "src/app.py" in outcome.content
    assert "README.md" not in outcome.content


def test_git_diff_refuses_a_path_outside_the_workspace(git_toolbox: ToolBox) -> None:
    outcome = git_toolbox.invoke("git_diff", {"path": "../../etc/hosts"})

    assert outcome.is_error
    assert "outside the workspace" in outcome.content


def test_git_diff_says_so_when_there_is_nothing_to_show(git_toolbox: ToolBox) -> None:
    assert git_toolbox.invoke("git_diff", {}).content == "No uncommitted changes."


def test_git_diff_outside_a_repository_is_an_error_not_a_crash(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("git_diff", {})

    assert outcome.is_error
    assert "not a git repository" in outcome.content


def _committed(root: Path) -> None:
    for arguments in (
        ["init", "-q", "."],
        ["config", "user.email", "agent@test"],
        ["config", "user.name", "agent"],
        ["add", "-A"],
        ["commit", "-qm", "baseline"],
    ):
        subprocess.run(  # noqa: S603
            ["git", *arguments],  # noqa: S607
            cwd=root,
            check=True,
            capture_output=True,
        )


def test_a_diff_says_which_changes_were_not_the_agents(tmp_path: Path) -> None:
    """The inference this exists to prevent.

    A diff is a picture of the working tree, not a log of the session. Told
    nothing, a real session read 1326 changed lines in a template it had never
    touched - left by the session before it - as its own mistake, and spent
    three calls trying to git checkout the user's work.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (tmp_path / "page.html").write_text("<p>original</p>\n", encoding="utf-8")
    _committed(tmp_path)
    # Dirtied before the session opens, which is what makes it not the agent's.
    (tmp_path / "page.html").write_text("<p>the user's own work</p>\n", encoding="utf-8")

    workspace = Workspace.open(tmp_path)
    toolbox = ToolBox(workspace, approver=lambda description, detail: True)

    assert workspace.baseline_dirty == frozenset({"page.html"})

    outcome = toolbox.invoke("git_diff", {})

    assert "page.html already had uncommitted changes when this session started" in outcome.content
    assert "do not revert it" in outcome.content


def test_a_file_the_agent_edited_is_not_disowned(tmp_path: Path) -> None:
    """The opposite failure, and the likelier one in this project.

    The repository this came from is entirely uncommitted, so every file is in
    the baseline - including the ones the agent was asked to change. Naming
    those would warn it off its own task, which is the over-restriction the
    note exists to prevent in the first place.
    """
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "page.html").write_text("<p>original</p>\n", encoding="utf-8")
    _committed(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "page.html").write_text("<p>the user's own work</p>\n", encoding="utf-8")

    workspace = Workspace.open(tmp_path)
    toolbox = ToolBox(workspace, approver=lambda description, detail: True)
    # The agent now edits one of the two itself.
    toolbox.invoke("read_file", {"path": "app.py"})
    toolbox.invoke("edit_file", {"path": "app.py", "old_string": "x = 2", "new_string": "x = 3"})

    content = toolbox.invoke("git_diff", {}).content

    assert "page.html" in content.split("Note:")[1]
    assert "app.py" not in content.split("Note:")[1]


def test_a_diff_of_the_agents_own_work_carries_no_such_note(git_toolbox: ToolBox) -> None:
    """The note must not cry wolf, or it stops being read."""
    git_toolbox.invoke("write_file", {"path": "fresh.py", "content": "VALUE = 1\n"})

    outcome = git_toolbox.invoke("git_diff", {})

    assert "before this session started" not in outcome.content


def test_a_narrowed_diff_only_notes_that_path(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("y = 1\n", encoding="utf-8")
    _committed(tmp_path)
    (tmp_path / "one.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("y = 2\n", encoding="utf-8")

    toolbox = ToolBox(Workspace.open(tmp_path), approver=lambda description, detail: True)

    assert "one.py already had" in toolbox.invoke("git_diff", {"path": "one.py"}).content
    assert "one.py already had" not in toolbox.invoke("git_diff", {"path": "two.py"}).content


# --- reading part of a file -------------------------------------------------
#
# The most expensive bug in the tool set, and the quietest. Across six real
# sessions 56 of 87 read_file calls asked for a line range - in five different
# spellings - and every one was dropped on the floor and answered with the
# whole file. One session asked for sixteen windows of 10 to 80 lines and got
# 109 kB back every time, including four consecutive requests for ten lines.


@pytest.fixture
def long_file(workspace: Workspace) -> Workspace:
    body = "".join(f"line {number}\n" for number in range(1, 501))
    (workspace.root / "long.txt").write_text(body, encoding="utf-8")
    return workspace


def test_a_line_range_returns_only_those_lines(long_file: Workspace) -> None:
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt", "begin": 10, "end": 12})

    assert "line 10" in outcome.content
    assert "line 12" in outcome.content
    assert "line 13" not in outcome.content
    assert "line 9" not in outcome.content
    # The window has to say where it sits, or the model cannot cite it back.
    assert "lines 10-12 of 500" in outcome.content


def test_line_numbers_in_a_range_are_the_files_own(long_file: Workspace) -> None:
    """Numbered from the window rather than the file, every line the model
    cited back would be wrong by the offset."""
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt", "begin": 100, "end": 101})

    assert "   100\tline 100" in outcome.content


@pytest.mark.parametrize(
    "arguments",
    [
        {"begin": "10", "end": "12"},
        {"start_line": 10, "end_line": 12},
        {"offset": "10", "length": "3"},
        {"offset": 10, "limit": 3},
        {"range": [10, 12]},
        {"range": "[10, 12]"},
        {"lines": [10, 12]},
    ],
)
def test_the_spellings_the_model_actually_uses_all_work(
    long_file: Workspace, arguments: dict[str, object]
) -> None:
    """Every one of these came out of a real transcript."""
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt", **arguments})

    assert not outcome.is_error
    assert "line 10" in outcome.content
    assert "line 12" in outcome.content
    assert "line 13" not in outcome.content


def test_a_whole_file_read_is_unchanged(long_file: Workspace) -> None:
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt"})

    assert outcome.content.startswith("     1\tline 1\n")
    assert "line 500" in outcome.content
    assert "of 500" not in outcome.content


def test_a_range_past_the_end_of_the_file_says_how_long_it_is(long_file: Workspace) -> None:
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt", "begin": 900, "end": 950})

    assert outcome.is_error
    assert "only 500 lines" in outcome.content


def test_a_range_that_overruns_the_end_is_clamped(long_file: Workspace) -> None:
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt", "begin": 498, "end": 600})

    assert not outcome.is_error
    assert "lines 498-500 of 500" in outcome.content


def test_a_backwards_range_is_refused_rather_than_guessed_at(long_file: Workspace) -> None:
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt", "begin": 50, "end": 10})

    assert outcome.is_error
    assert "before" in outcome.content


def test_line_zero_is_refused_rather_than_treated_as_one(long_file: Workspace) -> None:
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt", "begin": 0, "end": 10})

    assert outcome.is_error
    assert "count from 1" in outcome.content


def test_a_partial_read_still_permits_an_edit(long_file: Workspace) -> None:
    """The read-before-edit rule is about having looked at the file."""
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)
    toolbox.invoke("read_file", {"path": "long.txt", "begin": 10, "end": 12})

    outcome = toolbox.invoke(
        "edit_file",
        {"path": "long.txt", "old_string": "line 11\nline 12", "new_string": "changed\nline 12"},
    )

    assert not outcome.is_error


def test_a_partial_read_does_not_clear_the_stale_write_warning(long_file: Workspace) -> None:
    """Reading ten lines at the top says nothing about the write at line 400."""
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)
    toolbox.invoke("read_file", {"path": "long.txt"})
    toolbox.invoke("write_file", {"path": "long.txt", "content": "one\ntwo\nthree\n"})
    toolbox.invoke("read_file", {"path": "long.txt", "begin": 1, "end": 2})

    outcome = toolbox.invoke(
        "edit_file", {"path": "long.txt", "old_string": "line 400", "new_string": "x"}
    )

    assert outcome.is_error
    assert "written this file since you last read it" in outcome.content


# --- arguments a tool does not understand -----------------------------------


def test_an_unknown_argument_is_reported_rather_than_dropped(toolbox: ToolBox) -> None:
    """Silently ignoring part of a call is how the range bug survived: the
    result looks like an answer to the question that was asked."""
    outcome = toolbox.invoke("grep", {"pattern": "def", "recursive": True})

    assert "grep does not take recursive" in outcome.content
    assert "it was ignored" in outcome.content
    # And it still says what the tool does take, so the retry can be right.
    assert "glob" in outcome.content


def test_several_unknown_arguments_read_naturally(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("list_files", {"depth": 2, "sort": "name"})

    assert "does not take depth, sort" in outcome.content
    assert "they were ignored" in outcome.content


def test_a_known_argument_draws_no_note(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("grep", {"pattern": "def", "glob": "*.py"})

    assert "does not take" not in outcome.content


def test_a_range_alias_is_not_reported_as_unknown(long_file: Workspace) -> None:
    """It is understood, so complaining about it would be a lie."""
    toolbox = ToolBox(long_file, approver=lambda description, detail: True)

    outcome = toolbox.invoke("read_file", {"path": "long.txt", "offset": 10, "length": 3})

    assert "does not take" not in outcome.content


# --- commands that cannot be undone ----------------------------------------


def test_a_command_that_discards_work_is_labelled_as_one(toolbox: ToolBox) -> None:
    """git is the undo behind every edit here, so this is the one call with
    none behind it. A real session asked to git checkout 1326 lines of the
    user's own changes, under a prompt that read the same as `wc -l`."""
    request = toolbox.approval_for("run", {"command": "git checkout templates/index.html"})

    assert request is not None
    assert "DISCARD uncommitted changes" in request[0]


@pytest.mark.parametrize(
    "command",
    [
        "git restore .",
        "git reset --hard HEAD",
        "git clean -fd",
        "git stash",
        "cd x && git checkout -- .",
    ],
)
def test_the_other_ways_of_losing_work_are_labelled_too(toolbox: ToolBox, command: str) -> None:
    request = toolbox.approval_for("run", {"command": command})

    assert request is not None
    assert "DISCARD" in request[0]


@pytest.mark.parametrize(
    "command",
    ["git status", "git diff", "git stash list", "git log --oneline", "pytest -q", "wc -l app.py"],
)
def test_harmless_commands_are_not_labelled(toolbox: ToolBox, command: str) -> None:
    request = toolbox.approval_for("run", {"command": command})

    assert request is not None
    assert "DISCARD" not in request[0]


# --- editing from memory ---------------------------------------------------


def test_editing_after_your_own_write_is_told_the_file_moved_on(workspace: Workspace) -> None:
    """The commonest way a real edit misses.

    write_file marks the file seen, so read-before-edit is satisfied and the
    model goes on editing from its recollection of what it wrote. When that
    recollection is wrong the error should say why, not leave it to be
    guessed at.
    """
    toolbox = ToolBox(workspace, approver=lambda description, detail: True)
    toolbox.invoke("write_file", {"path": "new.py", "content": "a = 1\n"})

    outcome = toolbox.invoke(
        "edit_file", {"path": "new.py", "old_string": "a = 2", "new_string": "a = 3"}
    )

    assert outcome.is_error
    assert "written this file since you last read it" in outcome.content


def test_reading_the_file_again_clears_that(workspace: Workspace) -> None:
    toolbox = ToolBox(workspace, approver=lambda description, detail: True)
    toolbox.invoke("write_file", {"path": "new.py", "content": "a = 1\n"})
    toolbox.invoke("read_file", {"path": "new.py"})

    outcome = toolbox.invoke(
        "edit_file", {"path": "new.py", "old_string": "a = 2", "new_string": "a = 3"}
    )

    assert outcome.is_error
    assert "written this file since you last read it" not in outcome.content
