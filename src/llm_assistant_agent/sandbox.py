"""Where the ``run`` tool executes.

Every other tool is already contained: paths are resolved inside the workspace
and refused outside it. ``run`` is the hole in that - an arbitrary shell command
with your user, your environment and your network. The approval prompt is the
only gate, and ``cat .env`` looks perfectly reasonable at the end of a long
session.

So ``run`` can be pointed at a container instead. The workspace is bind-mounted
and everything else - your home directory, your keys, your network - is simply
not there. One long-lived container per workspace, because a test run that has
to reinstall its dependencies every time will not get used.

Two things follow from it being long-lived. It accumulates: caches, virtual
environments, whatever the agent installed at 2am. And it is therefore
disposable by design - nothing inside it is precious, because everything that
matters is in the bind-mounted workspace, on the host. ``wipe`` removes it and
the next command builds it again.

The host runner remains the default. Not everyone has Docker, the container
cannot run a macOS build, and an agent you cannot run is not safer than one you
can.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DockerSandbox", "Execution", "HostSandbox", "Sandbox", "SandboxError", "for_workspace"]

#: Built by `make sandbox-image` from docker/sandbox.Dockerfile.
DEFAULT_IMAGE = "assist-sandbox:latest"

#: Where the workspace appears inside the container. A fixed path rather than
#: the host's, so output does not leak where the repository lives.
WORKSPACE = "/workspace"

#: Directories inside the workspace that the container gets its own copy of,
#: rather than sharing with the host.
#:
#: These hold *built* artefacts compiled for whichever platform built them, and
#: the whole point of the sandbox is that the container is a different platform
#: from the host. Sharing them is not merely useless, it is destructive: a
#: ``python3 -m venv .venv`` inside the container rewrites the host's
#: ``pyvenv.cfg`` to container paths through the bind mount, and the next thing
#: the host runs finds its own virtualenv pointing at ``/usr/local/bin`` and
#: refusing to start. Nothing outside the workspace was ever at risk; this is
#: about the part of the workspace that cannot be shared across two platforms.
#:
#: Masked with anonymous volumes, so they live and die with the container and
#: ``docker rm --volumes`` takes them with it.
MASKED = (
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
)

#: Long enough that docker itself is not the reason a command fails.
_DOCKER_TIMEOUT = 120


class SandboxError(Exception):
    """The sandbox could not be prepared. Shown to the user, not the model."""


@dataclass(frozen=True)
class Execution:
    """A command, ready for :func:`subprocess.run`."""

    argv: str | list[str]
    shell: bool
    cwd: Path | None


class Sandbox:
    """Base: run it here, on this machine."""

    label = "on this machine"

    def prepare(self, command: str, root: Path) -> Execution:
        raise NotImplementedError

    def ensure_ready(self) -> str | None:
        """Set up whatever is needed, returning a line worth showing."""
        return None


class HostSandbox(Sandbox):
    """No isolation. What the agent has always done."""

    label = "on this machine"

    def prepare(self, command: str, root: Path) -> Execution:
        # shell=True is the whole point of this class: the model asked for a
        # shell command, on this machine, and the user approved that exact
        # string. --sandbox is the answer to not wanting it.
        return Execution(command, shell=True, cwd=root)  # noqa: S604


@dataclass
class DockerSandbox(Sandbox):
    """A container per workspace, with only the workspace mounted."""

    root: Path
    image: str = DEFAULT_IMAGE
    network: bool = False

    @property
    def label(self) -> str:  # type: ignore[override]
        return f"in container {self.name}"

    @property
    def name(self) -> str:
        """Stable per workspace, so the container is reused between sessions."""
        digest = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        return f"assist-{digest}"

    # --- lifecycle --------------------------------------------------------

    def ensure_ready(self) -> str | None:
        if shutil.which("docker") is None:
            raise SandboxError("--sandbox needs docker on PATH, and it is not there.")

        state = self._state()
        if state:
            self._check_reusable()
            if state != "running":
                # Exists but stopped - from a reboot, or a previous docker prune.
                self._docker("start", self.name)
                return f"sandbox {self.name} restarted {self._actual_reach()}"
            return f"sandbox {self.name} {self._actual_reach()} ({self.size() or 'new'})"

        if not self._image_exists():
            raise SandboxError(
                f"sandbox image {self.image!r} is not built. Run 'make sandbox-image', "
                "or pass --sandbox-image with one you already have."
            )
        self._create()
        return f"sandbox {self.name} created from {self.image} {self._actual_reach()}"

    def _check_reusable(self) -> None:
        """Refuse a container that cannot do what was asked of it.

        Docker fixes the network mode when a container is created, so
        ``--sandbox-network`` cannot be applied to one that already exists. The
        flag was previously accepted and then quietly ignored, and what the
        user saw instead was pip failing to resolve pypi.org - which reads like
        a broken network rather than a container built without one.

        Only this direction is an error. A container that *has* network when
        none was asked for still runs the command, and rebuilding it would
        throw away the dependencies that were installed with that network -
        which is the whole reason the workflow enables it for one run.
        """
        if self.network and self._network_mode() == "none":
            raise SandboxError(
                f"{self.name} was created without network access, and docker cannot "
                "add it to an existing container. Run 'assist --sandbox-wipe' and try "
                "again; nothing in it is worth keeping."
            )

    def _network_mode(self) -> str:
        result = self._docker("inspect", self.name, "--format", "{{.HostConfig.NetworkMode}}")
        return result.stdout.strip()

    def _actual_reach(self) -> str:
        """What the container can really reach, not what was requested. An
        existing one was configured by whichever run created it."""
        return "(no network)" if self._network_mode() == "none" else "with network"

    def _create(self) -> None:
        arguments = [
            "run",
            "--detach",
            "--name",
            self.name,
            "--volume",
            f"{self.root}:{WORKSPACE}",
            "--workdir",
            WORKSPACE,
            # Files the agent creates stay yours, rather than arriving in your
            # repository owned by root.
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            # A writable HOME, since the user above owns nothing in the image.
            "--env",
            "HOME=/tmp",
            "--label",
            "app=assist-sandbox",
        ]
        # Each of these shadows the bind mount at that path with an empty
        # container-local volume, so the host's build artefacts are neither
        # read nor - much more to the point - written.
        #
        # Only the ones actually present: mounting over a path the host does
        # not have would invent an empty node_modules in a project that has no
        # JavaScript in it, and the model reads the file listing.
        for directory in MASKED:
            if (self.root / directory).exists():
                arguments += ["--volume", f"{WORKSPACE}/{directory}"]
        if not self.network:
            arguments += ["--network", "none"]
        arguments += [self.image, "sleep", "infinity"]

        result = self._docker(*arguments)
        if result.returncode != 0:
            raise SandboxError(f"could not start the sandbox: {result.stderr.strip()}")

    def wipe(self) -> bool:
        """Throw the container away. Returns whether there was one."""
        if shutil.which("docker") is None:
            raise SandboxError("docker is not on PATH.")
        if not self._state():
            return False
        self._docker("rm", "--force", "--volumes", self.name)
        return True

    def size(self) -> str | None:
        """How much disk the container's own writable layer is using."""
        result = self._docker(
            "ps", "--all", "--filter", f"name=^{self.name}$", "--format", "{{.Size}}"
        )
        return result.stdout.strip() or None

    # --- running ----------------------------------------------------------

    def prepare(self, command: str, root: Path) -> Execution:
        return Execution(
            [
                "docker",
                "exec",
                "--workdir",
                WORKSPACE,
                self.name,
                # A login shell so the image's PATH additions apply.
                "sh",
                "-lc",
                command,
            ],
            shell=False,
            cwd=None,
        )

    # --- helpers ----------------------------------------------------------

    def _state(self) -> str | None:
        result = self._docker(
            "ps", "--all", "--filter", f"name=^{self.name}$", "--format", "{{.State}}"
        )
        return result.stdout.strip() or None

    def _image_exists(self) -> bool:
        return bool(self._docker("images", "--quiet", self.image).stdout.strip())

    def _docker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                ["docker", *arguments],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=_DOCKER_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"docker {arguments[0]} timed out") from exc
        except OSError as exc:
            raise SandboxError(f"could not run docker: {exc}") from exc


def for_workspace(
    root: Path, *, enabled: bool, image: str = DEFAULT_IMAGE, network: bool = False
) -> Sandbox:
    return DockerSandbox(root, image=image, network=network) if enabled else HostSandbox()
