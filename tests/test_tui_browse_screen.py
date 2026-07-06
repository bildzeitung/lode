"""Screen-level tests for the browse screen (lode-0wj.5).

Drives the real widgets end to end via Textual's ``run_test`` pilot, the same
style ``tests/test_tui_config.py`` / ``tests/test_tui_ask_screen.py`` use:
reaching the screen from capture via the app-level ``F3`` binding, the table's
contents/ordering, selecting a row to open a read-only note view, and the
"note -> list -> capture" Escape chain.
"""

import asyncio
from pathlib import Path

from textual.widgets import DataTable, TextArea

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.browse import (
    HISTORY_TABLE_ID,
    NOTE_BODY_ID,
    TABLE_ID,
    VERSION_BODY_ID,
    BrowseScreen,
    NoteViewScreen,
    VersionHistoryScreen,
    VersionViewScreen,
)
from lode.tui.screens.capture import CaptureScreen
from lode.versions import save


def test_app_registers_browse_screen(tmp_path: Path) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["browse"] is BrowseScreen


def test_f3_reaches_the_browse_screen_with_notes_newest_first(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "first captured note")
        save(conn, "note-b", "second captured note")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[tuple]:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            assert isinstance(app.screen, BrowseScreen)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return [tuple(table.get_row_at(i)) for i in range(table.row_count)]

    rows = asyncio.run(_drive())

    assert len(rows) == 2
    assert rows[0][2] == "second captured note"  # newest-first
    assert rows[1][2] == "first captured note"
    assert rows[0][1] == "v1"


def test_selecting_a_row_opens_a_read_only_note_view(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "the note body to view")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NoteViewScreen)
            return app.screen.query_one(f"#{NOTE_BODY_ID}", TextArea).text

    body = asyncio.run(_drive())

    assert body == "the note body to view"


def test_escape_steps_back_note_then_list_then_capture(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "a note")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool]:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NoteViewScreen)
            await pilot.press("escape")
            back_to_list = isinstance(app.screen, BrowseScreen)
            await pilot.press("escape")
            back_to_capture = isinstance(app.screen, CaptureScreen)
            return back_to_list, back_to_capture

    back_to_list, back_to_capture = asyncio.run(_drive())

    assert back_to_list
    assert back_to_capture


def test_deleted_note_does_not_appear_in_the_browse_list(
    tmp_path: Path,
) -> None:
    from lode.versions import delete

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "kept-note", "still here")
        gone_head = save(conn, "gone-note", "will be deleted").version_id
        delete(conn, "gone-note", parent=gone_head)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[2]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["still here"]


def test_h_from_note_view_opens_version_history_newest_first(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "v1 body").version_id
        save(conn, "note-a", "v2 body", parent=head)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[tuple]:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NoteViewScreen)
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen, VersionHistoryScreen)
            table = app.screen.query_one(f"#{HISTORY_TABLE_ID}", DataTable)
            return [tuple(table.get_row_at(i)) for i in range(table.row_count)]

    rows = asyncio.run(_drive())

    assert len(rows) == 2
    assert rows[0][1] == "v2"
    assert rows[1][1] == "v1"
    assert rows[0][2] == "update"
    assert rows[1][2] == "create"


def test_selecting_a_prior_version_shows_its_body_read_only(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "v1 body").version_id
        save(conn, "note-a", "v2 body", parent=head)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            # Cursor starts on the newest row (v2); move down to the prior (v1).
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, VersionViewScreen)
            return app.screen.query_one(f"#{VERSION_BODY_ID}", TextArea).text

    body = asyncio.run(_drive())

    assert body == "v1 body"


def test_escape_steps_back_version_view_then_history_then_note_view(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "v1 body").version_id
        save(conn, "note-a", "v2 body", parent=head)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool]:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, VersionViewScreen)
            await pilot.press("escape")
            back_to_history = isinstance(app.screen, VersionHistoryScreen)
            await pilot.press("escape")
            back_to_note_view = isinstance(app.screen, NoteViewScreen)
            return back_to_history, back_to_note_view

    back_to_history, back_to_note_view = asyncio.run(_drive())

    assert back_to_history
    assert back_to_note_view


def test_version_history_includes_the_head_row(tmp_path: Path) -> None:
    """A note with a single (root) version still shows one selectable row."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "only version")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> int:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            table = app.screen.query_one(f"#{HISTORY_TABLE_ID}", DataTable)
            return table.row_count

    row_count = asyncio.run(_drive())

    assert row_count == 1
