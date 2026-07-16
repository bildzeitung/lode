"""Screen-level tests for the Tags screen (lode-olmi.6).

Drives the real widgets end to end via Textual's ``run_test`` pilot, the same
style ``tests/test_tui_browse_screen.py`` uses: reaching the screen from
capture via the app-level ``Ctrl+T`` binding (originally the function key
``F5``, itself a land-time rekey off the function key ``F4``, which sibling
``lode-olmi.9`` claimed at Screen level first as ``Ctrl+F`` -- see
``docs/keybindings.md`` -- then remapped off function keys entirely by
lode-juz8.1's no-function-key policy), the tag multi-select's contents,
the AND/intersection notes filter, selecting a note to open its editor, and
the "tags -> capture" Escape chain.
"""

import asyncio
import json
from pathlib import Path

from textual.widgets import DataTable, SelectionList

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.browse import EDIT_BODY_ID, EditScreen
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.tags import NOTES_TABLE_ID, TAG_LIST_ID, TagsScreen
from lode.versions import save


def _write_tag(db_path: Path, note_id: str, version_id: str, tag: str) -> None:
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'tag', ?, 'ai', 'fresh')",
            (note_id, version_id, json.dumps(tag)),
        )
        conn.commit()
    finally:
        conn.close()


def test_app_registers_tags_screen(tmp_path: Path) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["tags"] is TagsScreen


def test_ctrl_t_reaches_the_tags_screen_with_every_tag_listed(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_a = save(conn, "note-a", "about staging").version_id
        head_b = save(conn, "note-b", "about prod").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head_a, "staging")
    _write_tag(db_path, "note-b", head_b, "prod")
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, TagsScreen)
            tag_list = app.screen.query_one(f"#{TAG_LIST_ID}", SelectionList)
            return [str(tag_list.get_option_at_index(i).prompt) for i in range(2)]

    tags = asyncio.run(_drive())

    assert tags == ["prod", "staging"]


def test_no_tag_selected_shows_every_live_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "first captured note")
        save(conn, "note-b", "second captured note")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["second captured note", "first captured note"]


def test_selecting_a_tag_narrows_notes_by_and_semantics(tmp_path: Path) -> None:
    """Selecting both tags leaves only the note carrying both -- AND, not OR."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_both = save(conn, "note-both", "has both tags").version_id
        head_one = save(conn, "note-one", "has only one tag").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-both", head_both, "prod")
    _write_tag(db_path, "note-both", head_both, "staging")
    _write_tag(db_path, "note-one", head_one, "staging")
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            # Tag options are sorted: "prod" (index 0) then "staging" (index
            # 1). Nothing is highlighted until the first "down" -- pressing
            # it once moves the cursor to index 0, not "next from current".
            await pilot.press("down")
            await pilot.press("space")  # select "prod"
            await pilot.press("down")
            await pilot.press("space")  # select "staging" too
            await pilot.pause()
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["has both tags"]


def test_deselecting_a_tag_widens_the_filter_again(tmp_path: Path) -> None:
    """Selecting prod+staging narrows to the one note with both; deselecting
    "prod" again widens back to every note carrying just "staging"."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_both = save(conn, "note-both", "has both tags").version_id
        head_one = save(conn, "note-one", "has only one tag").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-both", head_both, "prod")
    _write_tag(db_path, "note-both", head_both, "staging")
    _write_tag(db_path, "note-one", head_one, "staging")
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[list[str], list[str]]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            # Tag options sorted: "prod" (index 0), "staging" (index 1).
            await pilot.press("down")
            await pilot.press("space")  # select "prod"
            await pilot.press("down")
            await pilot.press("space")  # select "staging" too
            await pilot.pause()
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            narrowed = [str(table.get_row_at(i)[3]) for i in range(table.row_count)]
            # Move back up to "prod" and deselect it, leaving only "staging".
            await pilot.press("up")
            await pilot.press("space")
            await pilot.pause()
            widened = [str(table.get_row_at(i)[3]) for i in range(table.row_count)]
            return narrowed, widened

    narrowed, widened = asyncio.run(_drive())

    assert narrowed == ["has both tags"]
    assert sorted(widened) == ["has both tags", "has only one tag"]


def test_clearing_all_selected_tags_shows_every_note_again(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_a = save(conn, "note-a", "tagged note").version_id
        save(conn, "note-b", "untagged note")
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head_a, "staging")
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[list[str], list[str]]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            # Nothing is highlighted until the first "down" moves the cursor
            # to index 0 -- the only tag option here ("staging").
            await pilot.press("down")
            await pilot.press("space")  # select "staging"
            await pilot.pause()
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            narrowed = [str(table.get_row_at(i)[3]) for i in range(table.row_count)]
            await pilot.press("space")  # deselect it again
            await pilot.pause()
            widened = [str(table.get_row_at(i)[3]) for i in range(table.row_count)]
            return narrowed, widened

    narrowed, widened = asyncio.run(_drive())

    assert narrowed == ["tagged note"]
    assert sorted(widened) == ["tagged note", "untagged note"]


def test_selecting_a_note_opens_the_editor(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "the note body to edit")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            # Focus the notes table (tag list has initial focus) and select.
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            from textual.widgets import TextArea

            return app.screen.query_one(f"#{EDIT_BODY_ID}", TextArea).text

    body = asyncio.run(_drive())

    assert body == "the note body to edit"


def test_tag_selection_survives_a_round_trip_to_the_editor(tmp_path: Path) -> None:
    """Escaping back from the pushed EditScreen keeps the tag filter applied."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_both = save(conn, "note-both", "has both tags").version_id
        head_one = save(conn, "note-one", "has only one tag").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-both", head_both, "prod")
    _write_tag(db_path, "note-both", head_both, "staging")
    _write_tag(db_path, "note-one", head_one, "staging")
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("space")  # select "prod"
            await pilot.press("down")
            await pilot.press("space")  # select "staging"
            await pilot.pause()
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            table.focus()
            await pilot.press("enter")  # opens EditScreen on "note-both"
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("escape")  # unchanged -- pops straight back
            await pilot.pause()
            assert isinstance(app.screen, TagsScreen)
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["has both tags"]


def test_escape_from_tags_screen_returns_to_capture(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, TagsScreen)
            await pilot.press("escape")
            return isinstance(app.screen, CaptureScreen)

    back_to_capture = asyncio.run(_drive())

    assert back_to_capture


def test_empty_tag_list_and_notes_table_is_not_a_crash(tmp_path: Path) -> None:
    """No notes at all -- both panels render empty, no crash."""
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, int]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            tag_list = app.screen.query_one(f"#{TAG_LIST_ID}", SelectionList)
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return tag_list.option_count, table.row_count

    tag_count, row_count = asyncio.run(_drive())

    assert tag_count == 0
    assert row_count == 0
