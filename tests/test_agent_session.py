"""The agent loop.

Driven through a scripted fake gateway so a whole multi-step session - read,
edit, verify, answer - runs hermetically, with no model and no network.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from llm_assistant_agent.client import ChatClient
from llm_assistant_agent.render import Renderer
from llm_assistant_agent.session import Session
from llm_assistant_agent.workspace import Workspace

BASE_URL = "http://gateway.test/v1"


def _sse(events: list[dict[str, Any]]) -> str:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return body + "data: [DONE]\n\n"


def text_turn(text: str) -> str:
    return _sse([{"choices": [{"index": 0, "delta": {"content": text}}]}])


def tool_turn(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> str:
    return _sse(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        ]
    )


class ScriptedGateway:
    """Replies with the next scripted turn and records what it was sent."""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            payload = self.script.pop(0) if self.script else text_turn("done")

            async def body() -> AsyncIterator[bytes]:
                yield payload.encode()

            return httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            )

        return httpx.MockTransport(handler)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return Workspace.open(tmp_path)


def _session(workspace: Workspace, **overrides: Any) -> Session:
    settings: dict[str, Any] = {"auto_approve": True}
    settings.update(overrides)
    return Session(
        client=ChatClient(BASE_URL, "sk-test", "test-model"),
        workspace=workspace,
        renderer=Renderer(colour=False),
        **settings,
    )


async def _ask(session: Session, gateway: ScriptedGateway, message: str) -> None:
    async with httpx.AsyncClient(transport=gateway.transport()) as http:
        await session.ask(http, message)


# --- the loop --------------------------------------------------------------


async def test_a_plain_answer_ends_the_turn(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = ScriptedGateway([text_turn("It returns 1.")])
    session = _session(workspace)

    await _ask(session, gateway, "what does main return?")

    assert "It returns 1." in capsys.readouterr().out
    assert len(gateway.requests) == 1


async def test_a_tool_call_is_executed_and_its_result_fed_back(
    workspace: Workspace,
) -> None:
    gateway = ScriptedGateway(
        [tool_turn("read_file", {"path": "app.py"}), text_turn("It returns 1.")]
    )
    session = _session(workspace)

    await _ask(session, gateway, "read app.py")

    # Second request carries the tool result, correlated by id.
    second = gateway.requests[1]["messages"]
    tool_message = next(m for m in second if m["role"] == "tool")
    assert tool_message["tool_call_id"] == "call_1"
    assert "def main" in tool_message["content"]


async def test_a_full_read_edit_verify_session(workspace: Workspace) -> None:
    gateway = ScriptedGateway(
        [
            tool_turn("read_file", {"path": "app.py"}),
            tool_turn(
                "edit_file",
                {"path": "app.py", "old_string": "return 1", "new_string": "return 2"},
            ),
            text_turn("Changed the return value to 2."),
        ]
    )
    session = _session(workspace)

    await _ask(session, gateway, "make main return 2")

    assert (workspace.root / "app.py").read_text() == "def main():\n    return 2\n"


async def test_a_failed_edit_is_reported_back_so_the_model_can_retry(
    workspace: Workspace,
) -> None:
    gateway = ScriptedGateway(
        [
            tool_turn("read_file", {"path": "app.py"}),
            tool_turn(
                "edit_file",
                {"path": "app.py", "old_string": "nonexistent", "new_string": "x"},
            ),
            text_turn("I could not find that text."),
        ]
    )
    session = _session(workspace)

    await _ask(session, gateway, "edit it")

    tool_results = [
        m for request in gateway.requests for m in request["messages"] if m["role"] == "tool"
    ]
    assert any("was not found" in m["content"] for m in tool_results)


async def test_a_declined_command_does_not_end_the_session(workspace: Workspace) -> None:
    gateway = ScriptedGateway(
        [tool_turn("run", {"command": "rm -rf /"}), text_turn("Understood, skipping that.")]
    )
    session = _session(workspace, auto_approve=False)
    # A non-interactive renderer refuses rather than blocking on input().
    session.renderer.approval = lambda description, detail: False  # type: ignore[method-assign]

    await _ask(session, gateway, "clean up")

    tool_results = [
        m for request in gateway.requests for m in request["messages"] if m["role"] == "tool"
    ]
    assert any("declined" in m["content"] for m in tool_results)


async def test_a_malformed_tool_call_prompts_a_retry_rather_than_hanging(
    workspace: Workspace,
) -> None:
    broken = _sse(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c",
                                    "function": {"name": "read_file", "arguments": '{"path'},
                                }
                            ]
                        },
                    }
                ]
            }
        ]
    )
    gateway = ScriptedGateway([broken, text_turn("Sorry, retrying.")])
    session = _session(workspace)

    await _ask(session, gateway, "read something")

    assert len(gateway.requests) == 2
    assert "did not arrive intact" in gateway.requests[1]["messages"][-1]["content"]


async def test_a_tool_call_leaked_as_text_prompts_a_retry(workspace: Workspace) -> None:
    """The upstream parser rejects a call missing its wrapper and streams it as content.

    Nothing runs and no fragment ever arrives, so without this the agent stops
    silently having done nothing - the failure that is expensive to diagnose.
    """
    leaked = text_turn(
        "I'll look at the tests first.\n\n"
        "<function=list_files>\n<parameter=pattern>\n**/test*.py\n</parameter>\n"
        "</function>\n</tool_call>"
    )
    gateway = ScriptedGateway([leaked, text_turn("Sorry, retrying.")])
    session = _session(workspace)

    await _ask(session, gateway, "run the unit tests")

    assert len(gateway.requests) == 2
    retry = gateway.requests[1]["messages"][-1]["content"]
    assert "did not arrive intact" in retry
    # Naming the tool and the cause is what makes the retry actionable.
    assert "list_files" in retry
    assert "plain text" in retry


async def test_prose_about_tool_call_syntax_is_not_flagged(workspace: Workspace) -> None:
    """Asked about this bug, the agent has to be able to describe it without retrying."""
    gateway = ScriptedGateway(
        [text_turn("The parser wants <function=name></function> inside a wrapper.")]
    )
    session = _session(workspace)

    await _ask(session, gateway, "why did the tool call fail?")

    assert len(gateway.requests) == 1


async def test_quoting_tool_call_syntax_alongside_a_real_call_is_not_flagged(
    workspace: Workspace,
) -> None:
    """An agent working on this repository will quote this syntax legitimately."""
    quoted = _sse(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": "The parser wants <function=x></function> wrapped.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "notes.txt"}),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    )
    (workspace.root / "notes.txt").write_text("hello\n", encoding="utf-8")
    gateway = ScriptedGateway([quoted, text_turn("done")])
    session = _session(workspace)

    await _ask(session, gateway, "read the notes")

    assert not any(
        "did not arrive intact" in str(m.get("content"))
        for request in gateway.requests
        for m in request["messages"]
    )


async def test_tools_are_advertised_on_every_request(workspace: Workspace) -> None:
    gateway = ScriptedGateway([text_turn("hi")])
    session = _session(workspace)

    await _ask(session, gateway, "hello")

    names = {tool["function"]["name"] for tool in gateway.requests[0]["tools"]}
    assert {"read_file", "edit_file", "grep", "run"} <= names


async def test_the_system_prompt_forbids_committing(workspace: Workspace) -> None:
    """The repo's releases are driven by commit messages; the agent must not write them."""
    gateway = ScriptedGateway([text_turn("hi")])
    session = _session(workspace)

    await _ask(session, gateway, "hello")

    opening = gateway.requests[0]["messages"][0]
    assert opening["role"] == "user"
    assert "git commit" in opening["content"]


# --- context management ----------------------------------------------------


async def test_the_transcript_is_compacted_before_it_overflows(
    workspace: Workspace,
) -> None:
    gateway = ScriptedGateway([text_turn("ok")])
    session = _session(workspace, context_window=1000)
    # Ten turns of filler, well past 75% of a 1000-token window.
    for index in range(10):
        session.messages.append({"role": "user", "content": f"{index} " + "x" * 2000})
        session.messages.append({"role": "assistant", "content": "noted"})

    await _ask(session, gateway, "carry on")

    sent = gateway.requests[0]["messages"]
    # Compaction keeps the head, which is where the rules now live.
    assert sent[0]["role"] == "user"
    assert any("were dropped" in str(m.get("content")) for m in sent)
    assert len(sent) < 22


async def test_compaction_never_orphans_a_tool_result(workspace: Workspace) -> None:
    """A tool message whose assistant turn was dropped makes vLLM reject the request."""
    gateway = ScriptedGateway([text_turn("ok")])
    session = _session(workspace, context_window=1000)
    for index in range(10):
        session.messages.append({"role": "assistant", "content": "x" * 2000})
        session.messages.append(
            {"role": "tool", "tool_call_id": f"c{index}", "content": "y" * 2000}
        )

    await _ask(session, gateway, "carry on")

    sent = gateway.requests[0]["messages"]
    for position, message in enumerate(sent):
        if message.get("role") == "tool":
            assert sent[position - 1].get("role") in {"assistant", "tool"}


async def test_no_compaction_without_a_declared_window(workspace: Workspace) -> None:
    gateway = ScriptedGateway([text_turn("ok")])
    session = _session(workspace)
    for _ in range(10):
        session.messages.append({"role": "user", "content": "x" * 5000})

    await _ask(session, gateway, "carry on")

    # Ten filler turns plus the new one; the rules ride on the first user turn.
    assert len(gateway.requests[0]["messages"]) == 11
