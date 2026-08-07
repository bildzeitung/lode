"""Screen-level tests for the Tags screen (lode-olmi.6, multi-column grid lode-l38d.9).

Drives the real widgets end to end via Textual's ``run_test`` pilot, the same
style ``tests/test_tui_browse_screen.py`` uses: reaching the screen from
capture via the app-level ``Ctrl+T`` binding (originally the function key
``F5``, itself a land-time rekey off the function key ``F4``, which sibling
``lode-olmi.9`` claimed at Screen level first as ``Ctrl+F`` -- see
``docs/keybindings.md`` -- then remapped off function keys entirely by
lode-juz8.1's no-function-key policy), the tag grid's contents, the
AND/intersection notes filter, selecting a note to open its editor, the
"tags -> capture" Escape chain, and (lode-l38d.9) the multi-column reflow on
resize and selection surviving a reflow that moves selected tags to
different grid coordinates -- the ticket's own called-out main regression
risk.

Most tests pin a narrow terminal width (``size=(20, 24)``) so the tag grid
lays out as a single column, one tag per row -- the same shape the old
``SelectionList`` had, keeping keyboard-navigation assertions simple and
deterministic regardless of border/padding pixel details. The reflow tests
below pin a *wide* terminal instead specifically to exercise multiple
columns.
"""

import asyncio
import io
import json
from pathlib import Path

from rich.console import Console
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.edit import EDIT_BODY_ID, EditScreen
from lode.tui.screens.tags import NOTES_TABLE_ID, TAG_LIST_ID, TagsScreen
from lode.versions import save

#: A narrow terminal forces the tag grid to a single column regardless of
#: border/padding overhead -- ``_tag_grid_layout``'s ``max(1, ...)`` floor
#: guarantees at least one column even when nothing else fits.
_NARROW = (18, 24)
#: A wide terminal, used only by the reflow tests below to force multiple
#: columns for short tag names.
_WIDE = (60, 24)


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
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, TagsScreen)
            tag_list = app.screen.query_one(f"#{TAG_LIST_ID}", DataTable)
            # Single column at this width -- one tag per row, sorted.
            return [
                str(tag_list.get_cell_at(Coordinate(i, 0)))
                for i in range(tag_list.row_count)
            ]

    cells = asyncio.run(_drive())

    assert cells == ["[ ] prod", "[ ] staging"]


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
        async with app.run_test(size=_NARROW) as pilot:
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
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            # Single column, sorted: row 0 "prod", row 1 "staging". The
            # cursor starts on row 0 (DataTable's cursor_coordinate defaults
            # to (0, 0)), so no initial "down" is needed before the first
            # toggle -- unlike the old SelectionList, which highlighted
            # nothing until the first cursor move.
            await pilot.press("space")  # select "prod"
            await pilot.press("down")
            await pilot.press("space")  # select "staging" too
            await pilot.pause()
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["has both tags"]


def test_and_intersection_empty_result_shows_an_explanatory_message(
    tmp_path: Path,
) -> None:
    """lode-35nu.7: two tags that legitimately never co-occur is a correct,
    non-buggy outcome of AND/intersection -- the empty table now explains
    that instead of just going blank."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_a = save(conn, "note-a", "only prod").version_id
        head_b = save(conn, "note-b", "only staging").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head_a, "prod")
    _write_tag(db_path, "note-b", head_b, "staging")
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            await pilot.press("space")  # select "prod"
            await pilot.press("down")
            await pilot.press("space")  # select "staging" too -- no overlap
            await pilot.pause()
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["No notes carry every selected tag together."]


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
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            # Single column, sorted: row 0 "prod", row 1 "staging".
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
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            # Single tag ("staging") at row 0, cursor already there.
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


def test_enter_also_toggles_the_tag_under_the_cursor(tmp_path: Path) -> None:
    """DataTable's own native "enter" -> CellSelected binding toggles too,
    not just the hand-rolled "space" (lode-l38d.9's module docstring)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_a = save(conn, "note-a", "tagged note").version_id
        save(conn, "note-b", "untagged note")
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head_a, "staging")
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            await pilot.press("enter")  # select "staging" via enter, not space
            await pilot.pause()
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["tagged note"]


def test_selecting_a_note_opens_the_editor(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "the note body to edit")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            # Focus the notes table (tag grid has initial focus) and select.
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
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
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
        async with app.run_test(size=_NARROW) as pilot:
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
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            tag_list = app.screen.query_one(f"#{TAG_LIST_ID}", DataTable)
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return tag_list.row_count, table.row_count

    tag_row_count, note_row_count = asyncio.run(_drive())

    assert tag_row_count == 0
    assert note_row_count == 0


def test_tags_reflow_into_multiple_columns_on_a_wide_terminal(tmp_path: Path) -> None:
    """Acceptance: tags render in as many columns as the terminal width
    allows -- three short tags all fit on one grid row once the terminal is
    wide enough, instead of the old SelectionList's forced single column."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "a note").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head, "aaa")
    _write_tag(db_path, "note-a", head, "bbb")
    _write_tag(db_path, "note-a", head, "ccc")
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, int]:
        async with app.run_test(size=_WIDE) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            table = app.screen.query_one(f"#{TAG_LIST_ID}", DataTable)
            return table.row_count, len(table.columns)

    row_count, column_count = asyncio.run(_drive())

    # All three short tags fit on a single grid row once >1 column is
    # available -- the concrete, terminal-width-dependent proof of reflow.
    assert row_count == 1
    assert column_count >= 3


def test_tag_grid_reflows_and_selection_survives_a_resize(tmp_path: Path) -> None:
    """The ticket's main regression risk: selecting tags that land on
    DIFFERENT rows/columns of the grid, then resizing so they land on the
    SAME row (fewer columns -> more; more rows -> fewer) must not reset the
    selection or the AND-narrowed notes table (lode-l38d.9)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_both = save(conn, "note-both", "has aaa and ccc").version_id
        head_one = save(conn, "note-one", "has only aaa").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-both", head_both, "aaa")
    _write_tag(db_path, "note-both", head_both, "ccc")
    _write_tag(db_path, "note-one", head_one, "aaa")
    _write_tag(db_path, "note-one", head_one, "bbb")
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, int, list[str], int, int, list[str], list[str]]:
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            tag_list = app.screen.query_one(f"#{TAG_LIST_ID}", DataTable)
            narrow_rows = tag_list.row_count
            narrow_columns = len(tag_list.columns)
            # Single column, sorted: row 0 "aaa", row 1 "bbb", row 2 "ccc" --
            # select "aaa" (row 0) and "ccc" (row 2): two DIFFERENT rows.
            await pilot.press("space")  # select "aaa"
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("space")  # select "ccc"
            await pilot.pause()
            notes_table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            before_resize = [
                str(notes_table.get_row_at(i)[3]) for i in range(notes_table.row_count)
            ]

            await pilot.resize_terminal(*_WIDE)
            await pilot.pause()
            tag_list = app.screen.query_one(f"#{TAG_LIST_ID}", DataTable)
            wide_rows = tag_list.row_count
            wide_columns = len(tag_list.columns)
            notes_table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            after_resize = [
                str(notes_table.get_row_at(i)[3]) for i in range(notes_table.row_count)
            ]
            checkboxes = [
                str(tag_list.get_cell_at(Coordinate(0, c))) for c in range(wide_columns)
            ]
            return (
                narrow_rows,
                narrow_columns,
                before_resize,
                wide_rows,
                wide_columns,
                after_resize,
                checkboxes,
            )

    (
        narrow_rows,
        narrow_columns,
        before_resize,
        wide_rows,
        wide_columns,
        after_resize,
        checkboxes,
    ) = asyncio.run(_drive())

    # Narrow: one tag per row. Wide: reflows to one row, several columns --
    # "aaa" and "ccc" moved from rows 0/2 (column 0) to row 0, columns 0/2.
    assert narrow_rows == 3
    assert narrow_columns == 1
    assert wide_rows == 1
    assert wide_columns >= 3

    # AND semantics unaffected by the resize -- the only note tagged with
    # BOTH "aaa" and "ccc", before and after the reflow.
    assert before_resize == ["has aaa and ccc"]
    assert after_resize == ["has aaa and ccc"]

    # And the grid itself visibly reflects the still-selected checkboxes at
    # their new coordinates, not a reset-to-unselected redraw.
    assert checkboxes[:3] == ["[x] aaa", "[ ] bbb", "[x] ccc"]


def test_selected_checkbox_renders_literally_not_as_rich_markup(tmp_path: Path) -> None:
    """A selected tag draws ``[x] tag`` on screen, not a markup-eaten ``  tag``.

    The regression this guards (lode-7abi): cells were plain ``str``, which
    Textual renders through Rich *console markup* -- ``[x]`` was consumed as a
    style tag and the checkbox vanished, while ``[ ]`` survived only because
    the space makes it an invalid tag. The other tests here compare
    ``get_cell_at`` against ``"[x] ..."`` and pass either way: that returns the
    *stored* value, never the render. This one goes through a Rich console, so
    it fails on a markup-parsed cell.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "about rrsp").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head, "rrsp")
    app = LodeApp(db_path=db_path)

    async def _drive() -> object:
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            await pilot.press("space")  # select the only tag
            tag_list = app.screen.query_one(f"#{TAG_LIST_ID}", DataTable)
            return tag_list.get_cell_at(Coordinate(0, 0))

    cell = asyncio.run(_drive())

    buffer = io.StringIO()
    Console(file=buffer, width=40, legacy_windows=False).print(cell)
    assert buffer.getvalue().strip() == "[x] rrsp"


def test_note_summary_with_brackets_renders_literally_in_the_notes_table(
    tmp_path: Path,
) -> None:
    """A note summary containing ``[...]`` renders literally (lode-ix4i).

    Same hazard as the checkbox cell above, at a different seam:
    ``_reload_notes`` fed ``row.summary`` to ``add_row`` as a bare ``str``,
    50 lines below :meth:`~lode.tui.screens.tags.TagsScreen._tag_cell_text`'s
    own docstring explaining exactly this. Goes through a real Rich console,
    not ``get_cell_at``, for the same "stored value, not the render" reason
    the checkbox test above documents.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "reviewed [draft] spec")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> object:
        async with app.run_test(size=_NARROW) as pilot:
            await pilot.press("ctrl+t")
            table = app.screen.query_one(f"#{NOTES_TABLE_ID}", DataTable)
            return table.get_row_at(0)[3]

    cell = asyncio.run(_drive())

    buffer = io.StringIO()
    Console(file=buffer, width=40, legacy_windows=False).print(cell)
    assert buffer.getvalue().strip() == "reviewed [draft] spec"
