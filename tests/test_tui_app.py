"""Tests for the Textual TUI shell + capture screen (lode-mkc.1, lode-mkc.3).

Drives the real widgets end to end via Textual's ``run_test`` pilot: typing
into the capture screen's text area, pressing Ctrl+S, and asserting the note
actually landed via the same ``Repository.save`` seam ``lode add`` uses — the
screen-level twin of ``tests/test_tui_capture.py``'s direct unit coverage of
:func:`lode.tui.capture.save_capture`. Also covers the shell's screen
registration (``LodeApp.SCREENS``), the discard-without-saving path, and
(lode-mkc.3) that typing actually drives the passive related-notes panel
end to end through the real debounce timer + Textual worker — the
screen-level twin of ``tests/test_tui_related.py``'s direct unit coverage of
:func:`lode.tui.related.find_related_notes`.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from lode.config import Settings
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.capture import BODY_ID, CaptureScreen
from lode.tui.screens.reconcile import ReconcileScreen


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_app_registers_capture_as_the_starting_screen(tmp_path: Path) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["capture"] is CaptureScreen


def test_app_registers_the_reconcile_screen(tmp_path: Path) -> None:
    """lode-mkc.4: registered via ``SCREENS`` like every other E11 screen."""
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["reconcile"] is ReconcileScreen


# Driven via plain ``asyncio.run`` rather than an async test + pytest-asyncio
# marker: Textual's ``run_test`` pilot needs an event loop, but pulling in a
# whole plugin for that is unwarranted when wrapping the body in one is free.


def test_ctrl_s_saves_the_typed_note_and_exits(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "hello from the capture screen"
            await pilot.press("ctrl+s")

    asyncio.run(_drive())

    # The screen's Ctrl+S handler exits the app with the saved note id.
    note_id = app.return_value
    assert note_id is not None
    assert _rows(
        db_path,
        "SELECT body, op FROM versions WHERE note_id = ?",
        (note_id,),
    ) == [("hello from the capture screen", "create")]


def test_escape_on_empty_buffer_discards_without_saving(tmp_path: Path) -> None:
    """Empty/whitespace-only buffer: Escape exits immediately, no confirm (lode-0wj.1)."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("escape")

    asyncio.run(_drive())

    assert app.return_value is None
    assert not db_path.exists()


def test_escape_on_dirty_buffer_then_discard_exits_without_saving(
    tmp_path: Path,
) -> None:
    """A non-empty buffer's Escape confirms first; choosing Discard exits (lode-0wj.1)."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "never saved"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("d")

    asyncio.run(_drive())

    assert app.return_value is None
    assert not db_path.exists()


def test_saving_an_empty_note_does_not_exit_or_write(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)
    still_running = False

    async def _drive() -> None:
        nonlocal still_running
        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            # App is still running (no exit on an empty-note refusal).
            still_running = app.is_running

    asyncio.run(_drive())

    assert still_running
    assert not db_path.exists()


class _StubEmbedder:
    """Offline stand-in for the query embedder (no ONNX model download).

    Only :meth:`embed_query` is exercised by this test — the seeded note is
    indexed through the lexical leg only (mirrors ``save_capture``'s cache
    composition: embed stays async/pending), so the dense leg's LanceDB table
    is empty and simply contributes nothing.
    """

    def __init__(self, settings: Settings) -> None:
        self._dim = settings.embedding_vector_dim

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0] * self._dim


def test_typing_surfaces_a_related_past_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion end to end: while writing, a related past note
    surfaces passively, via the real debounce timer + Textual worker (lode-mkc.3).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    Repository(conn, CompositeCache([LexicalCacheBackend(conn)])).save(
        "note-a", "staging certificate rotation runbook"
    )
    conn.close()

    # No model download in the gate: same "swap the default ONNX embedder"
    # convention tests/test_cli.py's _offline_embedder uses, aimed at the
    # module that actually holds the reference (lode.tui.related imports it
    # at module scope, unlike cli._retrieve's per-call import).
    monkeypatch.setattr("lode.tui.related.FastEmbedEmbedder", _StubEmbedder)

    settings = Settings(related_notes_debounce_ms=1, related_notes_min_chars=0)
    app = LodeApp(db_path=db_path, settings=settings)
    related: list = []

    async def _drive() -> None:
        nonlocal related
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "writing about certificate rotation again"
            # Let the 1ms debounce timer fire and the search worker run.
            await pilot.pause(0.1)
            await app.workers.wait_for_complete()
            # Read the screen's state before the pilot context tears the
            # screen stack down (app.screen is unavailable once it exits).
            related = app.screen._related

    asyncio.run(_drive())

    assert [note.note_id for note in related] == ["note-a"]


def test_related_panel_renders_snippet_with_markup_like_brackets(
    tmp_path: Path,
) -> None:
    """Verbatim note text with bracket sequences must not crash the panel render.

    Work notes routinely contain ``list[0]``, ``[link](url)``, ``[ERROR]`` etc.;
    the related-notes ``Static`` renders snippets verbatim (``markup=False``), so
    such a snippet must render as plain text rather than raising ``MarkupError``
    (lode-mkc.3). Drives ``_render_related`` on the real mounted widget.
    """
    from lode.tui.related import RelatedNote

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path, settings=Settings())

    async def _drive() -> None:
        async with app.run_test():
            screen = app.screen
            assert isinstance(screen, CaptureScreen)
            screen._render_related(
                [
                    RelatedNote(
                        "note-a",
                        "config uses Dict[str, int] and [ERROR] logs",
                        "3 weeks ago",
                    )
                ]
            )

    asyncio.run(_drive())  # raised MarkupError before markup=False
