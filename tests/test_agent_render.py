"""Terminal output, and in particular the spinner.

The spinner is the one piece of rendering that writes over itself, so the
assertions here are about what it leaves behind: a line it drew must be erased
before anything else is printed on top of it, and none of it may reach a
stream that is not a terminal.
"""

from __future__ import annotations

import asyncio

import pytest

from llm_assistant_agent import render
from llm_assistant_agent.render import Renderer

#: The erase-line sequence the spinner cleans up with.
ERASE = "\r\033[2K"


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the animation on a timescale a test can wait for."""
    monkeypatch.setattr(render, "_SPINNER_DELAY", 0.01)
    monkeypatch.setattr(render, "_SPINNER_INTERVAL", 0.01)


@pytest.fixture
def terminal() -> Renderer:
    """A renderer that believes it is attached to one."""
    renderer = Renderer(colour=True)
    renderer.animate = True
    return renderer


async def test_nothing_is_drawn_when_output_is_not_a_terminal(
    quick: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Redraw sequences in a log file or a captured stream are just noise."""
    renderer = Renderer(colour=False)
    assert not renderer.animate

    async with renderer.working("thinking"):
        await asyncio.sleep(0.05)

    assert capsys.readouterr().out == ""


async def test_the_spinner_animates_and_erases_itself(
    quick: None, terminal: Renderer, capsys: pytest.CaptureFixture[str]
) -> None:
    async with terminal.working("running"):
        await asyncio.sleep(0.08)

    out = capsys.readouterr().out
    assert "running" in out
    assert any(frame in out for frame in render._SPINNER)
    # Whatever is printed next must land on a clean line.
    assert out.endswith(ERASE)


async def test_a_wait_too_short_to_notice_draws_nothing(
    quick: None,
    terminal: Renderer,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spinner that flashes up for one frame is worse than no spinner."""
    monkeypatch.setattr(render, "_SPINNER_DELAY", 5.0)

    async with terminal.working("working"):
        await asyncio.sleep(0.01)

    assert capsys.readouterr().out == ""


async def test_stopping_early_clears_the_line_before_the_next_write(
    quick: None, terminal: Renderer, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the first streamed token does.

    The spinner and the reply share a line, so the reply must not simply be
    written over a half-drawn frame.
    """
    waiting = terminal.working("thinking")
    async with waiting:
        await asyncio.sleep(0.05)
        waiting.stop()
        terminal.text_delta("Here is the answer.")
        await asyncio.sleep(0.05)

    out = capsys.readouterr().out
    answer = out.index("Here is the answer.")
    assert out[:answer].endswith(ERASE), "the spinner line was not cleared first"
    # And it must stay stopped: no frame may appear over the reply.
    assert not any(frame in out[answer:] for frame in render._SPINNER)


async def test_stopping_twice_is_harmless(
    quick: None, terminal: Renderer, capsys: pytest.CaptureFixture[str]
) -> None:
    waiting = terminal.working("thinking")
    async with waiting:
        await asyncio.sleep(0.05)
        waiting.stop()
        waiting.stop()

    assert capsys.readouterr().out.count(ERASE) >= 1


async def test_a_cancelled_turn_leaves_no_spinner_running(
    quick: None, terminal: Renderer, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl-C during a long wait must not leave a task drawing over the prompt."""

    async def turn() -> None:
        async with terminal.working("thinking"):
            await asyncio.sleep(10)

    task = asyncio.ensure_future(turn())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    capsys.readouterr()
    await asyncio.sleep(0.05)
    assert capsys.readouterr().out == "", "a spinner survived the cancellation"


async def test_a_long_wait_says_how_long(
    quick: None,
    terminal: Renderer,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Waiting" and "hung" look identical without a number."""
    monkeypatch.setattr(render, "_ELAPSED_AFTER", 0.0)

    async with terminal.working("running"):
        await asyncio.sleep(0.05)

    assert "running 0s" in capsys.readouterr().out


# --- the spinner across one turn -------------------------------------------


async def test_the_spinner_comes_back_when_prose_gives_way_to_a_tool_call(
    quick: None, terminal: Renderer, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure this exists for.

    Qwen3-Coder narrates before it acts, so a spinner that merely stopped at
    the first token would vanish exactly before the quietest part of the turn:
    the arguments of a whole-file write, streaming with nothing printed.
    """
    from llm_assistant_agent.session import _Stream

    waiting = terminal.working("thinking")
    stream = _Stream(terminal, waiting)
    async with waiting:
        await asyncio.sleep(0.03)
        stream("I'll rewrite the parser.")
        stream.tool_delta()
        await asyncio.sleep(0.05)

    out = capsys.readouterr().out
    prose = out.index("I'll rewrite the parser.")
    after = out[prose:]
    assert any(frame in after for frame in render._SPINNER), "no spinner after the prose"
    assert "\n" in after[: after.index(next(f for f in render._SPINNER if f in after))], (
        "the spinner drew over the prose instead of on its own line"
    )


async def test_the_line_is_only_ended_once(quick: None, terminal: Renderer) -> None:
    """``resumed`` is what stops the caller adding a second newline."""
    from llm_assistant_agent.session import _Stream

    waiting = terminal.working("thinking")
    stream = _Stream(terminal, waiting)
    async with waiting:
        stream("some prose")
        stream.tool_delta()
        stream.tool_delta()

    assert stream.wrote and stream.resumed


async def test_a_turn_with_no_prose_never_stops_the_spinner(
    quick: None, terminal: Renderer, capsys: pytest.CaptureFixture[str]
) -> None:
    from llm_assistant_agent.session import _Stream

    waiting = terminal.working("thinking")
    stream = _Stream(terminal, waiting)
    async with waiting:
        await asyncio.sleep(0.03)
        stream.tool_delta()
        await asyncio.sleep(0.03)

    assert not stream.resumed
    assert any(frame in capsys.readouterr().out for frame in render._SPINNER)


# --- approval prompts ------------------------------------------------------


def test_a_closed_stdin_is_not_a_refusal(
    terminal: Renderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this replaces.

    Returning False on EOF recorded a decision nobody made: the transcript
    said the user declined, the model apologised and tried something else, and
    every later approval refused the same way. In one real session that was
    seven calls in a row, ``wc -l`` among them.
    """

    def no_input(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)

    with pytest.raises(render.InputUnavailableError):
        terminal.approval("Run a shell command", "wc -l file.txt")


def test_keystrokes_typed_before_the_question_do_not_answer_it(
    terminal: Renderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn is mostly waiting, and people type into the gap."""
    flushed: list[str] = []
    monkeypatch.setattr(terminal, "_discard_pending_input", lambda: flushed.append("flushed"))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    assert terminal.approval("Edit app.py", "@@ -1 +1 @@") is True
    assert flushed == ["flushed"], "pending input was not discarded before asking"


def test_an_answer_of_yes_is_still_an_approval(
    terminal: Renderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "  YES  ")
    assert terminal.approval("Run a shell command", "ls") is True


def test_anything_else_is_still_a_refusal(
    terminal: Renderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert terminal.approval("Run a shell command", "rm -rf /") is False
