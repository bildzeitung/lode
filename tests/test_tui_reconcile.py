"""Tests for lode.tui.reconcile — the TUI's CAS-conflict draft store (lode-mkc.4).

Pins the ticket's acceptance criterion at the module level: a CAS-rejected
save's buffer is preserved as a draft, re-apply re-parents it onto the new
head through the same ``Repository.save`` CAS path any other save uses (and
a renewed conflict on re-apply comes back as a fresh, still-preserved
``Conflict`` rather than being silently retried or dropped), and discard
removes the draft without touching the live head. Mirrors
``tests/test_tui_capture.py``'s direct unit style.
"""

import sqlite3
from pathlib import Path

from lode.storage import init_db
from lode.tui.reconcile import (
    Conflict,
    conflict_from_error,
    discard,
    reapply,
    write_draft,
)
from lode.versions import HeadConflictError, SaveResult, save


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_conflict_from_error_writes_draft_with_rejected_buffer(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    error = HeadConflictError(
        "note-1",
        "stale-parent",
        "live-head",
        actual_head_body="live body",
        rejected_buffer="my unsaved edit",
    )

    conflict = conflict_from_error(db_path, error)

    assert conflict.note_id == "note-1"
    assert conflict.rejected_buffer == "my unsaved edit"
    assert conflict.actual_head == "live-head"
    assert conflict.actual_head_body == "live body"
    assert conflict.draft_path.read_text(encoding="utf-8") == "my unsaved edit"


def test_reapply_reparents_buffer_onto_new_head_and_discards_draft(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "original body").version_id
    finally:
        conn.close()
    draft_path = write_draft(db_path, "note-1", "the edit that got rejected")
    conflict = Conflict(
        note_id="note-1",
        expected_parent="some-stale-parent",
        rejected_buffer="the edit that got rejected",
        actual_head=head,
        actual_head_body="original body",
        draft_path=draft_path,
    )

    result = reapply(db_path, conflict)

    assert isinstance(result, SaveResult)
    assert result.op == "update"
    assert not draft_path.exists()
    assert _rows(
        db_path,
        "SELECT body FROM versions WHERE note_id = ? ORDER BY created, rowid",
        ("note-1",),
    ) == [("original body",), ("the edit that got rejected",)]


def test_reapply_returns_fresh_conflict_on_renewed_cas_loss(tmp_path: Path) -> None:
    """The head moves again while the reconcile screen is up: no auto-retry."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head1 = save(conn, "note-1", "v1").version_id
        # The head moves again after the conflict was built but before re-apply.
        save(conn, "note-1", "v3", parent=head1)
    finally:
        conn.close()
    draft_path = write_draft(db_path, "note-1", "v2 edit")
    stale_conflict = Conflict(
        note_id="note-1",
        expected_parent="whatever",
        rejected_buffer="v2 edit",
        actual_head=head1,
        actual_head_body="v1",
        draft_path=draft_path,
    )

    result = reapply(db_path, stale_conflict)

    assert isinstance(result, Conflict)
    assert result.actual_head_body == "v3"
    assert result.rejected_buffer == "v2 edit"
    assert result.draft_path.read_text(encoding="utf-8") == "v2 edit"
    # The superseded draft is dropped, not accumulated, on a renewed conflict.
    assert not draft_path.exists()
    assert result.draft_path != draft_path
    # The live head is untouched by the failed re-apply attempt — no clobber.
    assert _rows(
        db_path,
        "SELECT body FROM versions WHERE note_id = ? ORDER BY created, rowid",
        ("note-1",),
    ) == [("v1",), ("v3",)]


def test_discard_removes_draft_without_touching_head(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "original body").version_id
    finally:
        conn.close()
    draft_path = write_draft(db_path, "note-1", "an edit the user decided to drop")
    conflict = Conflict(
        note_id="note-1",
        expected_parent="stale",
        rejected_buffer="an edit the user decided to drop",
        actual_head=head,
        actual_head_body="original body",
        draft_path=draft_path,
    )

    discard(conflict)

    assert not draft_path.exists()
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", ("note-1",)
    ) == [("original body",)]
