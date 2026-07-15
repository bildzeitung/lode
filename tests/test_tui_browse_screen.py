"""Screen-level tests for the browse screen (lode-0wj.5).

Drives the real widgets end to end via Textual's ``run_test`` pilot, the same
style ``tests/test_tui_config.py`` / ``tests/test_tui_ask_screen.py`` use:
reaching the screen from capture via the app-level ``F3`` binding, the table's
contents/ordering, selecting a row to open a read-only note view, and the
"note -> list -> capture" Escape chain.
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import DataTable, Static, TextArea

from lode.ids import short_version_id
from lode.notes_read import short_note_id
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.dates import format_adaptive_date
from lode.tui.screens.browse import (
    DELETE_CONFIRM_MESSAGE_ID,
    HISTORY_TABLE_ID,
    INSPECTOR_EDGES_ID,
    INSPECTOR_EMBED_ID,
    INSPECTOR_ENTITIES_ID,
    INSPECTOR_STATE_ID,
    INSPECTOR_SUMMARY_ID,
    INSPECTOR_TAGS_ID,
    NOTE_BODY_ID,
    TABLE_ID,
    VERSION_BODY_ID,
    BrowseScreen,
    DeleteConfirmScreen,
    EditScreen,
    EnrichmentModalScreen,
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
            await pilot.press("f3")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            return str(table.get_row_at(0)[1])

    date_cell = asyncio.run(_drive())

    assert date_cell == format_adaptive_date(created)
    assert "T" not in date_cell  # never the raw ISO-8601 stamp


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
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("h")
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
            "INSERT INTO jobs (type, target_version, status) VALUES ('enrich', ?, ?)",
            (target_version, status),
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
            await pilot.pause()
            assert app.screen.query_one(f"#{TABLE_ID}", DataTable).row_count == 0
            await pilot.press("i")
            await pilot.pause()
            return isinstance(app.screen, BrowseScreen)

    stayed_on_browse = asyncio.run(_drive())

    assert stayed_on_browse


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
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, no_egress) "
            "VALUES (?, ?, ?)",
            (external_id, source_type, int(no_egress)),
        )
        conn.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, external_id, body, status, fetched_at) "
            "VALUES (?, ?, 'body', ?, ?)",
            (snapshot_id, external_id, status, fetched_at),
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
            await pilot.press("f3")
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
    between ``BrowseScreen`` re-resolving it (:func:`~lode.tui.edit.load_head`)
    and calling :func:`~lode.tui.edit.delete_note` -- not reachable from a
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
            await pilot.press("f3")
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
# Cursor preservation across a reload (lode-olmi.1) -- _reload_rows'
# clear(columns=True) used to discard the DataTable's cursor, so leaving a
# highlighted row to view/edit a note and Escaping back always snapped the
# cursor to the top. The fix captures the highlighted note_id before the
# rebuild and restores the cursor to that same row key afterward, falling
# back to the top only when that note is gone.
# ---------------------------------------------------------------------------


def _highlighted_note_id(table: DataTable) -> str | None:
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


def test_escape_from_note_view_keeps_the_same_row_highlighted(
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
            await pilot.press("f3")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            # Newest-first: note-c, note-b, note-a. Move down once to
            # highlight the middle row (note-b), not the default top row --
            # otherwise a bug that always leaves the cursor on the top row
            # would pass unnoticed.
            await pilot.press("down")
            before = _highlighted_note_id(table)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NoteViewScreen)
            assert app.screen.note_id == "note-b"
            await pilot.press("escape")
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
    """The same reload path (``on_screen_resume``) runs after ``e`` + save,

    not just after a plain view -- guards that the restore lives in
    ``_reload_rows`` itself, not something special-cased to ``NoteViewScreen``.
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
            await pilot.press("f3")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("down")  # highlight note-b
            assert _highlighted_note_id(table) == "note-b"
            await pilot.press("e")
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
            await pilot.press("f3")
            table = app.screen.query_one(f"#{TABLE_ID}", DataTable)
            await pilot.press("down")  # highlight note-b
            assert _highlighted_note_id(table) == "note-b"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NoteViewScreen)
            # note-b is deleted while its view is open, out from under the
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
