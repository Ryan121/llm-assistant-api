"""Argument handling, configuration resolution and the start-up guards.

The guards are the point: refusing to start on a dirty tree is what makes
"git is the undo buffer" true rather than aspirational.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_assistant_agent.cli import _parser, _read_env_file, _settings, main
from llm_assistant_agent.render import Renderer


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "demo.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return tmp_path


# --- .env parsing ----------------------------------------------------------


def test_env_file_is_parsed_without_being_executed(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\nAPI_KEYS="sk-one,sk-two"\nMODEL_ID=Qwen/Test\n\nBAD LINE\n',
        encoding="utf-8",
    )

    values = _read_env_file(env)

    assert values["API_KEYS"] == "sk-one,sk-two"
    assert values["MODEL_ID"] == "Qwen/Test"
    assert "BAD LINE" not in values


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert _read_env_file(tmp_path / "nope") == {}


def test_only_the_first_api_key_is_used(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("API_KEYS=sk-one,sk-two\nMODEL_ID=m\nAPI_PORT=9999\n", encoding="utf-8")
    args = _parser().parse_args(["--env-file", str(env)])

    base_url, api_key, model = _settings(args)

    assert api_key == "sk-one"
    assert model == "m"
    assert base_url == "http://127.0.0.1:9999/v1"


def test_flags_beat_the_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("MODEL_ID=from-env\n", encoding="utf-8")
    args = _parser().parse_args(
        ["--env-file", str(env), "--model", "from-flag", "--base-url", "http://x/v1"]
    )

    base_url, _, model = _settings(args)

    assert model == "from-flag"
    assert base_url == "http://x/v1"


# --- start-up guards -------------------------------------------------------


def test_refuses_to_start_on_a_dirty_tree(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "demo.py").write_text("x = 2\n", encoding="utf-8")

    code = main(
        ["--cwd", str(repo), "--model", "m", "--env-file", "/nonexistent", "--no-colour", "hi"]
    )

    assert code == 1
    assert "uncommitted changes" in capsys.readouterr().out


def test_allow_dirty_overrides_the_guard(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "demo.py").write_text("x = 2\n", encoding="utf-8")

    # Reaches the model check, which proves it got past the tree check.
    code = main(
        ["--cwd", str(repo), "--allow-dirty", "--env-file", "/nonexistent", "--no-colour", "hi"]
    )

    assert code == 1
    assert "No model configured" in capsys.readouterr().out


def test_a_non_git_directory_warns_but_proceeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--cwd", str(tmp_path), "--env-file", "/nonexistent", "--no-colour", "hi"])

    output = capsys.readouterr().out
    assert "Not a git repository" in output
    # Still stops, but on the model check rather than the tree check.
    assert code == 1
    assert "No model configured" in output


def test_a_missing_workspace_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--cwd", str(tmp_path / "nope"), "--env-file", "/nonexistent", "--no-colour"])

    assert code == 1
    assert "not a directory" in capsys.readouterr().out


# --- renderer --------------------------------------------------------------


def test_renderer_emits_no_escape_codes_when_colour_is_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = Renderer(colour=False)
    renderer.tool_call("read_file", "a.py")
    renderer.warn("careful")
    renderer.error("broken")
    renderer.note("fyi")
    renderer.rule()

    assert "\033[" not in capsys.readouterr().out


def test_renderer_colours_a_diff(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = Renderer(colour=True)
    renderer.diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n context\n")

    output = capsys.readouterr().out
    assert "\033[32m+new" in output
    assert "\033[31m-old" in output


def test_diffs_can_be_suppressed(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = Renderer(colour=False, show_diffs=False)
    renderer.diff("--- a/x\n+++ b/x\n-old\n+new\n")

    assert capsys.readouterr().out == ""


def test_turn_summary_warns_as_the_context_fills(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = Renderer(colour=False)
    renderer.turn_summary("x.py | 2 +-", used_tokens=90_000, budget=100_000)

    output = capsys.readouterr().out
    assert "x.py" in output
    assert "ctx ~90k / 100k" in output


def test_turn_summary_omits_context_when_no_budget_is_known(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = Renderer(colour=False)
    renderer.turn_summary("", used_tokens=100, budget=0)

    assert "ctx" not in capsys.readouterr().out
