"""Screen-level tests for EditScreen's passive related-notes panel (lode-aoc).

Pins the ticket's acceptance criterion end to end, the same style
``tests/test_tui_app.py``'s ``test_typing_surfaces_a_related_past_note`` pins
it for :class:`~lode.tui.screens.capture.CaptureScreen`: while editing an
existing note, a related past note surfaces passively via the real debounce
timer + Textual worker -- and the note being edited never appears in its own
related list (the edit-specific wrinkle
:class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel`'s ``exclude_note_id``
exists for).
"""

import asyncio
from pathlib import Path

import pytest

from lode.config import Settings
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.widgets.related_notes_panel import RelatedNotesPanel
from lode.tui.screens.edit import EDIT_BODY_ID, EDIT_RELATED_ID, EditScreen


class _StubEmbedder:
    """Offline stand-in for the query embedder (no ONNX model download).

    Only ``embed_query`` is exercised -- the seeded notes are indexed through
    the lexical leg only (mirrors ``tests/test_tui_app.py``'s convention), so
    the dense leg's LanceDB table stays empty and contributes nothing.
    """

    def __init__(self, settings: Settings) -> None:
        self._dim = settings.embedding_vector_dim

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0] * self._dim


def test_editing_surfaces_a_related_past_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion end to end: editing surfaces a related note,
    with the same debounce/knob behavior capture has (lode-aoc).

    ``BrowseScreen``'s table lists notes newest-first and row-select opens
    whatever row the cursor sits on (row 0 by default, lode-olmi.2) -- so the
    *second* saved note (``note-under-edit``) is the one that gets opened,
    not the first.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))
    repo.save("note-other", "certificate rotation notes from last quarter")
    repo.save("note-under-edit", "staging certificate rotation runbook")
    conn.close()

    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _StubEmbedder)
    settings = Settings(related_notes_debounce_ms=1, related_notes_min_chars=0)
    app = LodeApp(db_path=db_path, settings=settings)

    async def _drive() -> list:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            assert app.screen.note_id == "note-under-edit"
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "staging certificate rotation runbook, extended a bit"
            await pilot.pause(0.1)
            await app.workers.wait_for_complete()
            panel = app.screen.query_one(f"#{EDIT_RELATED_ID}", RelatedNotesPanel)
            return panel._related

    related = asyncio.run(_drive())

    assert [note.note_id for note in related] == ["note-other"]


def test_editing_a_note_never_surfaces_the_note_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit-specific wrinkle: without exclusion, the just-loaded buffer
    would trivially match its own note (lode-aoc's whole reason to exist).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))
    repo.save("note-a", "staging certificate rotation runbook")
    conn.close()

    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _StubEmbedder)
    settings = Settings(related_notes_debounce_ms=1, related_notes_min_chars=0)
    app = LodeApp(db_path=db_path, settings=settings)

    async def _drive() -> list:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            # Loading the head into the buffer alone (no further typing) is
            # enough to fire a pass -- setting TextArea.text posts a Changed
            # message, same as typing does.
            await pilot.pause(0.1)
            await app.workers.wait_for_complete()
            panel = app.screen.query_one(f"#{EDIT_RELATED_ID}", RelatedNotesPanel)
            return panel._related

    related = asyncio.run(_drive())

    assert related == []


def test_edit_screen_panel_excludes_this_note_id_at_construction(
    tmp_path: Path,
) -> None:
    """Direct unit check: the composed panel's ``exclude_note_id`` is the edited note."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    Repository(conn, CompositeCache([LexicalCacheBackend(conn)])).save(
        "note-a", "a note to edit"
    )
    conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            panel = app.screen.query_one(f"#{EDIT_RELATED_ID}", RelatedNotesPanel)
            return panel.exclude_note_id

    exclude_note_id = asyncio.run(_drive())

    assert exclude_note_id == "note-a"
