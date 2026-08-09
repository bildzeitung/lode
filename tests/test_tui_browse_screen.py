"""Screen-level tests for the browse screen (lode-0wj.5).

Drives the real widgets end to end via Textual's ``run_test`` pilot, the same
style ``tests/test_tui_config.py`` / ``tests/test_tui_ask_screen.py`` use:
reaching the screen from capture via the app-level ``Ctrl+B`` binding, the table's
contents/ordering, selecting a row to open the editor directly (lode-olmi.2),
and the "edit -> list -> capture" Escape chain.
"""

import asyncio
import io
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import _press_and_settle
from rich.console import Console
from rich.text import Text
from textual.widgets import DataTable, Footer, Header, Input, Static, TextArea
from textual.widgets._footer import FooterKey

from lode.ids import short_version_id
from lode.jobs import now_iso
from lode.lexical import LexicalCacheBackend
from lode.notes_read import short_note_id
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.dates import format_adaptive_date
from lode.tui.screens.browse import (
    QUICK_SEARCH_INPUT_ID,
    SEARCH_INPUT_ID,
    TABLE_ID,
    BrowseScreen,
)
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.delete_confirm import (
    DELETE_CONFIRM_MESSAGE_ID,
    DeleteConfirmScreen,
)
from lode.tui.screens.edit import EDIT_BODY_ID, EditScreen
from lode.tui.screens.enrichment_modal import (
    INSPECTOR_EDGES_ID,
    INSPECTOR_EMBED_ID,
    INSPECTOR_ENTITIES_ID,
    INSPECTOR_STATE_ID,
    INSPECTOR_SUMMARY_ID,
    INSPECTOR_TAGS_ID,
    EnrichmentModalScreen,
)
from lode.tui.screens.external_picker import (
    EXTERNAL_PICKER_TABLE_ID,
    ExternalPickerScreen,
)
from lode.tui.screens.snapshot_viewer import (
    SNAPSHOT_VIEWER_BODY_ID,
    SnapshotViewerScreen,
)
from lode.tui.screens.version_history import HISTORY_TABLE_ID, VersionHistoryScreen
from lode.tui.screens.version_view import VERSION_BODY_ID, VersionViewScreen
from lode.tui.widgets.lode_data_table import LodeDataTable
from lode.versions import save


def test_app_registers_browse_screen(tmp_path: Path) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["browse"] is BrowseScreen


def test_ctrl_b_reaches_the_browse_screen_with_notes_newest_first(
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
            await pilot.press("ctrl+b")
            assert isinstance(app.screen, BrowseScreen)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return [tuple(table.get_row_at(i)) for i in range(table.row_count)]

    rows = asyncio.run(_drive())

    assert len(rows) == 2
    assert str(rows[0][3]) == "second captured note"  # newest-first
    assert str(rows[1][3]) == "first captured note"
    assert str(rows[0][2]) == "v1"


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
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return str(table.get_row_at(0)[0])

    id_cell = asyncio.run(_drive())

    assert id_cell == short_note_id(long_note_id)
    assert id_cell == "01234567"
    assert id_cell != long_note_id


def test_date_column_shows_the_adaptive_form_not_full_iso_8601(
    tmp_path: Path,
) -> None:
    """The Date column (lode-1gr.8) renders the short adaptive form.

    A just-saved note's ``created`` is "now", so the adaptive bucket is
    "today" (just the time) -- the shortest bucket, and the clearest possible
    contrast with the raw ISO-8601 timestamp this replaces.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "a note")
        created = conn.execute(
            "SELECT created FROM notes WHERE note_id = ?", ("note-a",)
        ).fetchone()[0]
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return str(table.get_row_at(0)[1])

    date_cell = asyncio.run(_drive())

    assert date_cell == format_adaptive_date(created)
    assert "T" not in date_cell  # never the raw ISO-8601 stamp


def test_selecting_a_row_opens_the_editor_directly(tmp_path: Path) -> None:
    """Row-select opens the editor directly -- no read-only detour (lode-olmi.2)."""
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
            return app.screen.query_one(f"#{EDIT_BODY_ID}", TextArea).text

    body = asyncio.run(_drive())

    assert body == "the note body to edit"


def test_edit_screen_shows_the_full_note_id(tmp_path: Path) -> None:
    """The editor header shows the FULL id (lode-1gr.2), not the short prefix."""
    long_note_id = "0123456789abcdef-longer-than-eight-chars"
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, long_note_id, "the note body to edit")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            return app.screen.sub_title

    sub_title = asyncio.run(_drive())

    assert sub_title == long_note_id


def test_escape_steps_back_edit_then_list_then_capture(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "a note")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
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
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["still here"]


def test_ctrl_h_from_editor_opens_version_history_newest_first(
    tmp_path: Path,
) -> None:
    """Ctrl+H (not bare ``h`` -- the body TextArea is editable) opens history."""
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
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("ctrl+h")
            await pilot.pause()
            assert isinstance(app.screen, VersionHistoryScreen)
            table = app.screen.query_one(f"#{HISTORY_TABLE_ID}", DataTable)
            return [tuple(table.get_row_at(i)) for i in range(table.row_count)]

    rows = asyncio.run(_drive())

    assert len(rows) == 2
    assert str(rows[0][1]) == "v2"
    assert str(rows[1][1]) == "v1"
    assert str(rows[0][2]) == "update"
    assert str(rows[1][2]) == "create"


def test_version_history_table_renders_brackets_literally_not_as_rich_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version-history table is a ``LodeDataTable`` too (lode-3dz2).

    ``versions.op`` is schema-constrained to ``create``/``update``/``delete``
    (a ``NOT NULL CHECK`` -- see ``lode.retrieval``), so a real save/delete
    can never itself put a bracket through this table -- unlike
    ``ConfigScreen``'s knob table, there is no live-data reproduction here.
    What this proves instead is that the *seam* covers this screen exactly
    like every other: :func:`~lode.notes_read.list_versions` is monkeypatched
    to hand back a row whose ``op`` contains a bracket, standing in for any
    future column here that isn't so constrained, and the render is checked
    through a real ``rich.Console`` -- not ``get_row_at`` alone, which
    returns the stored cell, not the render, and cannot see this bug either
    way (the same distinction every sibling test in this lineage makes).
    """
    import lode.tui.screens.version_history as version_history_module
    from lode.notes_read import VersionRow

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "a note")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    monkeypatch.setattr(
        version_history_module,
        "list_versions",
        lambda db_path, note_id: [
            VersionRow(
                version_id="v1",
                created="2026-07-20T00:00:00.000000Z",
                op="[create]",
                seq=1,
            )
        ],
    )

    async def _drive() -> object:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+h")
            await pilot.pause()
            assert isinstance(app.screen, VersionHistoryScreen)
            table = app.screen.query_one(f"#{HISTORY_TABLE_ID}", DataTable)
            return table.get_row_at(0)[2]

    cell = asyncio.run(_drive())

    buffer = io.StringIO()
    Console(file=buffer, width=40, legacy_windows=False).print(cell)
    assert buffer.getvalue().strip() == "[create]"


def test_version_history_table_scrolls_within_its_own_pane_not_the_whole_screen(
    tmp_path: Path,
) -> None:
    """Locks in lode-efn2: the history table stays bounded above the Footer.

    READ THIS BEFORE TRUSTING THE TEST. This is a property guard, NOT proof
    of a fix -- it passes with lode.tcss's blanket ``DataTable { height:
    1fr; }`` rule removed, verified at six terminal sizes. That is not a
    defect in the test; it is a fact about the screen. lode-efn2 was filed
    believing version-history-table overflowed like #browse-table
    (lode-juz8.2) and #config-knobs (lode-l38d.2) did. It never did:
    VersionHistoryScreen.compose is Header()/DataTable/Footer(), so the
    table is the Screen's SOLE non-docked child, and DataTable's own
    DEFAULT_CSS (``height: auto; max-height: 100%``) already bounds it to
    exactly the area between the docked Header and Footer. The two screens
    that DID overflow each had a second space-consuming sibling, which is
    what ``max-height: 100%`` cannot account for (it resolves against the
    parent's height, not the space left after siblings) -- see the mechanism
    comment in src/lode/tui/lode.tcss and docs/tui.md.

    What this test is still worth: it fails the moment that structural
    accident stops holding -- e.g. if the table is later wrapped in a
    container or gains a non-docked sibling AND the blanket rule is gone
    (verified: sabotaging both together does fail it). It pins the property
    the user asked for regardless of which mechanism currently supplies it.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        parent = save(conn, "note-a", "v1 body").version_id
        for i in range(2, 31):
            parent = save(conn, "note-a", f"v{i} body", parent=parent).version_id
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[object, ...]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+h")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, VersionHistoryScreen)
            table = screen.query_one(f"#{HISTORY_TABLE_ID}", DataTable)
            header = screen.query_one(Header)
            footer = screen.query_one(Footer)
            return (
                screen.size,
                screen.max_scroll_y,
                header.region,
                footer.region,
                table.region,
                table.virtual_size,
            )

    (
        screen_size,
        screen_max_scroll_y,
        header_region,
        footer_region,
        table_region,
        table_virtual_size,
    ) = asyncio.run(_drive())

    # 30 versions is genuinely more content than fits an 80x24 terminal --
    # this test would be vacuous without it.
    assert table_virtual_size.height > table_region.height

    # The screen itself never scrolls...
    assert screen_max_scroll_y == 0
    # ...and Header/Footer -- both docked -- stay on-screen.
    assert header_region.y == 0
    assert footer_region.y + footer_region.height == screen_size.height

    # THE property being pinned: the table's own region ends at or above the
    # Footer's row, so it never extends past the visible window. (Currently
    # supplied by DataTable's own max-height: 100% as much as by the blanket
    # 1fr rule -- see the docstring; this holds either way, by design.)
    assert table_region.y + table_region.height <= footer_region.y


def test_bare_h_from_editor_types_into_the_body_instead(tmp_path: Path) -> None:
    """Bare ``h`` is consumed by the editable body TextArea, not a Screen binding.

    This is exactly why version history is bound to Ctrl+H rather than bare
    ``h`` (lode-olmi.2, amended acceptance criterion) -- guards against a
    regression back to a bare-letter binding, which would silently corrupt
    the note body instead of opening history.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("h")
            await pilot.pause()
            text = app.screen.query_one(f"#{EDIT_BODY_ID}", TextArea).text
            return text, isinstance(app.screen, VersionHistoryScreen)

    text, opened_history = asyncio.run(_drive())

    assert text == "hhello body"
    assert not opened_history


def test_version_history_date_column_shows_the_adaptive_form(
    tmp_path: Path,
) -> None:
    """The version-history Date column (lode-1gr.8) is also the adaptive form."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "only version")
        created = conn.execute(
            "SELECT created FROM versions WHERE note_id = ?", ("note-a",)
        ).fetchone()[0]
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+h")
            await pilot.pause()
            table = app.screen.query_one(f"#{HISTORY_TABLE_ID}", DataTable)
            return str(table.get_row_at(0)[0])

    date_cell = asyncio.run(_drive())

    assert date_cell == format_adaptive_date(created)
    assert "T" not in date_cell


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
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+h")
            await pilot.pause()
            # Cursor starts on the newest row (v2); move down to the prior (v1).
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, VersionViewScreen)
            return app.screen.query_one(f"#{VERSION_BODY_ID}", TextArea).text

    body = asyncio.run(_drive())

    assert body == "v1 body"


def test_escape_steps_back_version_view_then_history_then_editor(
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
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+h")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, VersionViewScreen)
            await pilot.press("escape")
            back_to_history = isinstance(app.screen, VersionHistoryScreen)
            await pilot.press("escape")
            back_to_editor = isinstance(app.screen, EditScreen)
            return back_to_history, back_to_editor

    back_to_history, back_to_editor = asyncio.run(_drive())

    assert back_to_history
    assert back_to_editor


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
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+h")
            await pilot.pause()
            table = app.screen.query_one(f"#{HISTORY_TABLE_ID}", DataTable)
            return table.row_count

    row_count = asyncio.run(_drive())

    assert row_count == 1


def test_version_history_table_sets_empty_message(tmp_path: Path) -> None:
    """VersionHistoryScreen sets ``empty_message`` on mount (lode-ligf).

    A note always has at least one version, so this table is never actually
    reached empty in practice -- the assertion is that the attribute is
    configured at all, matching the other bare-blank tables' adoption.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "only version")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+h")
            await pilot.pause()
            table = app.screen.query_one(f"#{HISTORY_TABLE_ID}", LodeDataTable)
            return table.empty_message

    empty_message = asyncio.run(_drive())

    assert empty_message == "No version history for this note."


def test_long_summary_is_capped_at_one_line_not_wrapped_unbounded(
    tmp_path: Path,
) -> None:
    """A long Summary never grows a row past one line; the table never scrolls sideways.

    Guards the lode-juz8.3 fix: earlier (lode-olmi.3), the Summary column was
    capped to the room left over after Date/Version and rows were fixed at
    two lines, but a busy list was still hard to scan. Every row is now fixed
    at one line and overflow is ellipsized rather than wrapped further.
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
            await pilot.press("ctrl+b")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            return (
                table.rows[row_key].height,
                table.virtual_size.width,
                table.size.width,
                str(table.get_row_at(0)[3]),
            )

    row_height, virtual_width, widget_width, summary_cell = asyncio.run(_drive())

    assert row_height == 1  # capped at one line, not grown to fit the whole summary
    assert virtual_width <= widget_width  # ... so the table needs no h-scroll
    assert summary_cell.count("\n") == 0  # exactly one rendered line
    assert summary_cell != long_summary  # truncated, not kept in full
    assert summary_cell.endswith("\N{HORIZONTAL ELLIPSIS}")  # overflow is ellipsized


def test_short_summary_is_unaffected_by_the_one_line_cap(tmp_path: Path) -> None:
    """A summary that already fits on one line is left alone, no ellipsis added."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    short_summary = "a short summary"
    try:
        save(conn, "note-a", short_summary)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, str]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            return table.rows[row_key].height, str(table.get_row_at(0)[3])

    row_height, summary_cell = asyncio.run(_drive())

    assert row_height == 1  # rows are always the fixed cap height
    assert summary_cell == short_summary  # left untouched, no ellipsis


def test_summary_with_brackets_renders_literally_not_as_rich_markup(
    tmp_path: Path,
) -> None:
    """A note summary containing ``[...]`` renders literally (lode-ix4i).

    Regression test for the same hazard :class:`~lode.tui.screens.tags.TagsScreen`'s
    checkbox cells had before lode-7abi: the Summary column was a bare ``str`` cell,
    which a ``DataTable`` renders through Rich console *markup*, silently eating a
    bracketed substring like ``[draft]``. Prints the actual cell object (now a
    :class:`~rich.text.Text`, never markup-parsed) through a real ``rich.Console`` --
    ``get_row_at`` alone returns the *stored* cell, which can't see this bug either way.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "reviewed [draft] spec")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> object:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return table.get_row_at(0)[3]

    cell = asyncio.run(_drive())

    console = Console(file=io.StringIO(), width=60, legacy_windows=False)
    console.print(cell)
    assert console.file.getvalue().strip() == "reviewed [draft] spec"


# ---------------------------------------------------------------------------
# Expand the highlighted row's summary (lode-juz8.4) -- 'x' toggles between
# the 1-line-capped rendering (lode-juz8.3) and the full, untruncated
# summary, highlighted row only.
# ---------------------------------------------------------------------------


def test_x_expands_the_highlighted_row_to_its_full_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    long_summary = ("wrap me " * 40).strip()  # ~320 chars: wraps to several lines
    try:
        save(conn, "note-a", long_summary)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, str]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            # Collapsed first: the row is ellipsized at one line, same as the
            # lode-juz8.3 cap test above.
            assert table.rows[row_key].height == 1
            await pilot.press("x")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            return table.rows[row_key].height, str(table.get_row_at(0)[3])

    row_height, summary_cell = asyncio.run(_drive())

    assert row_height > 1  # expanded past the one-line cap
    assert "\N{HORIZONTAL ELLIPSIS}" not in summary_cell  # nothing truncated
    assert summary_cell.replace("\n", " ") == long_summary  # full text, rewrapped


def test_x_twice_collapses_back_to_the_one_line_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    long_summary = ("wrap me " * 40).strip()
    try:
        save(conn, "note-a", long_summary)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, str]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("x")  # toggle back off
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            return table.rows[row_key].height, str(table.get_row_at(0)[3])

    row_height, summary_cell = asyncio.run(_drive())

    assert row_height == 1
    assert summary_cell.endswith("\N{HORIZONTAL ELLIPSIS}")


def test_x_only_affects_the_highlighted_row(tmp_path: Path) -> None:
    """Expanding one row leaves every other row's 1-line cap untouched."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    long_summary_a = ("wrap me " * 40).strip()
    long_summary_b = ("also wrap me " * 40).strip()
    try:
        save(conn, "note-a", long_summary_a)
        save(conn, "note-b", long_summary_b)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, int, str, str]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            # Newest-first: note-b (row 0) is highlighted by default.
            await pilot.press("x")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_keys = list(table.rows)
            return (
                table.rows[row_keys[0]].height,
                table.rows[row_keys[1]].height,
                str(table.get_row_at(0)[3]),
                str(table.get_row_at(1)[3]),
            )

    expanded_height, other_height, expanded_cell, other_cell = asyncio.run(_drive())

    assert expanded_height > 1
    assert other_height == 1
    assert "\N{HORIZONTAL ELLIPSIS}" not in expanded_cell
    assert other_cell.endswith("\N{HORIZONTAL ELLIPSIS}")


def test_expansion_collapses_after_returning_from_the_editor(tmp_path: Path) -> None:
    """Popping back from EditScreen re-fires on_screen_resume, which resets
    the expansion -- confirmed acceptable over preserving it (lode-juz8.4)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    long_summary = ("wrap me " * 40).strip()
    try:
        save(conn, "note-a", long_summary)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, int]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            expanded_height = table.rows[row_key].height
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("escape")  # unchanged buffer -- pops immediately
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            return expanded_height, table.rows[row_key].height

    expanded_height, height_after_return = asyncio.run(_drive())

    assert expanded_height > 1
    assert height_after_return == 1


def test_expansion_collapses_on_resize(tmp_path: Path) -> None:
    """A terminal resize re-fires on_resize, which also resets the
    expansion (lode-juz8.4) -- the same "does not survive this reload"
    contract as returning from the editor."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    long_summary = ("wrap me " * 40).strip()
    try:
        save(conn, "note-a", long_summary)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, int]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            expanded_height = table.rows[row_key].height
            await pilot.resize_terminal(100, 30)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            row_key = next(iter(table.rows))
            return expanded_height, table.rows[row_key].height

    expanded_height, height_after_resize = asyncio.run(_drive())

    assert expanded_height > 1
    assert height_after_resize == 1


def test_x_on_empty_table_is_a_no_op(tmp_path: Path) -> None:
    """Guards ``action_toggle_summary``'s ``row_count == 0`` check, the same
    pattern every other row action on this screen already follows."""
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> int:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("x")  # must not raise
            return app.screen.query_one(f"#{TABLE_ID}", DataTable).row_count

    row_count = asyncio.run(_drive())

    assert row_count == 0


# ---------------------------------------------------------------------------
# Enrichment inspector modal (lode-ay5.2) -- 'i' on a highlighted row opens a
# modal rendering lode.enrichment_view.enrichment_view (lode-ay5.1) verbatim.
# Seeding jobs/annotations/edges mirrors tests/test_enrichment_view.py's own
# hand-inserted convention -- save() alone (no Repository) enqueues no job and
# writes no AI output, so the enrichment_state predicate reads these directly.
# ---------------------------------------------------------------------------


def _insert_enrich_job(
    conn: sqlite3.Connection, *, target_version: str, status: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
            "VALUES ('enrich', ?, ?, ?)",
            (target_version, status, now_iso()),
        )


def _insert_annotation(
    conn: sqlite3.Connection,
    *,
    target: str,
    source_version: str,
    kind: str,
    payload_value: str,
    source: str = "ai",
    status: str = "fresh",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (target, source_version, kind, json.dumps(payload_value), source, status),
        )


def _insert_edge(
    conn: sqlite3.Connection,
    *,
    from_id: str,
    to_id: str,
    source_version: str,
    reason: str = "mentions related material",
    confidence: float = 0.75,
    source: str = "ai",
    status: str = "fresh",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO edges "
            "(from_id, to_id, source, reason, confidence, source_version, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (from_id, to_id, source, reason, confidence, source_version, status),
        )


def test_i_on_highlighted_row_opens_the_inspector_with_full_enrichment(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "a note about jwt auth").version_id
        _insert_annotation(
            conn,
            target="note-a",
            source_version=head,
            kind="summary",
            payload_value="a note about jwt auth",
        )
        _insert_annotation(
            conn, target="note-a", source_version=head, kind="tag", payload_value="auth"
        )
        _insert_annotation(
            conn,
            target="note-a",
            source_version=head,
            kind="entity",
            payload_value="JWT",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="note-b",
            source_version=head,
            reason="mentions jwt auth",
            confidence=0.82,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> dict[str, str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            assert isinstance(app.screen, EnrichmentModalScreen)
            screen = app.screen
            return {
                "state": str(
                    screen.query_one(f"#{INSPECTOR_STATE_ID}", Static).content
                ),
                "summary": str(
                    screen.query_one(f"#{INSPECTOR_SUMMARY_ID}", Static).content
                ),
                "tags": str(screen.query_one(f"#{INSPECTOR_TAGS_ID}", Static).content),
                "entities": str(
                    screen.query_one(f"#{INSPECTOR_ENTITIES_ID}", Static).content
                ),
                "edges": str(
                    screen.query_one(f"#{INSPECTOR_EDGES_ID}", Static).content
                ),
                "embed": str(
                    screen.query_one(f"#{INSPECTOR_EMBED_ID}", Static).content
                ),
            }

    fields = asyncio.run(_drive())

    assert fields["state"] == "Enrichment: ready"
    assert fields["summary"] == "Summary: a note about jwt auth"
    assert fields["tags"] == "Tags: auth"
    assert fields["entities"] == "Entities: JWT"
    assert (
        fields["edges"]
        == f"Edges:\n-> {short_note_id('note-b')} (mentions jwt auth, 0.82)"
    )
    assert fields["embed"] == "Embedded: no (0 passages)"


def test_ctrl_g_from_editor_opens_the_inspector_with_full_enrichment(
    tmp_path: Path,
) -> None:
    """Ctrl+G (not bare ``i`` -- the body TextArea is editable) opens the same
    enrichment inspector modal Browse's ``i`` binding does (lode-g5es)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "a note about jwt auth").version_id
        _insert_annotation(
            conn,
            target="note-a",
            source_version=head,
            kind="summary",
            payload_value="a note about jwt auth",
        )
        _insert_annotation(
            conn, target="note-a", source_version=head, kind="tag", payload_value="auth"
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> dict[str, str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("ctrl+g")
            await pilot.pause()
            assert isinstance(app.screen, EnrichmentModalScreen)
            screen = app.screen
            return {
                "state": str(
                    screen.query_one(f"#{INSPECTOR_STATE_ID}", Static).content
                ),
                "summary": str(
                    screen.query_one(f"#{INSPECTOR_SUMMARY_ID}", Static).content
                ),
                "tags": str(screen.query_one(f"#{INSPECTOR_TAGS_ID}", Static).content),
            }

    fields = asyncio.run(_drive())

    assert fields["state"] == "Enrichment: ready"
    assert fields["summary"] == "Summary: a note about jwt auth"
    assert fields["tags"] == "Tags: auth"


def test_bare_i_from_editor_types_into_the_body_instead(tmp_path: Path) -> None:
    """Bare ``i`` is consumed by the editable body TextArea, not a Screen binding.

    Guards against a regression back to a bare-letter binding for the
    inspector (lode-g5es) -- exactly the trap ``Ctrl+H``'s own guard test
    (``test_bare_h_from_editor_types_into_the_body_instead``, above) covers
    for version history.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "insightful body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("i")
            await pilot.pause()
            text = app.screen.query_one(f"#{EDIT_BODY_ID}", TextArea).text
            return text, isinstance(app.screen, EnrichmentModalScreen)

    text, opened_inspector = asyncio.run(_drive())

    assert text == "iinsightful body"
    assert not opened_inspector


def test_inspector_modal_field_coverage_matches_the_view_model(tmp_path: Path) -> None:
    """TUI-side parity guard (lode-ay5.4), mirroring the CLI one (lode-ay5.3,

    ``tests/test_cli.py::test_show_field_coverage_matches_the_view_model``).
    That guard only enumerated ``EnrichmentView``'s fields against ``lode
    show``'s stdout -- nothing enumerated them against the inspector modal, so
    a field added to the view-model and surfaced only on the CLI side (or vice
    versa) would pass every existing suite while the two surfaces silently
    diverged (the exact drift the epic's debate flagged, note 3). This
    enumerates the same field set and asserts each is surfaced by the modal,
    with the two fields the modal deliberately does not give their own line
    called out by name below -- not silently dropped.
    """
    import dataclasses

    from lode.enrichment_view import EnrichmentView

    field_names = {f.name for f in dataclasses.fields(EnrichmentView)}
    assert field_names == {
        "note_id",
        "enrichment_state",
        "summary",
        "tags",
        "entities",
        "edges",
        "embedded",
        "passage_count",
    }

    # Fields the modal does NOT give their own widget/line -- an explicit,
    # commented exemption per lode-ay5.4's acceptance criteria, not a silent
    # omission:
    #  - note_id: keyed to the Browse row the modal was opened on (``i`` on
    #    a highlighted row) -- that row's Id column already shows it, so the
    #    modal itself never re-prints it.
    #  - embedded / passage_count: collapsed together into the ONE
    #    "Embedded: yes/no (N passages)" line (INSPECTOR_EMBED_ID) rather than
    #    each getting its own line -- both are checked below via that single
    #    field.
    exempt = {"note_id", "embedded", "passage_count"}
    assert field_names - exempt == {
        "enrichment_state",
        "summary",
        "tags",
        "entities",
        "edges",
    }

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-parity-tui", "parity body").version_id
        _insert_annotation(
            conn,
            target="note-parity-tui",
            source_version=head,
            kind="summary",
            payload_value="a summary",
        )
        _insert_annotation(
            conn,
            target="note-parity-tui",
            source_version=head,
            kind="tag",
            payload_value="a-tag",
        )
        _insert_annotation(
            conn,
            target="note-parity-tui",
            source_version=head,
            kind="entity",
            payload_value="an-entity",
        )
        _insert_edge(
            conn,
            from_id="note-parity-tui",
            to_id="concept-parity-tui",
            source_version=head,
            reason="because",
            confidence=0.5,
        )
        conn.execute(
            "INSERT INTO passages (passage_id, target_version, ord, text) "
            "VALUES ('p-parity-tui', ?, 0, 'parity body')",
            (head,),
        )
        conn.commit()
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> dict[str, str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            screen = app.screen
            return {
                "enrichment_state": str(
                    screen.query_one(f"#{INSPECTOR_STATE_ID}", Static).content
                ),
                "summary": str(
                    screen.query_one(f"#{INSPECTOR_SUMMARY_ID}", Static).content
                ),
                "tags": str(screen.query_one(f"#{INSPECTOR_TAGS_ID}", Static).content),
                "entities": str(
                    screen.query_one(f"#{INSPECTOR_ENTITIES_ID}", Static).content
                ),
                "edges": str(
                    screen.query_one(f"#{INSPECTOR_EDGES_ID}", Static).content
                ),
                # embedded + passage_count together, the one combined line:
                "embed": str(
                    screen.query_one(f"#{INSPECTOR_EMBED_ID}", Static).content
                ),
            }

    fields = asyncio.run(_drive())

    assert fields["enrichment_state"] == "Enrichment: ready"
    assert fields["summary"] == "Summary: a summary"
    assert fields["tags"] == "Tags: a-tag"
    assert fields["entities"] == "Entities: an-entity"
    assert (
        fields["edges"]
        == f"Edges:\n-> {short_note_id('concept-parity-tui')} (because, 0.50)"
    )
    assert fields["embed"] == "Embedded: yes (1 passages)"


def test_inspector_modal_shows_pending_for_an_unenriched_note(
    tmp_path: Path,
) -> None:
    """A freshly captured note with a live enrich job and no AI output reads pending."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "freshly captured, not yet enriched").version_id
        _insert_enrich_job(conn, target_version=head, status="pending")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, str, str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            screen = app.screen
            return (
                str(screen.query_one(f"#{INSPECTOR_STATE_ID}", Static).content),
                str(screen.query_one(f"#{INSPECTOR_SUMMARY_ID}", Static).content),
                str(screen.query_one(f"#{INSPECTOR_EDGES_ID}", Static).content),
            )

    state, summary, edges = asyncio.run(_drive())

    assert state == "Enrichment: pending"
    assert summary == "Summary: (none)"
    assert edges == "Edges:\n(none)"


def test_inspector_modal_shows_failed_for_a_dead_lettered_job(
    tmp_path: Path,
) -> None:
    """A dead enrich job with zero AI output reads failed, not enriched-empty."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "enrichment kept dying").version_id
        _insert_enrich_job(conn, target_version=head, status="dead")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            return str(app.screen.query_one(f"#{INSPECTOR_STATE_ID}", Static).content)

    state = asyncio.run(_drive())

    assert state == "Enrichment: failed"


def test_escape_dismisses_the_inspector_modal_back_to_browse(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "a note")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            assert isinstance(app.screen, EnrichmentModalScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return isinstance(app.screen, BrowseScreen)

    back_to_list = asyncio.run(_drive())

    assert back_to_list


def test_stale_tag_is_styled_dim_not_printed_as_a_cli_style_suffix(
    tmp_path: Path,
) -> None:
    """Guards lode-0qc: the modal styles ``stale`` as a Rich span, never a baked
    ``" [stale]"`` marker -- that's ``lode show``'s rendering choice, not this
    screen's (docs/storage.md's "Enrichment view-model" section).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "a note").version_id
        _insert_annotation(
            conn,
            target="note-a",
            source_version=head,
            kind="tag",
            payload_value="stale-tag",
            status="stale",
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> Text:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            content = app.screen.query_one(f"#{INSPECTOR_TAGS_ID}", Static).content
            assert isinstance(content, Text)
            return content

    tags_text = asyncio.run(_drive())

    assert str(tags_text) == "Tags: stale-tag"
    assert "[stale]" not in str(tags_text)
    assert any(span.style == "dim" for span in tags_text.spans)


def test_i_on_an_empty_browse_list_is_a_no_op_not_a_crash(tmp_path: Path) -> None:
    """No highlighted row (empty list) -> 'i' opens nothing and does not raise.

    ``action_inspect_selected`` guards ``row_count == 0`` before touching
    ``coordinate_to_cell_key``; without that guard ``i`` on an empty Browse
    list would raise. Mirrors the same empty-list contract ``e`` (edit) holds.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert app.screen.query_one(f"#{TABLE_ID}", DataTable).row_count == 0
            await pilot.press("i")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    stayed_on_browse = asyncio.run(_drive())

    assert stayed_on_browse


def test_no_notes_at_all_shows_a_distinct_empty_message(tmp_path: Path) -> None:
    """Fresh install / everything tombstoned: distinct copy from a
    quick-search-matched-nothing empty result (lode-ligf)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, str | None]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", LodeDataTable)
            return table.row_count, table.empty_message

    row_count, empty_message = asyncio.run(_drive())

    assert row_count == 0
    assert empty_message == "No notes yet."


def test_quick_search_matching_nothing_shows_a_distinct_empty_message(
    tmp_path: Path,
) -> None:
    """A quick search that narrows to zero BM25 matches gets its own
    explanation, distinct from the no-notes-at-all case (lode-ligf)."""
    db_path = tmp_path / "lode.db"
    _seed_four_notes_indexed(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, str | None]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("s")
            await _press_and_settle(pilot, *"zzzznomatch")
            table = app.screen.query_one(f"#{TABLE_ID}", LodeDataTable)
            return table.row_count, table.empty_message

    row_count, empty_message = asyncio.run(_drive())

    assert row_count == 0
    assert empty_message == "No notes match 'zzzznomatch'."


# ---------------------------------------------------------------------------
# External-snapshot introspection (lode-8d2) -- an edge that draws down a web
# link gains a second, indented line in the Edges block showing that
# external's ExternalView (source_type/snapshot id/fetched_at/state),
# through the same lode.enrichment_view.enrichment_view seam.
# ---------------------------------------------------------------------------


def _insert_external(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    source_type: str = "web",
    snapshot_id: str,
    status: str = "ok",
    no_egress: bool = False,
    fetched_at: str = "2026-07-08T00:00:00.000000Z",
    body: str = "body",
    raw_payload: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, no_egress) "
            "VALUES (?, ?, ?)",
            (external_id, source_type, int(no_egress)),
        )
        conn.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, external_id, body, raw_payload, status, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snapshot_id, external_id, body, raw_payload, status, fetched_at),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
        )


def test_inspector_shows_external_snapshot_for_a_drawn_down_edge(
    tmp_path: Path,
) -> None:
    """An edge to a real external gains a snapshot line -- source_type, short
    snapshot id, fetched_at, and its 'un-refreshed' state (the default,
    still shown explicitly).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "see https://example.com/article").version_id
        _insert_external(
            conn,
            external_id="https://example.com/article",
            snapshot_id="snap-tui-1",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://example.com/article",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> Text:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            content = app.screen.query_one(f"#{INSPECTOR_EDGES_ID}", Static).content
            assert isinstance(content, Text)
            return content

    edges_text = asyncio.run(_drive())
    rendered = str(edges_text)

    assert "-> https://example.com/article (pasted URL, 1.00)" in rendered
    assert "web" in rendered
    assert short_version_id("snap-tui-1") in rendered
    assert "as of 2026-07-08T00:00:00.000000Z" in rendered
    assert "[un-refreshed]" in rendered


def test_inspector_distinguishes_stale_and_withheld_external_states(
    tmp_path: Path,
) -> None:
    """A tombstoned external reads [stale]; a no_egress one reads [withheld] --
    the three ExternalView states are distinguishable in one render.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(
            conn,
            "note-a",
            "see https://dead.example.com/ and https://sensitive.example.com/",
        ).version_id
        _insert_external(
            conn,
            external_id="https://dead.example.com/",
            snapshot_id="snap-tui-dead",
            status="tombstone",
        )
        _insert_external(
            conn,
            external_id="https://sensitive.example.com/",
            snapshot_id="snap-tui-sensitive",
            no_egress=True,
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://dead.example.com/",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://sensitive.example.com/",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> Text:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            content = app.screen.query_one(f"#{INSPECTOR_EDGES_ID}", Static).content
            assert isinstance(content, Text)
            return content

    edges_text = asyncio.run(_drive())
    rendered = str(edges_text)

    assert "[stale]" in rendered
    assert "[withheld]" in rendered
    assert "[un-refreshed]" not in rendered


def test_inspector_edge_without_a_matching_external_row_has_no_snapshot_line(
    tmp_path: Path,
) -> None:
    """A plain inferred edge (to_id not a real external) prints only its own
    line -- no extra snapshot sub-line, no crash."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "a note").version_id
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="note-b",
            source_version=head,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> Text:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("i")
            await pilot.pause()
            content = app.screen.query_one(f"#{INSPECTOR_EDGES_ID}", Static).content
            assert isinstance(content, Text)
            return content

    edges_text = asyncio.run(_drive())
    rendered = str(edges_text)

    assert (
        rendered
        == f"Edges:\n-> {short_note_id('note-b')} (mentions related material, 0.75)"
    )
    assert "snapshot" not in rendered


# ---------------------------------------------------------------------------
# Delete from browse (lode-d32.1) -- 'd' on a highlighted row pops a Yes/No
# confirm; confirming appends an op='delete' tombstone (routed through
# Repository so the cache leg is evicted too) and the note vanishes from the
# reloaded live table; declining leaves it untouched; an empty table is a
# no-op; a CAS reject notifies and reloads rather than crashing.
# ---------------------------------------------------------------------------


def test_d_on_a_highlighted_row_pops_the_delete_confirm(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "a note to maybe delete")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteConfirmScreen)
            return str(
                app.screen.query_one(f"#{DELETE_CONFIRM_MESSAGE_ID}", Static).content
            )

    message = asyncio.run(_drive())

    assert "elete" in message
    assert "Y" in message
    assert "N" in message


def test_confirming_delete_appends_a_tombstone_and_the_note_vanishes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "kept-note", "stays")
        save(conn, "gone-note", "goes")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            # Newest-first: "gone-note" (saved last) is the top row, and the
            # cursor starts there by default -- no cursor manipulation needed.
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["stays"]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT op FROM versions WHERE note_id = ? ORDER BY rowid",
            ("gone-note",),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("create",), ("delete",)]


def test_declining_delete_leaves_the_note_untouched(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "keep me")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteConfirmScreen)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[3]) for i in range(table.row_count)]

    summaries = asyncio.run(_drive())

    assert summaries == ["keep me"]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT op FROM versions WHERE note_id = ?", ("note-a",)
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("create",)]


def test_escape_on_the_delete_confirm_also_declines(tmp_path: Path) -> None:
    """Escape is a bound alias for "no" on this dialog, same as the decline key."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "keep me too")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteConfirmScreen)
            await pilot.press("escape")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    back_to_list = asyncio.run(_drive())

    assert back_to_list
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT op FROM versions WHERE note_id = ?", ("note-a",)
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("create",)]


def test_d_on_an_empty_browse_list_is_a_no_op_not_a_crash(tmp_path: Path) -> None:
    """No highlighted row (empty list) -> 'd' opens no confirm and does not raise.

    Mirrors the same empty-list guard ``i`` (inspect) and ``e`` (edit) hold.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert app.screen.query_one(f"#{TABLE_ID}", DataTable).row_count == 0
            await pilot.press("d")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    stayed_on_browse = asyncio.run(_drive())

    assert stayed_on_browse


def test_delete_confirmed_after_the_note_already_vanished_resyncs_not_crashes(
    tmp_path: Path,
) -> None:
    """The note is gone by the time the confirm is answered -- not a crash.

    ``BrowseScreen`` re-resolves the head right before deleting; if the note
    is already gone by then (``load_head`` returns ``None`` -- e.g. deleted
    from another session while this confirm dialog sat open), it just
    resyncs the table instead of attempting a doomed delete.
    """
    from lode.versions import delete as versions_delete

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "about to vanish").version_id
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, list[str]]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteConfirmScreen)
            # Someone else deletes the note out from under the still-open
            # confirm dialog.
            conn = init_db(db_path)
            try:
                versions_delete(conn, "note-a", parent=head)
            finally:
                conn.close()
            await pilot.press("y")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            summaries = [str(table.get_row_at(i)[3]) for i in range(table.row_count)]
            return isinstance(app.screen, BrowseScreen), summaries

    stayed_on_browse, summaries = asyncio.run(_drive())

    assert stayed_on_browse
    assert summaries == []


def test_delete_head_conflict_notifies_and_reloads_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine CAS reject inside ``delete_note`` (:class:`HeadConflictError`)
    is handled gracefully -- notify and reload, never an unhandled crash.

    Forcing this precisely requires the live head to move in the narrow gap
    between ``BrowseScreen`` re-resolving it (:func:`~lode.tui.services.edit.load_head`)
    and calling :func:`~lode.tui.services.edit.delete_note` -- not reachable from a
    single-threaded pilot flow without patching that gap directly, so
    ``load_head`` is monkeypatched to hand back a stale parent while the real
    head has already moved on.
    """
    import lode.tui.screens.browse as browse_module

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        stale_head = save(conn, "note-a", "original body").version_id
        # The real head moves on -- an edit lands, unbeknownst to the stale
        # parent load_head is about to (be made to) hand back.
        save(conn, "note-a", "moved on without you", parent=stale_head)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    monkeypatch.setattr(
        browse_module,
        "load_head",
        lambda db_path, note_id: (stale_head, "original body"),
    )

    async def _drive() -> tuple[bool, list[str]]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            summaries = [str(table.get_row_at(i)[3]) for i in range(table.row_count)]
            return isinstance(app.screen, BrowseScreen), summaries

    stayed_on_browse, summaries = asyncio.run(_drive())

    assert stayed_on_browse
    # No crash, and the reload reflects the note's real (undeleted) state.
    assert summaries == ["moved on without you"]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT body, op FROM versions WHERE note_id = ? ORDER BY rowid",
            ("note-a",),
        ).fetchall()
    finally:
        conn.close()
    # No tombstone was written -- the rejected delete left the chain untouched.
    assert rows == [("original body", "create"), ("moved on without you", "update")]


# ---------------------------------------------------------------------------
# Progressive incremental search (lode-olmi.4) -- '/' opens a hidden one-line
# Input at the bottom of the screen; each keystroke re-scans the table from
# the cursor's current row for the next Summary containing the query
# (case-insensitive substring), wrapping if needed. '?' does the same upward.
# Escape/Enter both close the box, keeping wherever the search landed.
#
# Four notes are saved in this order: alpha, beta, gamma, delta -- newest
# first, the table therefore reads (top to bottom) delta(0), gamma(1),
# beta(2), alpha(3).
# ---------------------------------------------------------------------------


def _seed_four_notes(db_path: Path) -> None:
    conn = init_db(db_path)
    try:
        save(conn, "note-alpha", "alpha widget")
        save(conn, "note-beta", "beta widget")
        save(conn, "note-gamma", "gamma widget")
        save(conn, "note-delta", "delta report")
    finally:
        conn.close()


def _seed_four_notes_indexed(db_path: Path) -> None:
    """Same four notes as :func:`_seed_four_notes`, but also FTS5-indexed
    (lode-35nu.6's quick search needs ``passages_fts`` populated -- plain
    :func:`~lode.versions.save` alone, unlike ``Repository``, never drives
    :class:`~lode.lexical.LexicalCacheBackend`)."""
    conn = init_db(db_path)
    try:
        backend = LexicalCacheBackend(conn)
        for note_id, body in (
            ("note-alpha", "alpha widget"),
            ("note-beta", "beta widget"),
            ("note-gamma", "gamma widget"),
            ("note-delta", "delta report"),
        ):
            result = save(conn, note_id, body)
            backend.index(note_id, result.version_id, body)
    finally:
        conn.close()


# _press_and_settle moved to tests/conftest.py (lode-lcju) -- see docs/tui.md's
# "Settling TUI tests under load" section for the ruling + mechanism, and
# tests/conftest.py's own docstring for the helper itself. In brief: this
# file's search is a STATEFUL cascade (``BrowseScreen._seek_match`` reads
# ``table.cursor_row`` as its scan start), so a key dispatched before the
# previous one's cascade lands corrupts the next scan -- pressing one key per
# call, each with its own real drain, fixes it. NARROW BY DESIGN: a plain
# multi-key ``pilot.press("down", "down", "down")`` elsewhere in this file is
# fine and deliberately left alone -- cursor moves are order-preserving with
# no read-back dependency between keys.


def test_slash_opens_a_hidden_search_box_and_focuses_it(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_four_notes(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool, bool]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            search_input = app.screen.query_one(f"#{SEARCH_INPUT_ID}", Input)
            closed_before = not search_input.display
            await pilot.press("slash")
            await pilot.pause()
            open_after = search_input.display
            focused_after = app.focused is search_input
            return closed_before, open_after, focused_after

    closed_before, open_after, focused_after = asyncio.run(_drive())

    assert closed_before
    assert open_after
    assert focused_after


def test_incremental_search_jumps_to_the_first_matching_summary(
    tmp_path: Path,
) -> None:
    """'/' then typing scans from the top for the first matching row."""
    db_path = tmp_path / "lode.db"
    _seed_four_notes(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> int:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            assert table.cursor_row == 0  # starts on delta
            await pilot.press("slash")
            await _press_and_settle(pilot, *"beta")
            return table.cursor_row

    cursor_row = asyncio.run(_drive())

    assert cursor_row == 2  # beta widget -- gamma (row 1) doesn't match "beta"


def test_incremental_search_restarts_from_the_top_every_keystroke(
    tmp_path: Path,
) -> None:
    """'/' always scans from row 0, even when the cursor already sits on a
    matching row -- search direction is retired, "continue from the cursor"
    no longer exists (lode-2bt3.1)."""
    db_path = tmp_path / "lode.db"
    _seed_four_notes(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> int:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("down", "down")
            assert table.cursor_row == 2  # beta widget -- itself a match for "widget"
            await pilot.press("slash")
            await _press_and_settle(pilot, *"widget")
            return table.cursor_row

    cursor_row = asyncio.run(_drive())

    assert (
        cursor_row == 1
    )  # gamma widget -- the topmost match, not the cursor's own row


def test_incremental_search_matches_case_insensitively(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_four_notes(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> int:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("slash")
            await _press_and_settle(pilot, *"GAMMA")
            return table.cursor_row

    cursor_row = asyncio.run(_drive())

    assert cursor_row == 1  # gamma widget, matched despite the differing case


def test_empty_query_is_a_no_op(tmp_path: Path) -> None:
    """Backspacing the query back to empty leaves the cursor wherever it last
    landed, rather than searching for (and "matching") the empty string."""
    db_path = tmp_path / "lode.db"
    _seed_four_notes(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> int:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("slash")
            await _press_and_settle(pilot, *"beta")
            assert table.cursor_row == 2
            await _press_and_settle(
                pilot, "backspace", "backspace", "backspace", "backspace"
            )
            return table.cursor_row

    cursor_row = asyncio.run(_drive())

    assert cursor_row == 2  # unchanged -- the empty query moved nothing


def test_escape_closes_the_search_box_and_keeps_the_current_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    _seed_four_notes(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool, int]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("slash")
            await _press_and_settle(pilot, *"beta")
            await pilot.press("escape")
            await pilot.pause()
            search_input = app.screen.query_one(f"#{SEARCH_INPUT_ID}", Input)
            still_browsing = isinstance(app.screen, BrowseScreen)
            return still_browsing, search_input.display, table.cursor_row

    still_browsing, box_visible, cursor_row = asyncio.run(_drive())

    assert still_browsing  # Escape closed the box, not the whole screen
    assert not box_visible
    assert cursor_row == 2  # selection kept where the search left it


def test_enter_confirms_and_closes_the_search_box(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_four_notes(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, int]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("slash")
            await _press_and_settle(pilot, *"gamma")
            await pilot.press("enter")
            await pilot.pause()
            search_input = app.screen.query_one(f"#{SEARCH_INPUT_ID}", Input)
            return search_input.display, table.cursor_row

    box_visible, cursor_row = asyncio.run(_drive())

    assert not box_visible
    assert cursor_row == 1  # gamma widget, kept after Enter confirms


# --- 's': BM25 quick search narrows the list (lode-35nu.6) --------------------


def test_s_opens_a_hidden_quick_search_box_and_focuses_it(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_four_notes_indexed(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool, bool]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            quick_input = app.screen.query_one(f"#{QUICK_SEARCH_INPUT_ID}", Input)
            closed_before = not quick_input.display
            await pilot.press("s")
            await pilot.pause()
            open_after = quick_input.display
            focused_after = app.focused is quick_input
            return closed_before, open_after, focused_after

    closed_before, open_after, focused_after = asyncio.run(_drive())

    assert closed_before
    assert open_after
    assert focused_after


def test_typing_in_quick_search_narrows_the_table_to_bm25_matches(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    _seed_four_notes_indexed(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("s")
            await _press_and_settle(pilot, *"report")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return [str(table.get_row_at(i)[0]) for i in range(table.row_count)]

    id_cells = asyncio.run(_drive())

    assert id_cells == [short_note_id("note-delta")]  # only "delta report" matches


def test_clearing_the_quick_search_box_restores_the_full_list(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    _seed_four_notes_indexed(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, int]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("s")
            await _press_and_settle(pilot, *"report")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            narrowed_count = table.row_count
            quick_input = app.screen.query_one(f"#{QUICK_SEARCH_INPUT_ID}", Input)
            quick_input.value = ""
            await pilot.pause()
            return narrowed_count, table.row_count

    narrowed_count, restored_count = asyncio.run(_drive())

    assert narrowed_count == 1
    assert restored_count == 4


def test_escape_closes_the_quick_search_box_but_keeps_the_narrowed_list(
    tmp_path: Path,
) -> None:
    """Mirrors '/'s own contract (lode-olmi.4): closing the box is not a
    revert-on-cancel -- only clearing the text (not closing the box) restores
    the full list, per :func:`test_clearing_the_quick_search_box_restores_the_full_list`."""
    db_path = tmp_path / "lode.db"
    _seed_four_notes_indexed(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool, int]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("s")
            await _press_and_settle(pilot, *"report")
            await pilot.press("escape")
            await pilot.pause()
            quick_input = app.screen.query_one(f"#{QUICK_SEARCH_INPUT_ID}", Input)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            still_browsing = isinstance(app.screen, BrowseScreen)
            return still_browsing, quick_input.display, table.row_count

    still_browsing, box_visible, row_count = asyncio.run(_drive())

    assert still_browsing  # Escape closed the box, not the whole screen
    assert not box_visible
    assert row_count == 1  # the narrowed result is kept


def test_opening_one_search_box_closes_the_other(tmp_path: Path) -> None:
    """At most one of the two boxes is ever open (lode-35nu.6, review).

    '/' leaves its box open when focus moves off it, so '/' then Tab then 's'
    used to display BOTH boxes at once -- and then Escape's branch order, not
    anything the user did, decided which one it closed. Verified in both
    directions.
    """
    db_path = tmp_path / "lode.db"
    _seed_four_notes_indexed(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool, bool, bool]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            scan = app.screen.query_one(f"#{SEARCH_INPUT_ID}", Input)
            quick = app.screen.query_one(f"#{QUICK_SEARCH_INPUT_ID}", Input)
            # '/' -> Tab (focus back to the table) -> 's'
            await pilot.press("slash")
            await pilot.press("tab")
            await pilot.press("s")
            await pilot.pause()
            scan_after_s, quick_after_s = scan.display, quick.display
            # ...and the mirror: 's' is open now, Tab off it, then '/'
            await pilot.press("tab")
            await pilot.press("slash")
            await pilot.pause()
            return scan_after_s, quick_after_s, scan.display, quick.display

    scan_after_s, quick_after_s, scan_after_slash, quick_after_slash = asyncio.run(
        _drive()
    )

    assert not scan_after_s and quick_after_s  # 's' closed the '/' box
    assert scan_after_slash and not quick_after_slash  # '/' closed the 's' box


def test_quick_search_never_touches_the_scan_search_box(tmp_path: Path) -> None:
    """The two boxes are independent -- opening one leaves the other closed."""
    db_path = tmp_path / "lode.db"
    _seed_four_notes_indexed(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("s")
            await pilot.pause()
            search_input = app.screen.query_one(f"#{SEARCH_INPUT_ID}", Input)
            return search_input.display

    scan_box_visible = asyncio.run(_drive())

    assert not scan_box_visible


def test_search_box_stays_on_screen_when_the_notes_list_overflows_the_viewport(
    tmp_path: Path,
) -> None:
    """lode-juz8.2: a notes list taller than the terminal used to push the
    (non-docked) search Input below the visible area once opened -- the
    DataTable had no height constraint, so it auto-sized past the viewport
    and Screen's default overflow-y: auto scrolled the whole screen to
    accommodate it, taking the Input (and only the Input -- Header/Footer
    are always docked) out of view with it.

    Regression guard, not a behavior test: drives Pilot at a small, FIXED
    screen size with a list deliberately taller than that, opens the search
    box, and asserts the Input's laid-out screen region is fully within the
    viewport bounds -- not merely that search still works, which would pass
    even with the box off-screen.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        for i in range(40):  # far more rows than a 10-row-tall screen can show
            save(conn, f"note-{i:02d}", f"note number {i}")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    screen_size = (80, 10)

    async def _drive() -> tuple[int, int, int, int]:
        async with app.run_test(size=screen_size) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            search_input = app.screen.query_one(f"#{SEARCH_INPUT_ID}", Input)
            region = search_input.region
            return region.y, region.bottom, region.right, region.x

    top, bottom, right, left = asyncio.run(_drive())

    width, height = screen_size
    assert 0 <= top < height  # the box's top edge is on-screen
    assert bottom <= height  # ... and its bottom edge doesn't run off it either
    assert 0 <= left < right <= width  # sanity: a real, non-empty region


# ---------------------------------------------------------------------------
# Cursor preservation across a reload (lode-olmi.1) -- _reload_rows'
# clear(columns=True) used to discard the DataTable's cursor, so leaving a
# highlighted row to view/edit a note and Escaping back always snapped the
# cursor to the top. The fix captures the highlighted note_id before the
# rebuild and restores the cursor to that same row key afterward, falling
# back to the top only when that note is gone.
# ---------------------------------------------------------------------------


def _highlighted_note_id(table: DataTable) -> str | None:
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


def test_escape_from_editor_keeps_the_same_row_highlighted(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "first")
        save(conn, "note-b", "second")
        save(conn, "note-c", "third")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str | None, str | None]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            # Newest-first: note-c, note-b, note-a. Move down once to
            # highlight the middle row (note-b), not the default top row --
            # otherwise a bug that always leaves the cursor on the top row
            # would pass unnoticed.
            await pilot.press("down")
            before = _highlighted_note_id(table)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            assert app.screen.note_id == "note-b"
            await pilot.press("escape")  # unchanged buffer -- pops immediately
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            after = _highlighted_note_id(
                app.screen.query_one(f"#{TABLE_ID}", DataTable)
            )
            return before, after

    before, after = asyncio.run(_drive())

    assert before == "note-b"
    assert after == "note-b"


def test_fresh_f3_after_editing_keeps_the_same_row_highlighted(
    tmp_path: Path,
) -> None:
    """The same reload path (``on_screen_resume``) runs after an edit + save,

    not just after opening the editor -- guards that the restore lives in
    ``_reload_rows`` itself, not something special-cased to a single entry
    path into :class:`EditScreen`.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "first")
        save(conn, "note-b", "second")
        save(conn, "note-c", "third")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("down")  # highlight note-b
            assert _highlighted_note_id(table) == "note-b"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("escape")  # unchanged buffer -- pops immediately
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            return _highlighted_note_id(app.screen.query_one(f"#{TABLE_ID}", DataTable))

    after = asyncio.run(_drive())

    assert after == "note-b"


def test_returning_falls_back_to_top_when_the_highlighted_note_is_gone(
    tmp_path: Path,
) -> None:
    """If the previously-highlighted note was deleted meanwhile, fall back

    to the top row rather than raising or leaving the cursor stranded.
    """
    from lode.versions import delete as versions_delete

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "first")
        b_head = save(conn, "note-b", "second").version_id
        save(conn, "note-c", "third")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str | None, list[str]]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("down")  # highlight note-b
            assert _highlighted_note_id(table) == "note-b"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            # note-b is deleted while its editor is open, out from under the
            # still-highlighted row.
            conn = init_db(db_path)
            try:
                versions_delete(conn, "note-b", parent=b_head)
            finally:
                conn.close()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            summaries = [str(table.get_row_at(i)[3]) for i in range(table.row_count)]
            return _highlighted_note_id(table), summaries

    highlighted, summaries = asyncio.run(_drive())

    assert summaries == ["third", "first"]  # note-b gone, newest-first order
    assert highlighted == "note-c"  # fell back to the (new) top row


# ---------------------------------------------------------------------------
# Content viewer + 'v' addressing flow (lode-olmi.8's decision, lode-0sjj) --
# bare 'v' on a Browse row (DataTable focused, safe for a bare key) and
# Ctrl+R from the editor (body TextArea focused, so a non-printable key is
# required -- docs/keybindings.md) both resolve the same zero/one/many
# addressing rule (_view_note_external_content) and land on the same
# SnapshotViewerScreen / ExternalPickerScreen.
# ---------------------------------------------------------------------------


def test_v_with_no_externals_notifies_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "a note with nothing retrieved")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    stayed_on_browse = asyncio.run(_drive())

    assert stayed_on_browse
    assert any("no retrieved content" in message for message in messages)


def test_v_with_one_external_opens_the_viewer_directly(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "see https://example.com/article").version_id
        _insert_external(
            conn,
            external_id="https://example.com/article",
            snapshot_id="snap-view-1",
            body="the extracted article text",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://example.com/article",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, SnapshotViewerScreen)
            return app.screen.query_one(f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea).text

    body_text = asyncio.run(_drive())

    assert body_text == "the extracted article text"


def test_v_with_many_externals_opens_the_picker_first(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(
            conn, "note-a", "see https://a.example.com/ and https://b.example.com/"
        ).version_id
        _insert_external(
            conn,
            external_id="https://a.example.com/",
            snapshot_id="snap-view-a",
            body="body a",
            fetched_at="2026-07-08T00:00:00.000000Z",
        )
        _insert_external(
            conn,
            external_id="https://b.example.com/",
            snapshot_id="snap-view-b",
            body="body b",
            fetched_at="2026-07-09T00:00:00.000000Z",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://a.example.com/",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://b.example.com/",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[list[tuple], str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, ExternalPickerScreen)
            table = app.screen.query_one(f"#{EXTERNAL_PICKER_TABLE_ID}", DataTable)
            rows = [tuple(table.get_row_at(i)) for i in range(table.row_count)]
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SnapshotViewerScreen)
            body_text = app.screen.query_one(
                f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea
            ).text
            return rows, body_text

    rows, body_text = asyncio.run(_drive())

    assert len(rows) == 2
    assert str(rows[0][0]) == "web"
    assert short_version_id("snap-view-a") in str(rows[0][1])
    assert short_version_id("snap-view-b") in str(rows[1][1])
    # Selected the second (b.example.com) row -- its body, not a's.
    assert body_text == "body b"


def test_external_picker_table_sets_empty_message(tmp_path: Path) -> None:
    """ExternalPickerScreen sets ``empty_message`` on mount (lode-ligf).

    This screen is only ever pushed with >1 external (the "many" branch of
    the zero/one/many addressing rule), so the table is never actually
    reached empty in practice -- the assertion is that the attribute is
    configured at all, matching the other bare-blank tables' adoption.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(
            conn, "note-a", "see https://a.example.com/ and https://b.example.com/"
        ).version_id
        _insert_external(
            conn, external_id="https://a.example.com/", snapshot_id="snap-empty-a"
        )
        _insert_external(
            conn, external_id="https://b.example.com/", snapshot_id="snap-empty-b"
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://a.example.com/",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://b.example.com/",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, ExternalPickerScreen)
            table = app.screen.query_one(f"#{EXTERNAL_PICKER_TABLE_ID}", LodeDataTable)
            return table.empty_message

    empty_message = asyncio.run(_drive())

    assert empty_message == "No externals for this note."


def test_external_picker_source_type_with_brackets_renders_literally(
    tmp_path: Path,
) -> None:
    """A ``source_type`` containing ``[...]`` renders literally (lode-ix4i).

    Low-severity in practice (``source_type`` is enum-ish today), but this
    picker duplicates the same data the sibling
    :func:`~lode.tui.screens._browse_render._external_text` renderer already
    protects with ``Text`` -- exercised here through a real Rich console, not
    ``get_row_at`` alone (stored value, not the render).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "see https://a.example.com/").version_id
        _insert_external(
            conn,
            external_id="https://a.example.com/",
            source_type="[web]",
            snapshot_id="snap-brk-a",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://a.example.com/",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
        _insert_external(
            conn,
            external_id="https://b.example.com/",
            source_type="web",
            snapshot_id="snap-brk-b",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://b.example.com/",
            source_version=head,
            source="user",
            reason="pasted URL",
            confidence=1.0,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> object:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, ExternalPickerScreen)
            table = app.screen.query_one(f"#{EXTERNAL_PICKER_TABLE_ID}", DataTable)
            return table.get_row_at(0)[0]

    cell = asyncio.run(_drive())

    console = Console(file=io.StringIO(), width=40, legacy_windows=False)
    console.print(cell)
    assert console.file.getvalue().strip() == "[web]"


def test_external_picker_table_scrolls_within_its_own_pane_not_the_whole_screen(
    tmp_path: Path,
) -> None:
    """Locks in lode-efn2: the picker table stays bounded above the Footer.

    Same standing as the version-history guard above -- a property guard,
    NOT proof of a fix. ExternalPickerScreen.compose is Header()/DataTable/
    Footer(), so the table is the Screen's sole non-docked child and
    DataTable's own ``max-height: 100%`` already bounded it; this test
    passes with the blanket ``DataTable { height: 1fr; }`` rule removed.
    See that test's docstring for the full mechanism and for why the test
    still earns its keep.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        urls = [f"https://example.com/article-{i}" for i in range(30)]
        head = save(conn, "note-a", "see " + " and ".join(urls)).version_id
        for i, url in enumerate(urls):
            _insert_external(
                conn,
                external_id=url,
                snapshot_id=f"snap-scroll-{i}",
                fetched_at=f"2026-07-{(i % 28) + 1:02d}T00:00:00.000000Z",
            )
            _insert_edge(conn, from_id="note-a", to_id=url, source_version=head)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[object, ...]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ExternalPickerScreen)
            table = screen.query_one(f"#{EXTERNAL_PICKER_TABLE_ID}", DataTable)
            header = screen.query_one(Header)
            footer = screen.query_one(Footer)
            return (
                screen.size,
                screen.max_scroll_y,
                header.region,
                footer.region,
                table.region,
                table.virtual_size,
            )

    (
        screen_size,
        screen_max_scroll_y,
        header_region,
        footer_region,
        table_region,
        table_virtual_size,
    ) = asyncio.run(_drive())

    # 30 externals is genuinely more content than fits an 80x24 terminal --
    # this test would be vacuous without it.
    assert table_virtual_size.height > table_region.height

    # The screen itself never scrolls...
    assert screen_max_scroll_y == 0
    # ...and Header/Footer -- both docked -- stay on-screen.
    assert header_region.y == 0
    assert footer_region.y + footer_region.height == screen_size.height

    # THE property being pinned: the table's own region ends at or above the
    # Footer's row, so it never extends past the visible window. (Currently
    # supplied by DataTable's own max-height: 100% as much as by the blanket
    # 1fr rule -- see the docstring; this holds either way, by design.)
    assert table_region.y + table_region.height <= footer_region.y


def test_escape_steps_back_picker_then_browse(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(
            conn, "note-a", "see https://a.example.com/ and https://b.example.com/"
        ).version_id
        _insert_external(
            conn, external_id="https://a.example.com/", snapshot_id="snap-esc-a"
        )
        _insert_external(
            conn, external_id="https://b.example.com/", snapshot_id="snap-esc-b"
        )
        _insert_edge(
            conn, from_id="note-a", to_id="https://a.example.com/", source_version=head
        )
        _insert_edge(
            conn, from_id="note-a", to_id="https://b.example.com/", source_version=head
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, ExternalPickerScreen)
            await pilot.press("escape")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    back_on_browse = asyncio.run(_drive())

    assert back_on_browse


def test_toggle_raw_switches_to_raw_html_and_back(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "see https://example.com/article").version_id
        _insert_external(
            conn,
            external_id="https://example.com/article",
            snapshot_id="snap-toggle-1",
            body="extracted text",
            raw_payload="<html>raw markup</html>",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://example.com/article",
            source_version=head,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, str, str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, SnapshotViewerScreen)
            initial = app.screen.query_one(f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea).text
            await pilot.press("t")
            await pilot.pause()
            toggled = app.screen.query_one(f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea).text
            await pilot.press("t")
            await pilot.pause()
            back = app.screen.query_one(f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea).text
            return initial, toggled, back

    initial, toggled, back = asyncio.run(_drive())

    assert initial == "extracted text"
    assert toggled == "<html>raw markup</html>"
    assert back == "extracted text"


def test_toggle_raw_with_no_raw_payload_notifies_and_stays_on_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No raw_payload captured -> 't' notifies and the body stays put --

    never a blank toggle (acceptance criteria).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "see https://example.com/article").version_id
        _insert_external(
            conn,
            external_id="https://example.com/article",
            snapshot_id="snap-toggle-2",
            body="extracted text only",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://example.com/article",
            source_version=head,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []

    async def _drive() -> str:
        async with app.run_test() as pilot:
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            return app.screen.query_one(f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea).text

    body_text = asyncio.run(_drive())

    assert body_text == "extracted text only"
    assert any("no raw HTML" in message for message in messages)


def test_escape_dismisses_the_snapshot_viewer_back_to_browse(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "see https://example.com/article").version_id
        _insert_external(
            conn,
            external_id="https://example.com/article",
            snapshot_id="snap-esc-viewer",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://example.com/article",
            source_version=head,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, SnapshotViewerScreen)
            await pilot.press("escape")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    back_on_browse = asyncio.run(_drive())

    assert back_on_browse


def test_v_on_an_empty_browse_list_is_a_no_op_not_a_crash(tmp_path: Path) -> None:
    """No highlighted row (empty list) -> 'v' opens nothing and does not raise.

    Mirrors the same empty-list contract 'i' (inspect) and 'e' (edit) hold --
    see ``test_i_on_an_empty_browse_list_is_a_no_op_not_a_crash`` above.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert app.screen.query_one(f"#{TABLE_ID}", DataTable).row_count == 0
            await pilot.press("v")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    stayed_on_browse = asyncio.run(_drive())

    assert stayed_on_browse


def test_ctrl_r_from_editor_opens_the_content_viewer(tmp_path: Path) -> None:
    """Ctrl+R (not bare ``v`` -- the body TextArea is editable) opens the same

    content viewer Browse's bare ``v`` binding does (lode-0sjj).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "see https://example.com/article").version_id
        _insert_external(
            conn,
            external_id="https://example.com/article",
            snapshot_id="snap-ctrl-r",
            body="from the editor",
        )
        _insert_edge(
            conn,
            from_id="note-a",
            to_id="https://example.com/article",
            source_version=head,
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert isinstance(app.screen, SnapshotViewerScreen)
            return app.screen.query_one(f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea).text

    body_text = asyncio.run(_drive())

    assert body_text == "from the editor"


def test_bare_v_from_editor_types_into_the_body_instead(tmp_path: Path) -> None:
    """Bare ``v`` is consumed by the editable body TextArea, not a Screen binding.

    Guards against a regression back to a bare-letter binding for the content
    viewer (lode-0sjj) -- exactly the trap ``Ctrl+H``'s
    (``test_bare_h_from_editor_types_into_the_body_instead``) and ``Ctrl+G``'s
    (``test_bare_i_from_editor_types_into_the_body_instead``) own guard tests
    cover for their own actions.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "very informative body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("v")
            await pilot.pause()
            text = app.screen.query_one(f"#{EDIT_BODY_ID}", TextArea).text
            return text, isinstance(app.screen, SnapshotViewerScreen)

    text, opened_viewer = asyncio.run(_drive())

    assert text == "vvery informative body"
    assert not opened_viewer


# ---------------------------------------------------------------------------
# Compact footer bar (lode-l38d.3, widget lode-uczx) -- BrowseScreen.BINDINGS
# renders 6 SHOWN entries plus 5 shown App-level ones (LodeApp.BINDINGS) in
# one footer line; with the original, full-length descriptions that
# overflowed the 80-column bound this screen was originally sized to and
# Textual clipped the tail. The fix stays inside the stock Footer
# (compact=True + show_command_palette=False), now baked into the shared
# :class:`~lode.tui.widgets.lode_footer.LodeFooter` every screen composes instead of
# repeating the two flags per call site.
#
# lode-uczx: lode's minimum supported terminal width is 100 columns, not 80
# (docs/tui.md) -- this test's bound moved accordingly, and the extra room
# lets the labels this shortened ("Insp"/"Del"/"Exp") go back to full words.
#
# lode-11io: the App-level "Ask" binding (ctrl+l) renders in every screen's
# footer, including this one.
#
# The consumed-width assert (lode-3aen's backport) is the same lever
# tests/test_tui_app.py's Capture footer test documents: show_horizontal_
# scrollbar alone is necessary but not sufficient (Textual can squeeze
# 1-column gutters to 0 to absorb a small overflow and still report
# hscroll=False), so this test checks the real consumed width too. Confirmed
# non-vacuous: pushing this screen with the pre-fix bare ``Footer()`` (no
# compact, no show_command_palette=False) and these same restored labels
# measures consumed=123/hscroll=True at 100 columns -- both this assert and
# the hscroll one would have caught it.
#
# WHAT "EVERY BINDING" MEANS, CHANGED HERE (lode-2bt3.3). Before this
# ticket, the footer WAS the complete binding contract -- "every binding
# visible" was the only honest assertion, because a hidden binding would
# have been undiscoverable. lode-2bt3.2 shipped the keybinding help overlay
# (Ctrl+_/'?'), which lists every binding on a screen INCLUDING show=False
# ones (tests/test_tui_help_screen.py's own anti-drift gate, parametrized
# over this exact screen), so a footer entry hidden here is still fully live and
# one keypress away -- the footer is a hint surface now, not the contract
# (docs/tui.md). This test's own semantics change accordingly: it asserts
# every SHOWN binding fits (the layout guarantee this test has always made
# and still must -- a screen whose shown entries overflow 100 columns is
# still a bug), and relies on test_tui_help_screen.py's anti-drift gate,
# not a duplicate assertion here, to guarantee every HIDDEN binding
# (including "Expand" below) stays reachable via the overlay.
# ---------------------------------------------------------------------------


def test_browse_footer_fits_100_columns_with_every_shown_binding_visible(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, list[str], int]:
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            footer = app.screen.query_one(Footer)
            keys = [c for c in footer.children if isinstance(c, FooterKey)]
            descriptions = [c.description for c in keys]
            # Natural width, immune to the gutter-squeeze trap described above.
            consumed = sum(k.region.width for k in keys) + (len(keys) - 1)
            return footer.show_horizontal_scrollbar, descriptions, consumed

    has_hscroll, descriptions, consumed = asyncio.run(_drive())

    assert has_hscroll is False  # the bar fits -- nothing dropped/compressed
    # ...and it fits WITHOUT Textual collapsing the gutters to get there.
    assert consumed <= 100, f"footer really consumes {consumed}/100 columns"
    # 6 of the 7 screen-level bindings stay shown, plus 5 shown App-level
    # ones. "Up" (question_mark/search_backward) is gone -- search direction
    # is retired (lode-2bt3.1). "Quit" is hidden (show=False, lode-2bt3.2,
    # re-verified by lode-2bt3.3 -- see docs/keybindings.md); "Help" (Ctrl+_,
    # lode-2bt3.2's keybinding overlay) takes its slot instead. lode-2bt3.3:
    # "Expand" (toggle_summary) is now hidden too -- see BrowseScreen's own
    # BINDINGS comment for why it is the least-needed reminder of the
    # seven -- which pays for "View" -> "View content" and "S" -> "Quick"
    # being un-abbreviated. MEASURED at 97/100.
    assert descriptions == [
        "Back",
        "Inspect",
        "View content",
        "Delete",
        "Find",
        "Quick",  # BM25 quick search (lode-35nu.6) -- see BrowseScreen.BINDINGS'
        # own comment for why this label and not "Search"
        "Cfg",
        "Browse",
        "Tags",
        "Ask",
        "Help",
    ]


# ---------------------------------------------------------------------------
# EditScreen footer (lode-uczx, folding in lode-3aen) -- long the tightest of
# the eleven footer-bearing screens, previously kept under 100 columns by
# shortening labels ("View content"->"View", "Related"->"Rel",
# "History"->"Hist") as new App-level bindings landed.
#
# lode-2bt3.3 retires that pattern here: per-screen footer priority
# (Binding show=False, honest now that lode-2bt3.2's help overlay lists
# every binding, shown or not) replaces "shorten a label" as how this screen
# pays for width. "Related"/"History" are restored to full words; "View
# content" (view external content) and "Link" (open URL under cursor) are
# hidden instead, as the least-needed reminders -- both apply only to a
# subset of notes rather than every note this screen edits. See
# EditScreen's own BINDINGS comment (src/lode/tui/screens/edit.py) for the
# full reasoning, including why "Cfg" (App-level) stays abbreviated even
# though this screen itself now has slack to spare.
#
# WHAT "EVERY BINDING" MEANS, CHANGED HERE (lode-2bt3.3) -- same change as
# BrowseScreen's footer test above: this asserts every SHOWN binding fits;
# every HIDDEN one stays reachable via the help overlay
# (tests/test_tui_help_screen.py's own anti-drift gate, which is
# parametrized over EditScreen as well as BrowseScreen precisely because
# this ticket made this screen hide bindings too -- not duplicated here).
# ---------------------------------------------------------------------------


def test_edit_footer_fits_100_columns_with_every_shown_binding_visible(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, list[str], int]:
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            footer = app.screen.query_one(Footer)
            keys = [c for c in footer.children if isinstance(c, FooterKey)]
            descriptions = [c.description for c in keys]
            consumed = sum(k.region.width for k in keys) + (len(keys) - 1)
            return footer.show_horizontal_scrollbar, descriptions, consumed

    has_hscroll, descriptions, consumed = asyncio.run(_drive())

    assert has_hscroll is False  # the bar fits -- nothing dropped/compressed
    assert consumed <= 100, f"footer really consumes {consumed}/100 columns"
    # 6 of the 8 screen-level bindings stay shown, plus 4 shown App-level
    # ones (the App-level ctrl+l is shadowed by this screen's own "Ask", so
    # it renders once, at screen level).
    # "Related"/"History" restored to full words (lode-2bt3.3);
    # "View content"/"Link" now hidden instead (see the block comment
    # above). "Ask" is a SCREEN-level binding here too (lode-35nu.11.3, same
    # key/label as the App-level one it shadows -- docs/keybindings.md), so
    # it renders in binding-declaration order right after "Inspect" rather
    # than at the tail with the other App-level entries. "Quit" is hidden
    # (show=False, lode-2bt3.2, re-verified by lode-2bt3.3); "Help" (Ctrl+_)
    # takes its slot at the tail instead -- MEASURED at 89/100.
    assert descriptions == [
        "Save",
        "Back",
        "Related",
        "History",
        "Inspect",
        "Ask",
        "Cfg",
        "Browse",
        "Tags",
        "Help",
    ]
