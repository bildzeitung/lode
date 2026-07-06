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

from lode.notes_read import short_note_id
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
    assert rows[0][3] == "second captured note"  # newest-first
    assert rows[1][3] == "first captured note"
    assert rows[0][2] == "v1"


def test_id_column_shows_the_shared_8_char_note_id_prefix(tmp_path: Path) -> None:
    """The Id column (lode-1gr.2) is the shared short id, not the full id."""
    long_note_id = "0123456789abcdef-longer-than-eight-chars"
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, long_note_id, "a note")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return str(table.get_row_at(0)[0])

    id_cell = asyncio.run(_drive())

    assert id_cell == short_note_id(long_note_id)
    assert id_cell == "01234567"
    assert id_cell != long_note_id


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


def test_note_view_screen_shows_the_full_note_id(tmp_path: Path) -> None:
    """The note-view header shows the FULL id (lode-1gr.2), not the short prefix."""
    long_note_id = "0123456789abcdef-longer-than-eight-chars"
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, long_note_id, "the note body to view")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NoteViewScreen)
            return app.screen.sub_title

    sub_title = asyncio.run(_drive())

    assert sub_title == long_note_id


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
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

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


def test_long_summary_wraps_instead_of_scrolling_the_table(tmp_path: Path) -> None:
    """A long Summary wraps down over several lines; the table never scrolls sideways.

    Guards the lode-5qp fix: the Summary column is capped to the room left over
    after Date/Version so a long summary grows the row's height (auto height)
    rather than growing the table past the terminal width and forcing a
    horizontal scroll.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    long_summary = ("wrap me " * 40).strip()  # ~320 chars: wider than any terminal
    try:
        save(conn, "note-a", long_summary)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, int, int, str]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f3")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            return (
                table.rows[row_key].height,
                table.virtual_size.width,
                table.size.width,
                table.get_row_at(0)[3],
            )

    row_height, virtual_width, widget_width, summary_cell = asyncio.run(_drive())

    assert row_height > 1  # the summary wrapped onto multiple lines
    assert virtual_width <= widget_width  # ... so the table needs no h-scroll
    assert summary_cell == long_summary  # the cell keeps the full text, untruncated
