"""The working tree the agent is allowed to touch.

Two invariants live here, and both exist to make a bad turn survivable:

* every path is resolved and checked to be inside the workspace root, so a
  model that emits ``../../.ssh/id_rsa`` gets an error rather than a file
* a file must have been read in this session before it can be edited, which
  is what stops an edit built from a hallucinated recollection of the code

Undo is git's job, not ours. Edits land in the working tree uncommitted, so
``git checkout --`` and VS Code's per-hunk revert both work, and the review
surface is the one the user already trusts.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Workspace", "WorkspaceError"]

#: Never walked when listing or searching. Anything here is either enormous,
#: generated, or secret.
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".terraform",
    }
)

#: Read as text or refused. Keeps a 60 MB weights file out of the context.
_MAX_READ_BYTES = 512_000


class WorkspaceError(Exception):
    """A request the workspace refuses. The message is shown to the model."""


@dataclass
class Workspace:
    """Filesystem access scoped to one directory."""

    root: Path
    #: Files read this session. An edit to anything not in here is refused.
    seen: set[Path] = field(default_factory=set)

    @classmethod
    def open(cls, root: Path) -> Workspace:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise WorkspaceError(f"{root} is not a directory")
        return cls(root=resolved)

    # --- path handling ----------------------------------------------------

    def resolve(self, path: str) -> Path:
        """Resolve ``path`` inside the workspace, or refuse."""
        candidate = (self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceError(
                f"{path} is outside the workspace ({self.root}). "
                "The agent may only touch files under the directory it was started in."
            )
        return candidate

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:  # pragma: no cover - resolve() already guarantees this
            return str(path)

    # --- reading ----------------------------------------------------------

    def read(self, path: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"{path} does not exist or is not a file.")
        if target.stat().st_size > _MAX_READ_BYTES:
            raise WorkspaceError(
                f"{path} is larger than {_MAX_READ_BYTES // 1000} kB. "
                "Use grep to find the relevant region instead of reading it whole."
            )
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{path} is not UTF-8 text.") from exc

        self.seen.add(target)
        return content

    def require_seen(self, target: Path, path: str) -> None:
        if target not in self.seen:
            raise WorkspaceError(
                f"{path} has not been read in this session. Read it first so the "
                "edit is built from what the file actually contains."
            )

    # --- writing ----------------------------------------------------------

    def write(self, path: str, content: str) -> Path:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        # A file the agent just wrote counts as seen: it knows the contents.
        self.seen.add(target)
        return target

    # --- walking ----------------------------------------------------------

    def walk(self) -> list[Path]:
        """Every readable file under the root, minus the noise directories."""
        found: list[Path] = []
        for entry in sorted(self.root.rglob("*")):
            if entry.is_dir():
                continue
            if _IGNORED_DIRECTORIES & set(entry.relative_to(self.root).parts):
                continue
            found.append(entry)
        return found

    # --- git --------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

    @property
    def is_git_repo(self) -> bool:
        return self._git("rev-parse", "--git-dir").returncode == 0

    def is_dirty(self) -> bool:
        result = self._git("status", "--porcelain")
        return bool(result.stdout.strip())

    def diff_stat(self) -> str:
        """``git diff --stat`` over the working tree, for the end-of-turn summary."""
        result = self._git("diff", "--stat")
        return result.stdout.strip()
