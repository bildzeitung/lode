"""Tests for the Textual TUI shell + capture screen (lode-mkc.1, lode-mkc.3).

Drives the real widgets end to end via Textual's ``run_test`` pilot: typing
into the capture screen's text area, pressing Ctrl+S, and asserting the note
actually landed via the same ``Repository.save`` seam ``lode add`` uses — the
screen-level twin of ``tests/test_tui_capture.py``'s direct unit coverage of
:func:`lode.tui.services.capture.save_capture`. Also covers the shell's screen
registration (``LodeApp.SCREENS``), the discard-without-saving path, and
(lode-mkc.3) that typing actually drives the passive related-notes panel
end to end through the real debounce timer + Textual worker — the
screen-level twin of ``tests/test_tui_related.py``'s direct unit coverage of
:func:`lode.tui.services.related.find_related_notes`.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from lode.config import Settings
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.widgets.related_notes_panel import RelatedNotesPanel
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


def test_ctrl_s_saves_the_typed_note_and_stays_in_the_app(tmp_path: Path) -> None:
    """Ctrl+S on the capture screen is stack-aware "Save & New" (lode-bsmc):

    this screen is always the bottom of the stack, so a clean save resets the
    buffer and stays in the app rather than exiting. The dedicated Ctrl+S
    coverage (reset, focus, notify, CAS conflict, related-notes cleanup) lives
    in ``tests/test_tui_capture_save_and_new.py``; this is the screen-level
    twin of that file's save-path assertion.
    """
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "hello from the capture screen"
            await pilot.press("ctrl+s")
            await pilot.pause()
            return app.is_running

    still_running = asyncio.run(_drive())

    # The screen's Ctrl+S handler saves and stays -- it no longer exits.
    assert still_running
    assert app.return_value is None
    assert _rows(
        db_path,
        "SELECT body, op FROM versions",
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
    # module that actually holds the reference. lode-0wj.4: the related-notes
    # panel constructs its own shared embedder (RelatedNotesPanel._ensure_embedder,
    # lode-aoc) rather than leaving find_related_notes build one internally, so
    # the patch target is lode.embedding (what _ensure_embedder imports from),
    # not lode.tui.services.related.
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _StubEmbedder)

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
            # Read the panel's state before the pilot context tears the
            # screen stack down (app.screen is unavailable once it exits).
            related = app.screen.query_one(RelatedNotesPanel)._related

    asyncio.run(_drive())

    assert [note.note_id for note in related] == ["note-a"]


class _CountingStubEmbedder(_StubEmbedder):
    """Counts constructions, to pin lode-0wj.4's actual fix: one instance, reused.

    Before lode-0wj.4, ``find_related_notes`` built a fresh embedder every
    debounce fire (the ONNX model's cold *construction* -- not inference,
    which the lode-0wj.2 spike already cleared -- turned out to hold the GIL,
    a ~1.5s event-loop stall per pause in typing measured against the real
    embedder/corpus). This proves the screen now constructs its query embedder
    at most once for its whole lifetime, however many passes fire.
    """

    instances = 0

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        type(self).instances += 1


def test_embedder_is_constructed_once_and_reused_across_multiple_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-0wj.4's fix, pinned: 3 debounce-fired passes, 1 embedder construction."""
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    _CountingStubEmbedder.instances = 0
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _CountingStubEmbedder)

    settings = Settings(related_notes_debounce_ms=1, related_notes_min_chars=0)
    app = LodeApp(db_path=db_path, settings=settings)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            for draft in ("first pass draft", "second pass draft", "third pass"):
                text_area.text = draft
                await pilot.pause(0.1)
                await app.workers.wait_for_complete()

    asyncio.run(_drive())

    assert _CountingStubEmbedder.instances == 1


def test_related_panel_renders_snippet_with_markup_like_brackets(
    tmp_path: Path,
) -> None:
    """Verbatim note text with bracket sequences must not crash the panel render.

    Work notes routinely contain ``list[0]``, ``[link](url)``, ``[ERROR]`` etc.;
    the related-notes ``Static`` renders snippets verbatim (``markup=False``), so
    such a snippet must render as plain text rather than raising ``MarkupError``
    (lode-mkc.3). Drives ``RelatedNotesPanel._render_related`` on the real
    mounted widget.
    """
    from lode.tui.services.related import RelatedNote

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path, settings=Settings())

    async def _drive() -> None:
        async with app.run_test():
            screen = app.screen
            assert isinstance(screen, CaptureScreen)
            screen.query_one(RelatedNotesPanel)._render_related(
                [
                    RelatedNote(
                        "note-a",
                        "config uses Dict[str, int] and [ERROR] logs",
                        "3 weeks ago",
                    )
                ]
            )

    asyncio.run(_drive())  # raised MarkupError before markup=False


# ---------------------------------------------------------------------------
# Compact footer bar (lode-3rvw, widget lode-uczx) -- CaptureScreen.BINDINGS
# renders 3 entries (4 before lode-bsmc folded ctrl+n's "Save & new" onto
# ctrl+s and freed the letter) plus 5 App-level ones (LodeApp.BINDINGS) in one
# footer line; with the original, full-length descriptions that really
# consumed 100 columns and Textual clipped the tail against the 80-column
# bound this screen was originally sized to. The fix stays inside the stock
# Footer (compact=True + show_command_palette=False + shorter descriptions),
# now baked into the shared :class:`~lode.tui.widgets.lode_footer.LodeFooter` every
# screen composes instead of repeating the two flags per call site.
#
# lode-uczx: lode's minimum supported terminal width is 100 columns, not 80
# (docs/tui.md) -- this test's bound moved accordingly. Consumed width is
# intrinsic to the labels (identical at 80 and 100; only the budget moved).
#
# lode-11io: the App-level "Ask" binding (ctrl+l) renders in every screen's
# footer, including this one -- consumed moved from 77 to 84 (+7, matching
# the measurement app.py's own "Cfg" rationale comment records).
#
# lode-5ill: a 4th screen-level entry, "Link" (Ctrl+N open-link-under-cursor,
# reclaiming the same letter lode-bsmc freed for a different action -- see
# docs/keybindings.md), MEASURED consumed at 84/100 -- comfortably under the
# bound with no label shortening needed.
#
# TRAP (lode-3rvw review): show_horizontal_scrollbar is necessary but NOT
# sufficient, so this test does not rely on it alone. Textual separates the
# FooterKeys with 1-column gutters, and when the bar overflows only SLIGHTLY
# it squeezes those gutters to 0 to make it fit -- entries visibly run
# together, yet show_horizontal_scrollbar reports False. Measured against the
# shorter "New" label this review replaced: with compact=False that footer
# really consumed 86 columns but rendered at right edge 79/80 with hscroll
# False, so an hscroll-only assertion passes on the degraded bar. Today's
# labels overflow by more than the gutters can absorb, so hscroll happens to
# catch a dropped lever -- luck, not a guarantee, and the labels are the most
# likely thing to change next. Hence the second assertion, on the real
# consumed width = sum(key widths) + (n - 1): the formula lode-l38d.3
# established, and the one that survives the squeeze.
# ---------------------------------------------------------------------------


def test_capture_footer_fits_100_columns_with_every_binding_visible(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, list[str], int]:
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, CaptureScreen)
            footer = app.screen.query_one(Footer)
            keys = [c for c in footer.children if isinstance(c, FooterKey)]
            descriptions = [c.description for c in keys]
            # Natural width, immune to the gutter squeeze described above.
            consumed = sum(k.region.width for k in keys) + (len(keys) - 1)
            return footer.show_horizontal_scrollbar, descriptions, consumed

    has_hscroll, descriptions, consumed = asyncio.run(_drive())

    assert has_hscroll is False  # the bar fits -- nothing dropped/compressed
    # ...and it fits WITHOUT Textual collapsing the gutters to get there.
    assert consumed <= 100, f"footer really consumes {consumed}/100 columns"
    # All 4 screen-level + 5 App-level bindings stay visible (none hidden via
    # show=False) -- only their description text was shortened, and ctrl+s
    # keeps its full "Save & new" (lode-bsmc: folded onto ctrl+s from the now-
    # retired ctrl+n) so it cannot read as a discard-and-restart. "Link"
    # (lode-5ill: Ctrl+N open-link, reclaiming the same freed letter for a
    # different action) MEASURED at 84/100 total -- comfortably under the
    # bound, no label shortening needed.
    assert descriptions == [
        "Save & new",
        "Discard",
        "Related",
        "Link",
        "Quit",
        "Cfg",
        "Browse",
        "Tags",
        "Ask",
    ]


# ---------------------------------------------------------------------------
# The LodeFooter invariant (lode-uczx) -- every screen composes LodeFooter, no
# screen constructs the stock Footer itself.
#
# Why this test exists rather than a one-time grep: the bug lode-uczx was filed
# for is drift-by-DEFAULT -- a screen that forgets the two flags regresses
# silently -- and a grep at review time closes that for today's ten screens
# only, not for the eleventh. The three footer-width tests don't cover it
# either: they drive Browse/Capture/Edit, and the other seven screens consume
# 41-78 columns even bare, so reverting any of those seven to a stock Footer()
# passes the entire suite unnoticed (verified: the full suite is green with all
# seven reverted). That is precisely how CaptureScreen -- the app's own landing
# screen -- clipped past BrowseScreen's fix undetected (lode-3rvw), and this
# footer bug has now been independently rediscovered three times
# (lode-l38d.3 -> lode-3rvw -> lode-3aen) rather than caught by a gate.
#
# Checked at import level, not by grepping source text: a screen cannot build a
# stock Footer without importing it, and the runtime check can't be fooled by
# whitespace or a `f = Footer(); yield f` split that a text match would miss.
# ---------------------------------------------------------------------------


def test_no_screen_module_imports_the_stock_footer() -> None:
    import importlib
    import pkgutil

    import lode.tui.screens

    offenders = []
    for info in pkgutil.iter_modules(lode.tui.screens.__path__):
        module = importlib.import_module(f"lode.tui.screens.{info.name}")
        # LodeFooter is a Footer subclass, so identity -- not issubclass -- is
        # what distinguishes "imported the stock widget" from "imported ours".
        if getattr(module, "Footer", None) is Footer:
            offenders.append(info.name)

    assert offenders == [], (
        "these screen modules import Textual's stock Footer; compose "
        f"LodeFooter instead (lode-uczx): {offenders}"
    )
