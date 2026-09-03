"""``assist`` - drive an agentic coding session against the local gateway.

Deliberately a CLI rather than an editor extension. It runs where the code is,
ships and versions with the gateway, and leaves review to the tools that are
already good at it: edits land uncommitted in the working tree, so VS Code's
Source Control panel, ``git diff`` and per-hunk revert all just work.

Run it on the machine holding the repository. If the GPU box is remote, the
gateway is plain HTTP over the SSH tunnel you already have, so only tokens
cross the network - never your source.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

from .client import ChatClient
from .render import Renderer
from .session import Session
from .workspace import Workspace, WorkspaceError

__all__ = ["main"]

_DEFAULT_PORT = "8081"


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``.env`` without sourcing it. Values are never evaluated."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _settings(args: argparse.Namespace) -> tuple[str, str, str]:
    """Resolve base URL, API key and model from flags, environment, then .env."""
    env_file = _read_env_file(Path(args.env_file).expanduser())

    def pick(flag: str | None, *names: str, default: str = "") -> str:
        if flag:
            return flag
        for name in names:
            value = os.environ.get(name) or env_file.get(name)
            if value:
                return value
        return default

    port = pick(None, "API_PORT", default=_DEFAULT_PORT)
    base_url = pick(args.base_url, "ASSIST_BASE_URL", default=f"http://127.0.0.1:{port}/v1")
    api_key = pick(args.api_key, "ASSIST_API_KEY", "API_KEYS").split(",")[0].strip()
    model = pick(args.model, "MODEL_ID", default="")
    return base_url, api_key, model


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assist",
        description="Agentic coding session against a self-hosted model.",
    )
    parser.add_argument("prompt", nargs="*", help="Run one task and exit. Omit for a REPL.")
    parser.add_argument("--cwd", default=".", help="Workspace root (default: current directory)")
    parser.add_argument("--base-url", help="Gateway /v1 URL")
    parser.add_argument("--api-key", help="Bearer token; defaults to the first of API_KEYS")
    parser.add_argument("--model", help="Model id; defaults to MODEL_ID")
    parser.add_argument("--env-file", default=".env", help="Where to read defaults from")
    parser.add_argument(
        "--context-window",
        type=int,
        default=0,
        help="Engine --max-model-len, used to compact the transcript before it overflows",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Approve shell commands automatically. For unattended runs only.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Start even though the working tree has uncommitted changes",
    )
    parser.add_argument("--no-colour", action="store_true", help="Disable ANSI colour")
    return parser


def _check_tree(workspace: Workspace, renderer: Renderer, allow_dirty: bool) -> bool:
    """Git is the undo buffer, so say so when there is not a clean one."""
    if not workspace.is_git_repo:
        renderer.warn(
            "Not a git repository. Edits will be applied with no way to undo them - "
            "run 'git init' first, or be ready to lose changes."
        )
        return True
    if workspace.is_dirty() and not allow_dirty:
        renderer.error(
            "The working tree has uncommitted changes, so you would not be able to "
            "tell yours from the agent's. Commit or stash first, or pass --allow-dirty."
        )
        return False
    return True


async def _run(args: argparse.Namespace) -> int:
    renderer = Renderer(colour=False if args.no_colour else None)

    try:
        workspace = Workspace.open(Path(args.cwd))
    except WorkspaceError as exc:
        renderer.error(str(exc))
        return 1

    if not _check_tree(workspace, renderer, args.allow_dirty):
        return 1

    base_url, api_key, model = _settings(args)
    if not model:
        renderer.error("No model configured. Pass --model or set MODEL_ID in .env.")
        return 1

    session = Session(
        client=ChatClient(base_url, api_key, model),
        workspace=workspace,
        renderer=renderer,
        context_window=args.context_window,
        auto_approve=args.yes,
    )

    renderer.note(f"{model} via {base_url}")
    renderer.note(f"workspace {workspace.root}")
    renderer.rule()

    async with httpx.AsyncClient() as http:
        if args.prompt:
            await session.ask(http, " ".join(args.prompt))
            return 0

        while True:
            try:
                message = renderer.prompt().strip()
            except (EOFError, KeyboardInterrupt):
                renderer.end_text()
                return 0
            if not message:
                continue
            if message in {"/exit", "/quit"}:
                return 0
            try:
                await session.ask(http, message)
            except KeyboardInterrupt:
                # Closing the stream releases the KV-cache slot upstream.
                renderer.warn("interrupted")
            renderer.rule()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
