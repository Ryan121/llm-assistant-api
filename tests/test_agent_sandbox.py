"""Where the `run` tool executes.

Docker is not assumed to be present, so the container calls are asserted on the
argv that would be handed to it rather than by starting one. The end-to-end
check against a real daemon lives in docs/AGENT.md, not in the test suite - CI
should not need a container runtime to tell you the agent works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_assistant_agent.sandbox import (
    MASKED,
    WORKSPACE,
    DockerSandbox,
    HostSandbox,
    SandboxError,
    for_workspace,
)
from llm_assistant_agent.tools import ToolBox
from llm_assistant_agent.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path)


# --- the default: no sandbox at all ----------------------------------------


def test_without_a_sandbox_commands_run_here(workspace: Workspace) -> None:
    """The agent has to work on a machine with no container runtime."""
    execution = HostSandbox().prepare("echo hi", workspace.root)

    assert execution.argv == "echo hi"
    assert execution.shell is True
    assert execution.cwd == workspace.root


def test_a_toolbox_defaults_to_running_here(workspace: Workspace) -> None:
    outcome = ToolBox(workspace, lambda description, detail: True).invoke(
        "run", {"command": "echo hello-from-host"}
    )

    assert "hello-from-host" in outcome.content


def test_for_workspace_returns_the_host_runner_when_disabled(workspace: Workspace) -> None:
    assert isinstance(for_workspace(workspace.root, enabled=False), HostSandbox)


# --- the container ---------------------------------------------------------


def test_a_command_is_execed_into_the_container(workspace: Workspace) -> None:
    sandbox = DockerSandbox(workspace.root)

    execution = sandbox.prepare("pytest -q", workspace.root)

    assert execution.shell is False
    assert execution.argv == [
        "docker",
        "exec",
        "--workdir",
        WORKSPACE,
        sandbox.name,
        "sh",
        "-lc",
        "pytest -q",
    ]


def test_the_command_is_never_shell_expanded_on_the_host(workspace: Workspace) -> None:
    """Passed as one argv element, so the host shell never sees it."""
    execution = DockerSandbox(workspace.root).prepare("rm -rf / ; echo $HOME", workspace.root)

    assert execution.shell is False
    assert isinstance(execution.argv, list)
    assert execution.argv[-1] == "rm -rf / ; echo $HOME"


def test_the_container_name_is_stable_for_a_workspace(tmp_path: Path) -> None:
    """Same workspace, same container - so it is reused between sessions."""
    first = DockerSandbox(tmp_path / "repo").name
    again = DockerSandbox(tmp_path / "repo").name
    other = DockerSandbox(tmp_path / "elsewhere").name

    assert first == again
    assert first != other
    assert first.startswith("assist-")


def test_the_workspace_is_the_only_thing_mounted(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the exercise: your home directory is simply not in there."""
    calls: list[list[str]] = []

    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[1] == "ps":
            return subprocess.CompletedProcess(arguments, 0, "", "")  # no container yet
        if arguments[1] == "images":
            return subprocess.CompletedProcess(arguments, 0, "sha256:abc", "")
        return subprocess.CompletedProcess(arguments, 0, "started", "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)

    DockerSandbox(workspace.root).ensure_ready()

    created = next(call for call in calls if "run" in call)
    pairs = zip(created, created[1:], strict=False)
    mounts = [value for flag, value in pairs if flag == "--volume"]
    assert mounts[0] == f"{workspace.root}:{WORKSPACE}"
    assert "--network" in created and "none" in created


def test_host_build_artefacts_are_masked_not_shared(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .venv built on the host is for the host's platform. Sharing it into a
    Linux container is not just useless - `python -m venv .venv` in there
    rewrites the host's pyvenv.cfg through the bind mount and leaves the host
    unable to start its own interpreter."""
    calls: list[list[str]] = []

    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        output = "sha256:abc" if arguments[1] == "images" else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)
    for directory in MASKED:
        (workspace.root / directory).mkdir()

    DockerSandbox(workspace.root).ensure_ready()

    created = next(call for call in calls if "run" in call)
    pairs = zip(created, created[1:], strict=False)
    mounts = [value for flag, value in pairs if flag == "--volume"]

    assert f"{WORKSPACE}/.venv" in mounts
    for directory in MASKED:
        assert f"{WORKSPACE}/{directory}" in mounts


def test_only_directories_the_host_actually_has_are_masked(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mounting over a path the host lacks would invent an empty node_modules
    in a project with no JavaScript in it, and the model reads the listing."""
    calls: list[list[str]] = []

    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        output = "sha256:abc" if arguments[1] == "images" else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)
    (workspace.root / ".venv").mkdir()  # the only one present

    DockerSandbox(workspace.root).ensure_ready()

    created = next(call for call in calls if "run" in call)
    pairs = zip(created, created[1:], strict=False)
    mounts = [value for flag, value in pairs if flag == "--volume"]

    assert mounts == [f"{workspace.root}:{WORKSPACE}", f"{WORKSPACE}/.venv"]


def test_network_is_off_unless_asked_for(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        output = "sha256:abc" if arguments[1] == "images" else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)

    DockerSandbox(workspace.root, network=True).ensure_ready()

    created = next(call for call in calls if "run" in call)
    assert "--network" not in created


def test_a_missing_docker_is_refused_not_worked_around(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone who asked for a sandbox must not silently get an unsandboxed agent."""
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(SandboxError, match="docker"):
        DockerSandbox(workspace.root).ensure_ready()


def test_a_missing_image_says_how_to_build_it(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)

    with pytest.raises(SandboxError, match="make sandbox-image"):
        DockerSandbox(workspace.root).ensure_ready()


def _daemon(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], *, state: str, network: str
) -> None:
    """A docker that reports one existing container in the given shape."""

    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        output = ""
        if arguments[1] == "ps":
            output = state
        elif arguments[1] == "inspect":
            output = network
        elif arguments[1] == "images":
            output = "sha256:abc"
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)


def test_asking_for_network_on_a_container_without_it_is_refused(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker fixes network mode at creation. Accepting --sandbox-network and
    then ignoring it surfaces as pip failing to resolve pypi.org, which reads
    like a broken network rather than a container built without one."""
    _daemon(monkeypatch, [], state="running", network="none")

    with pytest.raises(SandboxError, match="--sandbox-wipe"):
        DockerSandbox(workspace.root, network=True).ensure_ready()


def test_a_container_with_network_is_reused_when_none_was_asked_for(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebuilding would throw away the dependencies installed with that
    network - the whole reason the workflow turns it on for one run."""
    _daemon(monkeypatch, [], state="running", network="bridge")

    note = DockerSandbox(workspace.root, network=False).ensure_ready()

    assert note is not None
    assert "with network" in note


def test_the_startup_line_reports_what_the_container_actually_has(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daemon(monkeypatch, [], state="running", network="none")

    note = DockerSandbox(workspace.root, network=False).ensure_ready()

    assert note is not None
    assert "(no network)" in note


def test_a_stopped_container_is_restarted_rather_than_rebuilt(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        output = "exited" if arguments[1] == "ps" else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)

    DockerSandbox(workspace.root).ensure_ready()

    assert any(call[1] == "start" for call in calls)
    assert not any(call[1] == "run" for call in calls)


# --- wiping ----------------------------------------------------------------


def test_wiping_removes_the_container(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        output = "running" if arguments[1] == "ps" else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)

    sandbox = DockerSandbox(workspace.root)
    assert sandbox.wipe() is True
    assert ["docker", "rm", "--force", "--volumes", sandbox.name] in calls


def test_wiping_nothing_is_not_an_error(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake)

    assert DockerSandbox(workspace.root).wipe() is False


# --- what the user is asked to approve -------------------------------------


def test_the_approval_prompt_says_where_the_command_will_run(workspace: Workspace) -> None:
    """Approving `rm -rf /` in a container is a different decision."""
    on_host = ToolBox(workspace, lambda description, detail: True)
    contained = ToolBox(
        workspace, lambda description, detail: True, sandbox=DockerSandbox(workspace.root)
    )

    here = on_host.approval_for("run", {"command": "rm -rf /"})
    there = contained.approval_for("run", {"command": "rm -rf /"})

    assert here is not None and "on this machine" in here[0]
    assert there is not None and "container" in there[0]
