"""Tests for lode.tui.edit — the TUI edit screen's save wiring (lode-0wj.6).

The direct unit-level twin of ``tests/test_tui_capture.py``: pins that
``save_edit`` appends a *version* onto an existing note's chain via the CAS
head path (never mints a new note), refuses an empty body the same way
capture does, dedups an unchanged save with no new row, and turns a CAS
reject into a preserved-draft ``Conflict`` exactly like the capture path.

**Delete from browse (lode-d32.1).** ``delete_note``'s own unit tests pin
that it writes an ``op='delete'`` tombstone via the same CAS ``parent`` guard
and, critically, that it goes through ``Repository`` -- so the FTS/lexical
cache row for the deleted note is evicted, not just left stale.
"""

import sqlite3
from pathlib import Path

import pytest

from lode.repository import Repository
from lode.storage import init_db
from lode.tui.edit import (
    EditConflict,
    EmptyEditError,
    delete_note,
    load_head,
    save_edit,
)
from lode.versions import HeadConflictError, SaveResult, save


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _seed(db_path: Path, note_id: str, body: str) -> str:
    """Save a note directly and return its head version id."""
    conn = init_db(db_path)
    try:
        return save(conn, note_id, body).version_id
    finally:
        conn.close()


def test_load_head_returns_the_live_head(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    head = _seed(db_path, "note-a", "original body")

    result = load_head(db_path, "note-a")

    assert result == (head, "original body")


def test_load_head_returns_none_for_an_absent_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    assert load_head(db_path, "does-not-exist") is None


def test_load_head_returns_none_for_a_deleted_note(tmp_path: Path) -> None:
    from lode.versions import delete

    db_path = tmp_path / "lode.db"
    head = _seed(db_path, "note-a", "original body")
    conn = init_db(db_path)
    try:
        delete(conn, "note-a", parent=head)
    finally:
        conn.close()

    assert load_head(db_path, "note-a") is None


def test_save_edit_appends_a_new_version_not_a_new_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    head = _seed(db_path, "note-a", "original body")

    result = save_edit(db_path, "note-a", "edited body", parent=head)

    assert isinstance(result, SaveResult)
    assert result.note_id == "note-a"
    assert result.op == "update"
    assert not result.deduped
    assert _rows(
        db_path,
        "SELECT note_id, body, op FROM versions WHERE note_id = ? ORDER BY created",
        ("note-a",),
    ) == [
        ("note-a", "original body", "create"),
        ("note-a", "edited body", "update"),
    ]


def test_save_edit_makes_the_new_version_keyword_findable(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    head = _seed(db_path, "note-a", "original body")

    result = save_edit(
        db_path, "note-a", "staging certificate rotation runbook", parent=head
    )

    conn = sqlite3.connect(db_path)
    try:
        hits = conn.execute(
            "SELECT target_version FROM passages_fts WHERE passages_fts MATCH ?",
            ("rotation",),
        ).fetchall()
    finally:
        conn.close()
    assert (result.version_id,) in hits


def test_save_edit_unchanged_buffer_is_a_no_op_dedup(tmp_path: Path) -> None:
    """The dedup that lets "unchanged" mean something other than "empty"."""
    db_path = tmp_path / "lode.db"
    head = _seed(db_path, "note-a", "original body")

    result = save_edit(db_path, "note-a", "original body", parent=head)

    assert isinstance(result, SaveResult)
    assert result.deduped
    assert result.version_id == head
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", ("note-a",)
    ) == [("original body",)]


def test_save_edit_refuses_empty_body(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    head = _seed(db_path, "note-a", "original body")

    with pytest.raises(EmptyEditError):
        save_edit(db_path, "note-a", "   \n  ", parent=head)

    # Refusal happened before any write -- the original head is untouched.
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", ("note-a",)
    ) == [("original body",)]


def test_save_edit_cas_reject_writes_draft_and_does_not_clobber(tmp_path: Path) -> None:
    """A stale ``parent`` (the head moved since it was loaded) is an honest reject."""
    db_path = tmp_path / "lode.db"
    stale_head = _seed(db_path, "note-a", "original body")
    # Someone else's edit lands first, moving the live head.
    save_edit(db_path, "note-a", "someone else's edit", parent=stale_head)

    result = save_edit(db_path, "note-a", "my conflicting edit", parent=stale_head)

    assert isinstance(result, EditConflict)
    assert result.draft_path.read_text(encoding="utf-8") == "my conflicting edit"
    assert result.actual_head_body == "someone else's edit"
    # No clobber, no auto-merge: the winning edit is untouched.
    assert _rows(
        db_path,
        "SELECT body FROM versions WHERE note_id = ? ORDER BY created",
        ("note-a",),
    ) == [("original body",), ("someone else's edit",)]


def test_delete_note_appends_a_delete_tombstone(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    head = _seed(db_path, "note-a", "original body")

    result = delete_note(db_path, "note-a", parent=head)

    assert isinstance(result, SaveResult)
    assert result.note_id == "note-a"
    assert result.op == "delete"
    assert _rows(
        db_path,
        "SELECT note_id, op FROM versions WHERE note_id = ? ORDER BY created",
        ("note-a",),
    ) == [("note-a", "create"), ("note-a", "delete")]
    assert _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", ("note-a",)
    ) == [(result.version_id,)]


def test_delete_note_cas_reject_raises_head_conflict(tmp_path: Path) -> None:
    """A stale ``parent`` (the head moved since it was loaded) is an honest reject.

    Unlike :func:`save_edit`, :func:`delete_note` has no draft to preserve on a
    reject -- it lets :class:`HeadConflictError` propagate rather than
    converting it to a :class:`~lode.tui.reconcile.Conflict`.
    """
    db_path = tmp_path / "lode.db"
    stale_head = _seed(db_path, "note-a", "original body")
    # Someone else's edit lands first, moving the live head.
    save_edit(db_path, "note-a", "someone else's edit", parent=stale_head)

    with pytest.raises(HeadConflictError):
        delete_note(db_path, "note-a", parent=stale_head)

    # No tombstone was written -- the rejected delete left the chain untouched.
    assert _rows(
        db_path,
        "SELECT body, op FROM versions WHERE note_id = ? ORDER BY created",
        ("note-a",),
    ) == [("original body", "create"), ("someone else's edit", "update")]


def test_delete_note_routes_through_repository_not_versions_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding per the lode-d32 epic's ``/debate``: Repository.delete(), not
    ``versions.delete()`` directly, so the FTS/lexical cache leg is evicted
    along with the tombstone write."""
    db_path = tmp_path / "lode.db"
    head = _seed(db_path, "note-a", "original body")

    calls: list[tuple[str, str]] = []
    original_delete = Repository.delete

    def spy_delete(
        self: Repository, note_id: str, *, parent: str, settings=None
    ) -> SaveResult:
        calls.append((note_id, parent))
        return original_delete(self, note_id, parent=parent, settings=settings)

    monkeypatch.setattr(Repository, "delete", spy_delete)

    result = delete_note(db_path, "note-a", parent=head)

    assert calls == [("note-a", head)]
    assert isinstance(result, SaveResult)
    assert result.op == "delete"
