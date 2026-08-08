"""Interactive stepping + highlighted-context modal for the related-notes panel (lode-olmi.9).

Covers the acceptance criterion straight: the passive panel
(:class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel`) is made interactive
without disturbing its existing debounce/render behavior (that's
``tests/test_tui_related.py``, ``tests/test_tui_edit_related_notes.py``, and the
related-notes bits of ``tests/test_tui_app.py``/
``tests/test_tui_capture_save_and_new.py`` -- untouched here). This file adds: stepping
through :attr:`RelatedNotesPanel._related` with Up/Down once the panel holds focus,
opening :class:`~lode.tui.screens.related_note_modal.RelatedNoteModalScreen` for the
selected note with Enter, Ctrl+F moving focus onto the panel from each composing screen,
and the modal's highlighted-context rendering itself
(:meth:`RelatedNoteModalScreen._highlighted_body`).
"""

import asyncio
from pathlib import Path

from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.capture import RELATED_ID, CaptureScreen
from lode.tui.screens.edit import EDIT_RELATED_ID, EditScreen
from lode.tui.screens.related_note_modal import (
    RELATED_MODAL_BODY_ID,
    RelatedNoteModalScreen,
)
from lode.tui.services.related import RelatedNote
from lode.tui.widgets.related_notes_panel import RelatedNotesPanel

_RELATED = [
    RelatedNote(note_id="note-a", snippet="a", age="1 day ago"),
    RelatedNote(note_id="note-b", snippet="b", age="2 days ago"),
    RelatedNote(note_id="note-c", snippet="c", age="3 days ago"),
]


def _seed_note(db_path: Path, note_id: str, body: str) -> str:
    """Save one note via the real ``Repository`` path; return its ``version_id``."""
    conn = init_db(db_path)
    try:
        result = Repository(conn, CompositeCache([LexicalCacheBackend(conn)])).save(
            note_id, body
        )
        return result.version_id
    finally:
        conn.close()


def test_down_and_up_step_through_related_notes_with_wraparound(
    tmp_path: Path,
) -> None:
    """Down/Up move the selection cursor through ``_related``, wrapping at the ends."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[int]:
        seen = []
        async with app.run_test() as pilot:
            assert isinstance(app.screen, CaptureScreen)
            panel = app.screen.query_one(f"#{RELATED_ID}", RelatedNotesPanel)
            panel._render_related(_RELATED)
            panel.focus()
            await pilot.pause()
            seen.append(panel._selected_index)
            await pilot.press("down")
            seen.append(panel._selected_index)
            await pilot.press("down")
            seen.append(panel._selected_index)
            await pilot.press("down")  # wraps 2 -> 0
            seen.append(panel._selected_index)
            await pilot.press("up")  # wraps 0 -> 2
            seen.append(panel._selected_index)
        return seen

    assert asyncio.run(_drive()) == [0, 1, 2, 0, 2]


def test_enter_on_empty_related_is_a_noop(tmp_path: Path) -> None:
    """Enter with nothing surfaced does nothing -- no crash, no modal pushed."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[int, bool]:
        async with app.run_test() as pilot:
            panel = app.screen.query_one(f"#{RELATED_ID}", RelatedNotesPanel)
            assert panel._related == []
            before = len(app.screen_stack)
            panel.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return before, len(app.screen_stack) == before

    before, unchanged = asyncio.run(_drive())
    assert before > 0
    assert unchanged  # no screen pushed


def test_ctrl_f_focuses_the_related_panel_from_capture(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            panel = app.screen.query_one(f"#{RELATED_ID}", RelatedNotesPanel)
            await pilot.press("ctrl+f")
            await pilot.pause()
            return app.focused is panel

    assert asyncio.run(_drive())


def test_ctrl_f_focuses_the_related_panel_from_edit(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_note(db_path, "note-a", "a note to edit")
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            panel = app.screen.query_one(f"#{EDIT_RELATED_ID}", RelatedNotesPanel)
            await pilot.press("ctrl+f")
            await pilot.pause()
            return app.focused is panel

    assert asyncio.run(_drive())


def test_enter_opens_modal_with_highlighted_matched_span(tmp_path: Path) -> None:
    """Enter on a selected related note opens the modal, body + highlight correct.

    The matched span is the exact ``char_range`` of the real saved note's
    body -- the modal must show the *verbatim* body (not the truncated
    snippet) with only that span styled, confirming
    :meth:`RelatedNoteModalScreen._highlighted_body` locates and stylizes the
    right slice rather than re-deriving it from the snippet.
    """
    db_path = tmp_path / "lode.db"
    body = "staging certificate rotation runbook, extended notes follow"
    version_id = _seed_note(db_path, "note-other", body)
    start = body.index("certificate rotation")
    end = start + len("certificate rotation")
    note = RelatedNote(
        note_id="note-other",
        snippet="certificate rotation",
        age="3 weeks ago",
        version_id=version_id,
        char_range=f"{start}:{end}",
    )
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, str, list]:
        async with app.run_test() as pilot:
            panel = app.screen.query_one(f"#{RELATED_ID}", RelatedNotesPanel)
            panel._render_related([note])
            panel.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            pushed = isinstance(app.screen, RelatedNoteModalScreen)
            static = app.screen.query_one(f"#{RELATED_MODAL_BODY_ID}")
            content = static.content
            return pushed, content.plain, content.spans

    pushed, plain, spans = asyncio.run(_drive())

    assert pushed
    assert plain == body
    assert spans == [(start, end, "reverse")]


def test_escape_dismisses_the_modal_back_to_the_composing_screen(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    version_id = _seed_note(db_path, "note-other", "a saved note")
    note = RelatedNote(
        note_id="note-other",
        snippet="a saved",
        age="just now",
        version_id=version_id,
        char_range="0:7",
    )
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            panel = app.screen.query_one(f"#{RELATED_ID}", RelatedNotesPanel)
            panel._render_related([note])
            panel.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RelatedNoteModalScreen)
            await pilot.press("escape")
            await pilot.pause()
            return isinstance(app.screen, CaptureScreen)

    assert asyncio.run(_drive())


def test_highlighted_body_falls_back_to_plain_on_malformed_char_range() -> None:
    """A malformed ``char_range`` never crashes the modal -- just no highlight."""
    note = RelatedNote(
        note_id="note-x", snippet="x", age="just now", version_id="v1", char_range=""
    )
    screen = RelatedNoteModalScreen(note)
    text = screen._highlighted_body("some body text")
    assert text.plain == "some body text"
    assert text.spans == []


def test_highlighted_body_falls_back_to_plain_on_out_of_range_char_range() -> None:
    """A ``char_range`` past the end of a (possibly stale) body is ignored, not clamped."""
    note = RelatedNote(
        note_id="note-x",
        snippet="x",
        age="just now",
        version_id="v1",
        char_range="0:999",
    )
    screen = RelatedNoteModalScreen(note)
    text = screen._highlighted_body("short body")
    assert text.plain == "short body"
    assert text.spans == []
