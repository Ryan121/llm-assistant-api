"""Sessions that outlive the process.

A transcript is the expensive part of an agent session - it is what the model
has already learned about the repository, bought a tool call at a time - and
losing it to a closed terminal means paying for all of it again. So each turn
is written to ``~/.assist/sessions`` and can be picked up later.

One thing here is a safety property rather than a convenience. ``Workspace``
refuses to edit a file that has not been read in this session, which is what
stops an edit built from a hallucinated recollection of the code. Restoring
that set verbatim from a session recorded last week would launder a stale
memory into a fresh permission: the model would be allowed to edit a file it
has not read, whose contents have since changed - precisely the case the rule
exists to prevent. So the digest of every file is stored with it, and on resume
an entry is restored only if the file on disk still hashes the same. Anything
that moved on has to be read again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .workspace import Workspace, WorkspaceError

__all__ = ["SessionStore", "StoredSession", "new_id", "restore_seen"]

#: Bumped when the on-disk shape changes. An older or newer file is skipped
#: rather than guessed at.
_VERSION = 1

#: Sessions kept before the oldest are pruned. Generous - a transcript is a few
#: hundred kB at most - but not unbounded.
_KEEP = 100

#: An id is one filename component and nothing else. Ids arrive from the
#: command line, and the store turns them into paths - ``--delete ../../..``
#: must not reach outside the sessions directory, least of all for an operation
#: that unlinks what it finds.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe(session_id: str) -> bool:
    return bool(_SAFE_ID.match(session_id)) and ".." not in session_id


@dataclass
class StoredSession:
    """One saved conversation."""

    id: str
    workspace: Path
    model: str
    updated: datetime
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: Workspace-relative path -> digest of the contents when it was read.
    seen: dict[str, str] = field(default_factory=dict)
    head: str | None = None

    @property
    def summary(self) -> str:
        """The first thing the user asked, for the listing."""
        for message in self.messages:
            if message.get("role") != "user":
                continue
            text = str(message.get("content") or "")
            # The rules ride on the first user turn; the question is after them.
            _, _, question = text.rpartition("\n\n---\n\n")
            first = (question or text).strip().splitlines()
            if first:
                return first[0][:70]
        return "(nothing asked yet)"

    @property
    def turns(self) -> int:
        return sum(1 for message in self.messages if message.get("role") == "user")


class SessionStore:
    """Where sessions are kept, and how they come back."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _default_root()

    # --- writing ----------------------------------------------------------

    def save(
        self,
        session_id: str,
        *,
        workspace: Workspace,
        model: str,
        messages: list[dict[str, Any]],
    ) -> Path:
        path = self.root / f"{session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _VERSION,
            "id": session_id,
            "updated": datetime.now(UTC).isoformat(),
            "workspace": str(workspace.root),
            "model": model,
            "head": workspace.head(),
            "seen": _digests(workspace),
            "messages": messages,
        }
        # Written beside and moved into place, so an interrupted save cannot
        # leave a half-written transcript where a whole one used to be.
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
        self._prune()
        return path

    def _prune(self) -> None:
        # By mtime rather than by reading each file: this runs on every turn,
        # and "which is oldest" does not justify parsing every transcript.
        saved = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in saved[_KEEP:]:
            stale.unlink(missing_ok=True)

    # --- reading ----------------------------------------------------------

    def load(self, session_id: str) -> StoredSession | None:
        if not _safe(session_id):
            return None
        return _read(self.root / f"{session_id}.json")

    def listing(self, workspace: Path | None = None, limit: int | None = 20) -> list[StoredSession]:
        """Newest first, optionally only those recorded against ``workspace``.

        Ordered by the recorded timestamp, not by filename. Ids carry a random
        suffix to avoid collisions, so two sessions started in the same second
        sort by that suffix - which is to say, arbitrarily. ``--resume`` picks
        the first of these, and silently resuming the wrong conversation is a
        bad way to find that out.
        """
        found: list[StoredSession] = []
        for path in self.root.glob("*.json"):
            stored = _read(path)
            if stored is None:
                continue
            if workspace is not None and stored.workspace != workspace:
                continue
            found.append(stored)
        found.sort(key=lambda stored: stored.updated, reverse=True)
        return found if limit is None else found[:limit]

    def latest(self, workspace: Path) -> StoredSession | None:
        found = self.listing(workspace, limit=1)
        return found[0] if found else None

    # --- deleting ---------------------------------------------------------

    def delete(self, session_id: str) -> bool:
        """Remove one saved session. Returns whether there was one to remove."""
        if not _safe(session_id):
            return False
        try:
            (self.root / f"{session_id}.json").unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True

    def delete_all(self, workspace: Path | None = None) -> list[str]:
        """Remove every saved session, or every one for ``workspace``.

        Returns the ids actually removed. Scoped to a workspace by default:
        sessions elsewhere belong to work the caller is not looking at, and
        deleting them from the wrong directory would be a surprising way to
        lose a transcript.
        """
        return [
            stored.id for stored in self.listing(workspace, limit=None) if self.delete(stored.id)
        ]


def new_id() -> str:
    """Readable and unique. Ordering comes from the recorded timestamp, not
    from this - see :meth:`SessionStore.listing`."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def restore_seen(stored: StoredSession, workspace: Workspace) -> tuple[int, int]:
    """Re-grant read-before-edit only for files that have not changed.

    Returns how many were restored and how many were dropped as stale.
    """
    restored = 0
    stale = 0
    for relative, digest in stored.seen.items():
        try:
            target = workspace.resolve(relative)
        except (WorkspaceError, OSError):
            stale += 1  # no longer resolves inside the workspace; just stale
            continue
        if target.is_file() and _digest(target) == digest:
            workspace.seen.add(target)
            restored += 1
        else:
            stale += 1
    return restored, stale


# --- helpers ---------------------------------------------------------------


def _default_root() -> Path:
    home = os.environ.get("ASSIST_HOME")
    return Path(home).expanduser() / "sessions" if home else Path.home() / ".assist" / "sessions"


def _read(path: Path) -> StoredSession | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _VERSION:
        return None
    try:
        return StoredSession(
            id=str(payload["id"]),
            workspace=Path(str(payload["workspace"])),
            model=str(payload.get("model", "")),
            updated=datetime.fromisoformat(str(payload["updated"])),
            messages=list(payload.get("messages") or []),
            seen=dict(payload.get("seen") or {}),
            head=payload.get("head"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digests(workspace: Workspace) -> dict[str, str]:
    found: dict[str, str] = {}
    for target in workspace.seen:
        try:
            found[workspace.relative(target)] = _digest(target)
        except OSError:
            continue  # deleted since it was read; it will simply be stale
    return found
