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
import signal
import sys
from pathlib import Path

import httpx

from .checks import Check, Checks, detect_checks
from .client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    ChatClient,
)
from .render import Renderer
from .sandbox import DEFAULT_IMAGE, DockerSandbox, SandboxError, for_workspace
from .session import Session
from .store import SessionStore, StoredSession, new_id, restore_seen
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


def _settings(args: argparse.Namespace) -> tuple[str, str, str, int]:
    """Resolve base URL, API key, model and context window from flags, then the
    environment, then ``.env``."""
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

    # Compaction defaults on. It used to need --context-window, and a flag you
    # have to remember is a flag that is not set: a long session then runs
    # straight past the engine's window - 163k tokens against a 131k limit, in
    # the one that prompted this - and every turn after that is the model
    # working from a prompt the engine has had to truncate.
    if args.context_window:
        window = args.context_window
    else:
        declared = pick(None, "ASSIST_CONTEXT_WINDOW", "MAX_MODEL_LEN", default="")
        window = int(declared) if declared.isdigit() else 0
    return base_url, api_key, model, window


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
        help="Engine --max-model-len, used to compact the transcript before it "
        "overflows. Defaults to MAX_MODEL_LEN from the environment or .env; 0 "
        "anywhere disables compaction.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help=f"Sampling temperature (default: {DEFAULT_TEMPERATURE}). Raise it to "
        "explore, but edits get less reliable.",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=DEFAULT_TOP_P,
        help=f"Nucleus sampling cutoff (default: {DEFAULT_TOP_P})",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Ceiling on one reply (default: {DEFAULT_MAX_TOKENS}). "
        "0 defers to the gateway's cap.",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        default=os.environ.get("ASSIST_SANDBOX", "") not in {"", "0", "false"},
        help="Run shell commands in a container with only the workspace mounted, "
        "instead of on this machine. Set ASSIST_SANDBOX=1 to make it the default.",
    )
    parser.add_argument(
        "--no-sandbox",
        dest="sandbox",
        action="store_false",
        help="Run shell commands on this machine, overriding ASSIST_SANDBOX.",
    )
    parser.add_argument(
        "--sandbox-image",
        default=DEFAULT_IMAGE,
        help=f"Image for the sandbox (default: {DEFAULT_IMAGE}, built by 'make sandbox-image')",
    )
    parser.add_argument(
        "--sandbox-network",
        action="store_true",
        help="Give the sandbox network access. Off by default.",
    )
    parser.add_argument(
        "--sandbox-wipe",
        action="store_true",
        help="Throw away this workspace's sandbox container and exit. It is rebuilt "
        "on next use; nothing inside it is worth keeping.",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="ID",
        help="Continue a saved session: the most recent one here, or the given ID.",
    )
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="List saved sessions for this workspace and exit.",
    )
    parser.add_argument(
        "--delete",
        nargs="+",
        metavar="ID",
        help="Delete saved sessions by id and exit. See --sessions for the ids.",
    )
    parser.add_argument(
        "--delete-all",
        action="store_true",
        help="Delete every saved session for this workspace and exit.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not write this session to disk.",
    )
    parser.add_argument(
        "--check",
        action="append",
        metavar="COMMAND",
        help="Run COMMAND on each file the agent changes and feed any output back "
        "to it. '{path}' is replaced with the file. Repeatable; replaces the "
        "auto-detected checks.",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="Do not check changed files.",
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


#: A second Ctrl-C inside this window leaves the session instead of cancelling
#: the turn, so there is always a way out of one that will not stop - a tool
#: wedged on a syscall a thread cannot be pulled off, most likely.
_DOUBLE_INTERRUPT_SECONDS = 2.0


async def _ask(session: Session, http: httpx.AsyncClient, message: str, renderer: Renderer) -> bool:
    """Run one turn, cancellable with Ctrl-C. False means: leave the REPL.

    Ctrl-C is handled through the loop rather than as a ``KeyboardInterrupt``
    because the exception is raised inside ``run_forever``, not inside the
    coroutine - which is why catching it around the ``await`` did not work and
    the whole session went down with the turn.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(session.ask(http, message))
    last = 0.0
    leaving = False

    def interrupt() -> None:
        nonlocal last, leaving
        now = loop.time()
        leaving = leaving or (now - last) < _DOUBLE_INTERRUPT_SECONDS
        last = now
        task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, interrupt)
    except NotImplementedError:  # pragma: no cover - not a POSIX loop
        await task
        return True

    try:
        await task
    except asyncio.CancelledError:
        # Mid-stream the cursor is part-way through a line of model prose.
        renderer.end_text()
        if leaving:
            renderer.warn("interrupted twice - leaving")
        else:
            renderer.warn("interrupted - the conversation is kept, carry on or /exit")
        return not leaving
    finally:
        loop.remove_signal_handler(signal.SIGINT)
    return True


def _checks(args: argparse.Namespace) -> Checks:
    """What to run against each changed file.

    Auto-detected unless overridden, because a check the user has to switch on
    is a check that stays off. The Python syntax check inside ``Checks`` needs
    nothing installed and runs regardless.
    """
    if args.no_check:
        return Checks()
    if args.check:
        return Checks([Check(command) for command in args.check])
    return Checks(detect_checks())


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


def _wipe_sandbox(args: argparse.Namespace, workspace: Workspace, renderer: Renderer) -> int:
    container = DockerSandbox(workspace.root, image=args.sandbox_image)
    try:
        removed = container.wipe()
    except SandboxError as exc:
        renderer.error(str(exc))
        return 1
    renderer.note(f"removed {container.name}" if removed else f"no sandbox for {workspace.root}")
    return 0


def _show_sessions(store: SessionStore, workspace: Workspace, renderer: Renderer) -> int:
    saved = store.listing(workspace.root)
    if not saved:
        renderer.note(f"no saved sessions for {workspace.root}")
        return 0
    for stored in saved:
        renderer.note(_describe(stored))
    return 0


def _describe(stored: StoredSession) -> str:
    when = stored.updated.astimezone().strftime("%Y-%m-%d %H:%M")
    turns = f"{stored.turns:>3} turn{'s' if stored.turns != 1 else ' '}"
    return f"{stored.id}  {when}  {turns}  {stored.summary}"


def _delete_sessions(
    store: SessionStore, args: argparse.Namespace, workspace: Workspace, renderer: Renderer
) -> int:
    """Remove saved sessions, after showing exactly which ones.

    A transcript is not recoverable once deleted and is often the expensive
    part of a session, so what is about to go is listed first and confirmed -
    which also catches a mistyped id before it silently matches nothing.
    """
    if args.delete_all:
        doomed = store.listing(workspace.root, limit=None)
        what = f"every saved session for {workspace.root}"
    else:
        doomed = [found for found in (store.load(one) for one in args.delete) if found]
        missing = set(args.delete) - {found.id for found in doomed}
        for absent in sorted(missing):
            renderer.warn(f"no saved session {absent!r}")
        what = f"{len(doomed)} saved session{'s' if len(doomed) != 1 else ''}"

    if not doomed:
        renderer.note("nothing to delete")
        # Naming ids that matched nothing is a failure to do what was asked.
        return 1 if args.delete else 0

    for stored in doomed:
        renderer.note(_describe(stored))
    if not args.yes and not renderer.approval(f"Permanently delete {what}?", ""):
        renderer.note("left alone")
        return 0

    removed = [stored.id for stored in doomed if store.delete(stored.id)]
    renderer.note(f"deleted {len(removed)} session{'s' if len(removed) != 1 else ''}")
    return 0 if len(removed) == len(doomed) else 1


def _resume(
    store: SessionStore, args: argparse.Namespace, workspace: Workspace, renderer: Renderer
) -> StoredSession | None:
    """The session to carry on from, or None when there is nothing to resume."""
    if args.resume == "latest":
        stored = store.latest(workspace.root)
        if stored is None:
            renderer.warn(f"no saved session for {workspace.root} - starting a new one")
            return None
    else:
        stored = store.load(args.resume)
        if stored is None:
            renderer.error(f"no saved session {args.resume!r}. Try --sessions.")
            return None
        if stored.workspace != workspace.root:
            # The transcript is all about files over there.
            renderer.error(
                f"session {stored.id} belongs to {stored.workspace}, not {workspace.root}. "
                "Run it from there, or pass --cwd."
            )
            return None

    restored, stale = restore_seen(stored, workspace)
    renderer.note(f"resumed {stored.id} - {stored.turns} turns, {len(stored.messages)} messages")
    if stale:
        # Not a warning about the restore; a statement of what the model must
        # now re-read before it is allowed to edit.
        renderer.note(f"{restored} files still match, {stale} changed and must be re-read")
    head = workspace.head()
    if stored.head and head and stored.head != head:
        renderer.warn(
            f"the repository has moved on since this session (was {stored.head[:8]}, "
            f"now {head[:8]}). What it remembers about the code may be out of date."
        )
    return stored


async def _run(args: argparse.Namespace) -> int:
    renderer = Renderer(colour=False if args.no_colour else None)

    try:
        workspace = Workspace.open(Path(args.cwd))
    except WorkspaceError as exc:
        renderer.error(str(exc))
        return 1

    if args.sandbox_wipe:
        return _wipe_sandbox(args, workspace, renderer)

    store = SessionStore()
    if args.sessions:
        return _show_sessions(store, workspace, renderer)
    if args.delete or args.delete_all:
        return _delete_sessions(store, args, workspace, renderer)

    stored = _resume(store, args, workspace, renderer) if args.resume else None
    if args.resume and stored is None and args.resume != "latest":
        return 1

    # A resumed session brings its own uncommitted work with it, so the clean
    # tree check would fire on the agent's own previous edits.
    if not stored and not _check_tree(workspace, renderer, args.allow_dirty):
        return 1

    base_url, api_key, model, context_window = _settings(args)
    if not model:
        renderer.error("No model configured. Pass --model or set MODEL_ID in .env.")
        return 1

    sandbox = for_workspace(
        workspace.root,
        enabled=args.sandbox,
        image=args.sandbox_image,
        network=args.sandbox_network,
    )
    try:
        ready = sandbox.ensure_ready()
    except SandboxError as exc:
        # Refused rather than quietly falling back to the host: someone who
        # asked for a sandbox should not get an unsandboxed agent instead.
        renderer.error(str(exc))
        return 1

    session = Session(
        client=ChatClient(
            base_url,
            api_key,
            model,
            temperature=args.temperature,
            top_p=args.top_p,
            max_tokens=args.max_tokens,
        ),
        workspace=workspace,
        renderer=renderer,
        context_window=context_window,
        auto_approve=args.yes,
        checks=_checks(args),
        sandbox=sandbox,
        messages=list(stored.messages) if stored else [],
    )

    session_id = stored.id if stored else new_id()

    def keep() -> None:
        if args.no_save:
            return
        try:
            # session.history, not session.messages: compaction trims what is
            # sent to fit the context window, and saving the trimmed list makes
            # that a permanent, cumulative deletion of the conversation.
            store.save(session_id, workspace=workspace, model=model, messages=session.history)
        except OSError as exc:
            # Losing the transcript is bad; losing the session over it is worse.
            renderer.warn(f"could not save the session: {exc}")

    renderer.note(f"{model} via {base_url}")
    renderer.note(f"workspace {workspace.root}")
    if context_window:
        renderer.note(
            f"compacting at {int(context_window * 0.75) // 1000}k of {context_window // 1000}k"
        )
    else:
        renderer.warn("no context window known - the transcript will not be compacted")
    if ready:
        renderer.note(ready)
    for check in session.checks.checks:
        renderer.note(f"checking edits with {check.command.split()[0]}")
    if not args.no_save:
        renderer.note(f"session {session_id}")
    renderer.rule()

    async with httpx.AsyncClient() as http:
        if args.prompt:
            # One-shot: an interrupt has no session to return to, so let it end
            # the process the way any other cancelled command would.
            await _ask(session, http, " ".join(args.prompt), renderer)
            keep()
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
            # Cancelling closes the stream, which releases the KV-cache slot
            # upstream; the transcript is repaired by the session itself.
            staying = await _ask(session, http, message, renderer)
            # Saved after a cancel too - an interrupted turn is exactly the one
            # worth being able to come back to.
            keep()
            if not staying:
                return 130
            renderer.rule()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
