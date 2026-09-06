"""Saved sessions.

The round trip is the easy part. The assertion that matters is the one about
``seen``: restoring it verbatim would let the model edit a file it has not read
in this process, whose contents may have changed since - laundering a stale
memory into a fresh permission, which is the exact case the read-before-edit
rule exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_assistant_agent.store import SessionStore, new_id, restore_seen
from llm_assistant_agent.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "b.py").write_text("y = 1\n", encoding="utf-8")
    return Workspace.open(root)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


def _save(store: SessionStore, workspace: Workspace, messages: list[dict[str, object]]) -> str:
    session_id = new_id()
    store.save(session_id, workspace=workspace, model="test-model", messages=messages)
    return session_id


# --- round trip ------------------------------------------------------------


def test_a_session_comes_back_with_its_transcript(
    store: SessionStore, workspace: Workspace
) -> None:
    messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

    stored = store.load(_save(store, workspace, messages))

    assert stored is not None
    assert stored.messages == messages
    assert stored.workspace == workspace.root
    assert stored.model == "test-model"


def test_the_listing_is_newest_first_and_scoped_to_one_workspace(
    store: SessionStore, workspace: Workspace, tmp_path: Path
) -> None:
    older = _save(store, workspace, [{"role": "user", "content": "first"}])
    newer = _save(store, workspace, [{"role": "user", "content": "second"}])
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    _save(store, Workspace.open(elsewhere), [{"role": "user", "content": "unrelated"}])

    listed = [s.id for s in store.listing(workspace.root)]

    assert listed == [newer, older]


def test_the_summary_strips_the_rules_riding_on_the_first_turn(
    store: SessionStore, workspace: Workspace
) -> None:
    stored = store.load(
        _save(store, workspace, [{"role": "user", "content": "THE RULES\n\n---\n\nfix the bug"}])
    )

    assert stored is not None
    assert stored.summary == "fix the bug"


def test_latest_is_the_most_recent_for_this_workspace(
    store: SessionStore, workspace: Workspace
) -> None:
    _save(store, workspace, [{"role": "user", "content": "first"}])
    newest = _save(store, workspace, [{"role": "user", "content": "second"}])

    latest = store.latest(workspace.root)

    assert latest is not None
    assert latest.id == newest


# --- read-before-edit survives the round trip ------------------------------


def test_an_unchanged_file_stays_editable_on_resume(
    store: SessionStore, workspace: Workspace
) -> None:
    workspace.read("a.py")
    stored = store.load(_save(store, workspace, []))
    assert stored is not None

    fresh = Workspace.open(workspace.root)
    restored, stale = restore_seen(stored, fresh)

    assert (restored, stale) == (1, 0)
    assert fresh.resolve("a.py") in fresh.seen


def test_a_file_that_changed_since_must_be_read_again(
    store: SessionStore, workspace: Workspace
) -> None:
    """The safety property: a stale memory must not become a fresh permission."""
    workspace.read("a.py")
    stored = store.load(_save(store, workspace, []))
    assert stored is not None
    (workspace.root / "a.py").write_text("x = 999\n", encoding="utf-8")

    fresh = Workspace.open(workspace.root)
    restored, stale = restore_seen(stored, fresh)

    assert (restored, stale) == (0, 1)
    assert fresh.resolve("a.py") not in fresh.seen


def test_a_file_deleted_since_is_simply_stale(store: SessionStore, workspace: Workspace) -> None:
    workspace.read("a.py")
    stored = store.load(_save(store, workspace, []))
    assert stored is not None
    (workspace.root / "a.py").unlink()

    assert restore_seen(stored, Workspace.open(workspace.root)) == (0, 1)


# --- bad files -------------------------------------------------------------


def test_an_unreadable_session_is_skipped_not_raised(store: SessionStore) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "20990101-000000-dead.json").write_text("not json at all", encoding="utf-8")

    assert store.listing() == []
    assert store.load("20990101-000000-dead") is None


def test_a_session_from_another_version_is_skipped(store: SessionStore) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "20990101-000000-beef.json").write_text(
        json.dumps({"version": 99, "id": "x"}), encoding="utf-8"
    )

    assert store.listing() == []


def test_old_sessions_are_pruned(
    store: SessionStore, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("llm_assistant_agent.store._KEEP", 3)

    for _ in range(5):
        _save(store, workspace, [{"role": "user", "content": "x"}])

    assert len(list(store.root.glob("*.json"))) == 3


def test_a_save_leaves_no_partial_file_behind(store: SessionStore, workspace: Workspace) -> None:
    _save(store, workspace, [{"role": "user", "content": "x"}])

    assert list(store.root.glob("*.tmp")) == []


# --- deleting --------------------------------------------------------------


def test_deleting_removes_just_that_session(store: SessionStore, workspace: Workspace) -> None:
    doomed = _save(store, workspace, [{"role": "user", "content": "go away"}])
    kept = _save(store, workspace, [{"role": "user", "content": "stay"}])

    assert store.delete(doomed) is True

    assert store.load(doomed) is None
    assert store.load(kept) is not None


def test_deleting_something_that_is_not_there_is_false_not_an_error(
    store: SessionStore, workspace: Workspace
) -> None:
    _save(store, workspace, [])

    assert store.delete("20990101-000000-none") is False


def test_delete_all_is_scoped_to_one_workspace(
    store: SessionStore, workspace: Workspace, tmp_path: Path
) -> None:
    """Sessions elsewhere are work the caller is not looking at; deleting them
    from the wrong directory would be a surprising way to lose a transcript."""
    here = _save(store, workspace, [{"role": "user", "content": "mine"}])
    elsewhere_root = tmp_path / "other"
    elsewhere_root.mkdir()
    elsewhere = _save(
        store, Workspace.open(elsewhere_root), [{"role": "user", "content": "theirs"}]
    )

    removed = store.delete_all(workspace.root)

    assert removed == [here]
    assert store.load(here) is None
    assert store.load(elsewhere) is not None


def test_delete_all_without_a_workspace_clears_everything(
    store: SessionStore, workspace: Workspace, tmp_path: Path
) -> None:
    _save(store, workspace, [])
    elsewhere_root = tmp_path / "other"
    elsewhere_root.mkdir()
    _save(store, Workspace.open(elsewhere_root), [])

    assert len(store.delete_all()) == 2
    assert store.listing() == []


def test_delete_all_beyond_the_listing_page_size(store: SessionStore, workspace: Workspace) -> None:
    """listing() pages at 20 by default - deleting must not stop there."""
    for _ in range(25):
        _save(store, workspace, [])

    assert len(store.delete_all(workspace.root)) == 25
    assert store.listing(workspace.root) == []


@pytest.mark.parametrize(
    "hostile",
    ["../../../etc/passwd", "..", ".", "", "a/b", "/etc/passwd", "../secret"],
)
def test_an_id_cannot_escape_the_sessions_directory(
    store: SessionStore, workspace: Workspace, tmp_path: Path, hostile: str
) -> None:
    """Ids come from the command line and are turned into paths - and this one
    unlinks what it finds."""
    _save(store, workspace, [])
    bystander = tmp_path / "secret.json"
    bystander.write_text("precious", encoding="utf-8")

    assert store.delete(hostile) is False
    assert store.load(hostile) is None

    assert bystander.exists()
