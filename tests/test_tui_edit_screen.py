"""Screen-level tests for editing an existing note from browse (lode-0wj.6).

Drives the real widgets end to end via Textual's ``run_test`` pilot, the same
style ``tests/test_tui_browse_screen.py`` / ``tests/test_tui_capture_confirm.py``
use: reaching the edit screen via browse's row-select (lode-olmi.2 -- row-select
opens the editor directly, retiring the earlier separate ``e`` binding), saving
appends a version onto the existing note (never a new note), Escape returns to
the list, the confirm-on-unsaved guard applies -- but with "changed from the
loaded buffer" as the dirty check, not "non-empty" (a freshly loaded existing
version is never empty).
"""

import asyncio
import sqlite3
from pathlib import Path

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.browse import TABLE_ID, BrowseScreen
from lode.tui.screens.discard_confirm import CONFIRM_MESSAGE_ID, DiscardConfirmScreen
from lode.tui.screens.edit import EDIT_BODY_ID, EditScreen
from lode.tui.screens.reconcile import DIFF_ID, ReconcileScreen
from lode.versions import save


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_row_select_on_a_highlighted_row_opens_it_editable(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "the note body to edit")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            return app.screen.query_one(f"#{EDIT_BODY_ID}").text

    body = asyncio.run(_drive())

    assert body == "the note body to edit"


def test_saving_an_edit_appends_a_version_not_a_new_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "edited body"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)

    asyncio.run(_drive())

    assert _rows(
        db_path,
        "SELECT note_id, body, op FROM versions WHERE note_id = ? ORDER BY rowid",
        ("note-a",),
    ) == [
        ("note-a", "original body", "create"),
        ("note-a", "edited body", "update"),
    ]


def test_escape_on_unchanged_buffer_returns_to_the_list_without_confirm(
    tmp_path: Path,
) -> None:
    """The core lode-0wj.6 distinction: unchanged != empty, so no confirm pops up."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "untouched body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("escape")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    back_to_list = asyncio.run(_drive())

    assert back_to_list
    # No version was written -- the buffer was never touched.
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", ("note-a",)
    ) == [("untouched body",)]


def test_escape_on_a_dirty_buffer_shows_the_confirm_dialog(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "a real edit"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DiscardConfirmScreen)
            return str(app.screen.query_one(f"#{CONFIRM_MESSAGE_ID}").content)

    message = asyncio.run(_drive())

    assert "ave" in message
    assert "iscard" in message
    assert "ancel" in message


def test_confirm_discard_returns_to_the_list_without_saving(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "discarded edit"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    back_to_list = asyncio.run(_drive())

    assert back_to_list
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", ("note-a",)
    ) == [("original body",)]


def test_confirm_save_saves_and_returns_to_the_list(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "saved via the confirm dialog"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    back_to_list = asyncio.run(_drive())

    assert back_to_list
    assert _rows(
        db_path,
        "SELECT body, op FROM versions WHERE note_id = ? ORDER BY rowid",
        ("note-a",),
    ) == [("original body", "create"), ("saved via the confirm dialog", "update")]


def test_confirm_cancel_returns_to_editing_with_buffer_intact(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "do not lose this"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            return app.screen.query_one(f"#{EDIT_BODY_ID}").text, app.is_running

    body_text, still_running = asyncio.run(_drive())

    assert body_text == "do not lose this"
    assert still_running


def test_saving_an_empty_edit_is_refused_without_leaving_the_screen(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "   "
            await pilot.press("ctrl+s")
            await pilot.pause()
            return isinstance(app.screen, EditScreen)

    still_editing = asyncio.run(_drive())

    assert still_editing
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", ("note-a",)
    ) == [("original body",)]


def test_browse_list_reflects_the_new_version_after_returning_from_edit(
    tmp_path: Path,
) -> None:
    """The table is reloaded on return -- an edit's Version/Summary aren't stale."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, str]:
        from textual.widgets import DataTable

        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "edited body"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row = table.get_row_at(0)
            return row[2], row[3]

    version, summary = asyncio.run(_drive())

    assert version == "v2"
    assert summary == "edited body"


def test_cas_reject_on_save_shows_reconcile_then_returns_to_the_list(
    tmp_path: Path,
) -> None:
    """A concurrent edit moves the head; resolving reconcile lands back on browse.

    Unlike capture's use of ``ReconcileScreen`` (which ends the whole app on
    resolve, ``tests/test_tui_reconcile_screen.py``), this screen is pushed
    on top of ``BrowseScreen`` -- resolving it pops both ``ReconcileScreen``
    and this ``EditScreen``, landing back on the note list rather than
    exiting the session.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        original_head = save(conn, "note-a", "original body").version_id
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            # A concurrent process saves onto this note while it's being
            # edited, moving the live head out from under the loaded parent.
            conn = init_db(db_path)
            try:
                save(
                    conn,
                    "note-a",
                    "someone else's concurrent edit",
                    parent=original_head,
                )
            finally:
                conn.close()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "my conflicting edit"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ReconcileScreen)
            diff_text = app.screen.query_one(f"#{DIFF_ID}").text
            assert "someone else's concurrent edit" in diff_text
            assert "my conflicting edit" in diff_text
            await pilot.press("d")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    back_to_list = asyncio.run(_drive())

    assert back_to_list
    # Discard: the concurrent edit's head is untouched, no clobber -- the
    # rejected buffer never became a third version.
    assert _rows(
        db_path,
        "SELECT body FROM versions WHERE note_id = ? ORDER BY rowid",
        ("note-a",),
    ) == [("original body",), ("someone else's concurrent edit",)]
