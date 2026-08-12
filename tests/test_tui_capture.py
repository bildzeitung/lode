"""Tests for lode.tui.services.capture — the TUI capture screen's save wiring (lode-mkc.1).

Pins the ticket's acceptance criterion directly: saving through
``save_capture`` persists the note via the same ``Repository.save`` /
``LexicalCacheBackend`` seam ``lode add`` uses, and never triggers an AI call
— both ``embed`` and ``enrich`` derive jobs stay ``pending`` (unlike ``lode
add``, which opportunistically runs ``enrich`` inline). Also covers the empty
refusal and the (practically unreachable but handled) CAS-reject draft
fallback, mirroring ``tests/test_cli.py``'s equivalent coverage of ``lode add``.
"""

import sqlite3
from pathlib import Path

import pytest

from lode.hashing import NO_PARENT, content_version_id
from lode.storage import init_db
from lode.tui.services import capture as capture_mod
from lode.tui.services.capture import CaptureConflict, EmptyCaptureError, save_capture
from lode.versions import SaveResult, save


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_save_capture_persists_note_via_repository_save(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = save_capture(db_path, "hello world")

    assert isinstance(result, SaveResult)
    assert result.op == "create"
    assert _rows(
        db_path,
        "SELECT note_id, body, op FROM versions WHERE note_id = ?",
        (result.note_id,),
    ) == [(result.note_id, "hello world", "create")]


def test_save_capture_omitted_settings_logs_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """lode-xa5d: ``save_capture`` mints a fresh note row, so it seeds
    ``notes.no_egress`` from ``Settings.no_egress_default`` — an omitted
    ``settings=`` here must be loud, not a silent library-defaults fallback.
    Without this the loud fallback inside ``Repository.save`` can never fire
    for this path: ``save_capture`` would hand it a non-``None`` defaults-only
    ``Settings``, masking the omission.
    """
    with caplog.at_level("WARNING", logger="lode.config"):
        save_capture(tmp_path / "lode.db", "hello world")
    assert any(
        "tui.services.capture.save_capture" in r.getMessage() for r in caplog.records
    )


def test_save_capture_makes_note_keyword_findable_instantly(tmp_path: Path) -> None:
    """The synchronous FTS5 leg runs inline, same as ``lode add`` (lode-xyb)."""
    db_path = tmp_path / "lode.db"
    result = save_capture(db_path, "staging certificate rotation runbook")

    conn = sqlite3.connect(db_path)
    try:
        hits = conn.execute(
            "SELECT target_version FROM passages_fts WHERE passages_fts MATCH ?",
            ("rotation",),
        ).fetchall()
    finally:
        conn.close()
    assert (result.version_id,) in hits


def test_save_capture_never_calls_ai_jobs_stay_pending(tmp_path: Path) -> None:
    """The acceptance criterion: no AI call anywhere in the capture path.

    ``Repository.save`` enqueues both derive jobs atomically (same as ``lode
    add``), but ``save_capture`` — unlike ``lode.cli.add`` — never claims/runs
    the ``enrich`` job inline, so both stay ``pending`` for the async
    ``lode work`` drain rather than one running synchronously here.
    """
    db_path = tmp_path / "lode.db"
    result = save_capture(db_path, "hello world")

    assert _rows(
        db_path,
        "SELECT type, status FROM jobs WHERE target_version = ? ORDER BY type",
        (result.version_id,),
    ) == [("embed", "pending"), ("enrich", "pending")]


def test_save_capture_refuses_empty_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    with pytest.raises(EmptyCaptureError):
        save_capture(db_path, "   \n  ")
    assert not db_path.exists()


class _FixedUUID:
    """Stand-in so ``str(uuid4())`` yields a chosen note id (forces a collision)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


def test_save_capture_cas_reject_writes_draft_and_does_not_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    # Pre-create the note so the minted-id create collides -> HeadConflictError.
    conn = init_db(db_path)
    try:
        save(conn, fixed_id, "original body")
    finally:
        conn.close()
    monkeypatch.setattr(capture_mod.uuid, "uuid4", lambda: _FixedUUID(fixed_id))

    result = save_capture(db_path, "rejected body")

    assert isinstance(result, CaptureConflict)
    assert result.draft_path.read_text(encoding="utf-8") == "rejected body"

    # The original note is untouched (no clobber, no auto-merge).
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", (fixed_id,)
    ) == [("original body",)]
    (head,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (fixed_id,)
    )[0]
    assert head == content_version_id(fixed_id, NO_PARENT, "original body")
