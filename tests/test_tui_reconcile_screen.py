"""Screen-level tests for the CAS-conflict reconciliation UI (lode-mkc.4).

Drives the real widgets end to end via Textual's ``run_test`` pilot, the same
style ``tests/test_tui_app.py`` uses for the capture screen: force a CAS
reject with the fixed-uuid collision trick from
``tests/test_tui_capture.py``, then assert the reconcile screen shows a
buffer-vs-head diff and that both resolutions (re-apply / discard) work end
to end against the real ``Repository.save`` CAS path.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from lode.hashing import NO_PARENT, content_version_id
from lode.storage import init_db
from lode.tui import capture as capture_mod
from lode.tui.app import LodeApp
from lode.tui.screens.capture import BODY_ID
from lode.tui.screens.reconcile import DIFF_ID, ReconcileScreen
from lode.versions import save


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


class _FixedUUID:
    """Stand-in so ``str(uuid4())`` yields a chosen note id (forces a collision)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


def _seed_collision(db_path: Path, fixed_id: str, original_body: str) -> None:
    conn = init_db(db_path)
    try:
        save(conn, fixed_id, original_body)
    finally:
        conn.close()


def test_cas_reject_shows_a_buffer_vs_head_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    _seed_collision(db_path, fixed_id, "original body")
    monkeypatch.setattr(capture_mod.uuid, "uuid4", lambda: _FixedUUID(fixed_id))
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the conflicting edit"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ReconcileScreen)
            diff_text = app.screen.query_one(f"#{DIFF_ID}").text
            assert "original body" in diff_text
            assert "the conflicting edit" in diff_text

    asyncio.run(_drive())


def test_reapply_saves_onto_new_head_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    _seed_collision(db_path, fixed_id, "original body")
    monkeypatch.setattr(capture_mod.uuid, "uuid4", lambda: _FixedUUID(fixed_id))
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the conflicting edit"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("r")

    asyncio.run(_drive())

    assert app.return_value == fixed_id
    assert _rows(
        db_path,
        "SELECT body, op FROM versions WHERE note_id = ? ORDER BY created, rowid",
        (fixed_id,),
    ) == [("original body", "create"), ("the conflicting edit", "update")]
    assert list(tmp_path.glob("*.draft")) == []


def test_discard_leaves_head_untouched_and_removes_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    _seed_collision(db_path, fixed_id, "original body")
    monkeypatch.setattr(capture_mod.uuid, "uuid4", lambda: _FixedUUID(fixed_id))
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the conflicting edit"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("d")

    asyncio.run(_drive())

    assert app.return_value is None
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", (fixed_id,)
    ) == [("original body",)]
    (head,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (fixed_id,)
    )[0]
    assert head == content_version_id(fixed_id, NO_PARENT, "original body")
    assert list(tmp_path.glob("*.draft")) == []
