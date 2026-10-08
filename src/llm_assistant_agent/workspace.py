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

__all__ = ["Workspace", "WorkspaceError", "MultiWorkspace"]

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
    #: Files written since they were last read, so the model is working from
    #: its own recollection of what it wrote rather than from the file.
    written_since_read: set[Path] = field(default_factory=set)
    #: Everything written this session, never cleared. Unlike the set above
    #: this answers "is this change mine?", which a reader of the diff needs.
    written: set[Path] = field(default_factory=set)
    #: Files that already had uncommitted changes when the session opened, so
    #: the diff can say which part of itself is not the agent's work. Without
    #: this the model reads the working tree as a record of what it did: in a
    #: real session it saw 1326 changed lines in a template it had not touched
    #: - left there by the session before it - decided they "weren't part of my
    #: intended changes", and tried three times to git checkout the user's work.
    baseline_dirty: frozenset[str] = frozenset()

    @classmethod
    def open(cls, root: Path) -> Workspace:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise WorkspaceError(f"{root} is not a directory")
        workspace = cls(root=resolved)
        workspace.baseline_dirty = workspace._dirty_paths()
        return workspace

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

    def read(self, path: str, *, partial: bool = False) -> str:
        """The file's text, and a note that it has been seen.

        ``partial`` says the caller is only going to show part of what comes
        back. That still counts as having read the file, but it must not clear
        the "you have written this since you read it" flag: reading ten lines
        at the top says nothing about the edit made at line 500, and clearing
        the flag there would lose the one hint that explains the next miss.
        """
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
        if not partial:
            self.written_since_read.discard(target)
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
        self.written.add(target)
        # Knowing them and being able to reproduce them verbatim are different
        # things, though. After a write the model has only a confirmation, not
        # the new text, and an anchor recalled from before the write is the
        # most common way an edit comes to match nothing at all. Recorded so
        # the failure can say so instead of leaving it to be guessed at.
        self.written_since_read.add(target)
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

    def _dirty_paths(self) -> frozenset[str]:
        """Workspace-relative paths with uncommitted changes, right now."""
        if not self.is_git_repo:
            return frozenset()
        found: set[str] = set()
        for line in self._git("status", "--porcelain").stdout.splitlines():
            # Porcelain v1: two status characters, a space, then the path. A
            # rename reads "old -> new"; the new name is the one on disk.
            name = line[3:].strip()
            if " -> " in name:
                name = name.rpartition(" -> ")[2]
            name = name.strip().strip('"')
            if name:
                found.add(name)
        return frozenset(found)

    def head(self) -> str | None:
        """The current commit, recorded with a session so a resume can tell
        whether the code has moved on underneath it."""
        if not self.is_git_repo:
            return None
        return self._git("rev-parse", "HEAD").stdout.strip() or None

    def diff_stat(self) -> str:
        """``git diff --stat`` over the working tree, for the end-of-turn summary."""
        result = self._git("diff", "--stat")
        return result.stdout.strip()

    def diff(self, path: str | None = None, *, stat: bool = False) -> str:
        """Uncommitted changes, for the model to review its own work.

        Untracked files are included - a new file the agent just wrote is
        exactly what it most needs to see, and plain ``git diff`` would show
        nothing for it.
        """
        if not self.is_git_repo:
            raise WorkspaceError(
                "This workspace is not a git repository, so there is no diff to show."
            )

        arguments = ["diff", "--stat" if stat else "--patch"]
        if path:
            # Resolved first, so the path cannot walk out of the workspace.
            arguments += ["--", self.relative(self.resolve(path))]

        result = self._git(*arguments)
        if result.returncode != 0:
            raise WorkspaceError(f"git diff failed: {result.stderr.strip()}")

        sections = [result.stdout.strip()]
        if not path:
            sections.extend(self._untracked_sections(stat=stat))
        return "\n".join(section for section in sections if section)

    def _untracked_sections(self, *, stat: bool) -> list[str]:
        listing = self._git("ls-files", "--others", "--exclude-standard")
        names = [name for name in listing.stdout.splitlines() if name.strip()]
        if not names:
            return []
        if stat:
            return [f"untracked: {', '.join(names)}"]
        # --no-index against /dev/null renders a new file as an addition,
        # which is the shape the model already knows how to read.
        return [
            self._git("diff", "--no-index", "--", "/dev/null", name).stdout.strip()
            for name in names
        ]


@dataclass
class MultiWorkspace:
    """Manages multiple workspaces (e.g., git worktrees) in one session.

    Allows the agent to work across related repositories simultaneously.
    Each workspace is independent - files must be read in their respective
    workspace before editing.

    Example::

        multi = MultiWorkspace([
            Workspace.open(Path("/repo-main")),
            Workspace.open(Path("/repo-frontend")),
        ])

        # Switch active workspace
        multi.set_active(0)
        multi.active.read("src/main.py")

        # Or access by name
        multi["frontend"].read("package.json")
    """

    workspaces: list[Workspace]
    names: list[str] = field(default_factory=list)
    _active_index: int = 0

    @classmethod
    def open(cls, roots: list[Path], names: list[str] | None = None) -> MultiWorkspace:
        """Create a multi-workspace from multiple roots."""
        workspaces = [Workspace.open(root) for root in roots]
        if names is None:
            names = [root.name for root in roots]
        return cls(workspaces=workspaces, names=names)

    @property
    def active(self) -> Workspace:
        """The currently active workspace."""
        return self.workspaces[self._active_index]

    @property
    def active_name(self) -> str:
        """Name of the active workspace."""
        return self.names[self._active_index] if self.names else str(self._active_index)

    def set_active(self, index: int | str) -> None:
        """Switch the active workspace by index or name."""
        if isinstance(index, str):
            try:
                index = self.names.index(index)
            except ValueError as exc:
                raise ValueError(f"Unknown workspace: {index!r}") from exc
        if not 0 <= index < len(self.workspaces):
            raise IndexError(f"Workspace index {index} out of range")
        self._active_index = index

    def __getitem__(self, key: int | str) -> Workspace:
        """Get a workspace by index or name."""
        if isinstance(key, str):
            try:
                key = self.names.index(key)
            except ValueError as exc:
                raise KeyError(f"Unknown workspace: {key!r}") from exc
        return self.workspaces[key]

    def __len__(self) -> int:
        return len(self.workspaces)

    def list_workspaces(self) -> list[tuple[str, Path]]:
        """List all workspaces as (name, root) pairs."""
        return [
            (name, ws.root) for name, ws in zip(self.names, self.workspaces, strict=False)
        ]
