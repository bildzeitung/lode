"""Styling gate for the five remaining .confirm-dialog dedups (lode-f0qf).

lode-1ip2 factored ``border: thick $primary; background: $panel;
padding: 1 2;`` into a shared ``.confirm-dialog`` class for the three
Yes/No confirms. This ticket applies the same class (and, where a dialog
also deviates to the larger 80%/80% popup size, an id-rule size override --
see ``lode.tcss``) to the five OTHER modals that used to hand-write the same
triple verbatim: :class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`,
:class:`~lode.tui.screens.enrichment_modal.EnrichmentModalScreen`,
:class:`~lode.tui.screens.related_note_modal.RelatedNoteModalScreen`,
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` and
:class:`~lode.tui.screens.help.HelpScreen`. None of these subclass
``YesNoConfirmScreen`` -- this is a pure styling dedup, not a base-class
change -- so this file exercises them directly through a real ``LodeApp``
pilot rather than reusing ``tests/test_tui_yes_no_confirm.py``'s bare-``App``
harness: three of the five (enrichment, related-note, snapshot) need real
seeded data for ``on_mount`` to succeed, and one ``LodeApp`` harness for all
five is simpler than splitting them across two.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.discard_confirm import DiscardConfirmScreen
from lode.tui.screens.enrichment_modal import (
    INSPECTOR_DIALOG_ID,
    EnrichmentModalScreen,
)
from lode.tui.screens.help import HELP_DIALOG_ID, HelpScreen
from lode.tui.screens.related_note_modal import (
    RELATED_MODAL_DIALOG_ID,
    RelatedNoteModalScreen,
)
from lode.tui.screens.snapshot_viewer import (
    SNAPSHOT_VIEWER_DIALOG_ID,
    SnapshotViewerScreen,
)
from lode.tui.services.related import RelatedNote

#: Terminal size the harness renders at.
SCREEN_WIDTH = 100
SCREEN_HEIGHT = 40


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


def _seed_snapshot(db_path: Path, *, snapshot_id: str, body: str) -> None:
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
                ("https://example.com/article", "web"),
            )
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
                "VALUES (?, ?, ?, 'ok')",
                (snapshot_id, "https://example.com/article", body),
            )
    finally:
        conn.close()


def _push_and_check(db_path: Path, dialog_id: str, factory, check) -> None:
    """Push ``factory(app)`` onto a real ``LodeApp`` and run ``check(screen, dialog)``.

    Same "check runs inside run_test" shape as
    ``tests/test_tui_yes_no_confirm.py``'s ``with_dialog`` -- once the app
    exits the widgets are unmounted, so the assertions have to happen while
    the pilot is still live.
    """
    app = LodeApp(db_path=db_path)

    async def _run() -> None:
        async with app.run_test(size=(SCREEN_WIDTH, SCREEN_HEIGHT)) as pilot:
            screen = factory(app)
            app.push_screen(screen)
            await pilot.pause()
            check(screen, screen.query_one(f"#{dialog_id}"))

    asyncio.run(_run())


def _check_framed_and_centered(screen, dialog) -> None:
    assert dialog.has_class("confirm-dialog"), (
        f"{type(screen).__name__}'s outer container must carry .confirm-dialog"
        " -- that class is the ONLY thing supplying its frame"
    )
    border_style, _border_color = dialog.styles.border.top
    assert border_style == "thick", f"{type(screen).__name__} rendered unframed"
    assert dialog.styles.padding.top == 1
    assert dialog.styles.padding.left == 2

    assert screen.styles.align_horizontal == "center"
    assert screen.styles.align_vertical == "middle"
    region = dialog.region
    assert region.x > 0 and region.y > 0, (
        f"{type(screen).__name__} rendered at the top-left corner "
        f"({region.x}, {region.y})"
    )
    assert region.width < SCREEN_WIDTH
    assert region.height < SCREEN_HEIGHT


def test_discard_confirm_is_framed_centered_and_small(tmp_path: Path) -> None:
    def check(screen, dialog) -> None:
        _check_framed_and_centered(screen, dialog)
        assert dialog.region.width == 50

    _push_and_check(
        tmp_path / "lode.db",
        "capture-confirm-dialog",
        lambda _app: DiscardConfirmScreen(),
        check,
    )


def test_enrichment_modal_is_framed_centered_and_large(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_note(db_path, "note-a", "a note body")

    def check(screen, dialog) -> None:
        _check_framed_and_centered(screen, dialog)
        assert dialog.region.width == int(SCREEN_WIDTH * 0.8)
        assert dialog.region.height == int(SCREEN_HEIGHT * 0.8)

    _push_and_check(
        db_path,
        INSPECTOR_DIALOG_ID,
        lambda _app: EnrichmentModalScreen(note_id="note-a"),
        check,
    )


def test_related_note_modal_is_framed_centered_and_large(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    version_id = _seed_note(db_path, "note-a", "a related note's body")
    note = RelatedNote(
        note_id="note-a",
        snippet="a related",
        age="just now",
        version_id=version_id,
        char_range="0:9",
    )

    def check(screen, dialog) -> None:
        _check_framed_and_centered(screen, dialog)
        assert dialog.region.width == int(SCREEN_WIDTH * 0.8)
        assert dialog.region.height == int(SCREEN_HEIGHT * 0.8)

    _push_and_check(
        db_path,
        RELATED_MODAL_DIALOG_ID,
        lambda _app: RelatedNoteModalScreen(note),
        check,
    )


def test_snapshot_viewer_is_framed_centered_and_large(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_snapshot(db_path, snapshot_id="snap-1", body="the extracted article")

    def check(screen, dialog) -> None:
        _check_framed_and_centered(screen, dialog)
        assert dialog.region.width == int(SCREEN_WIDTH * 0.8)
        # Unlike the other 80%-height dialogs, this screen also composes a
        # docked LodeFooter sibling (lode-ev5j.3, dock: bottom; height: 1),
        # which claims one row BEFORE the percentage resolves -- so 80% is
        # taken of what's left, not of the full screen. Written as that
        # mechanism rather than as the other dialogs' expectation minus a bare
        # `- 1`: the two happen to coincide at SCREEN_HEIGHT = 40, but only
        # this form stays correct if that constant changes.
        assert dialog.region.height == int((SCREEN_HEIGHT - 1) * 0.8)

    _push_and_check(
        db_path,
        SNAPSHOT_VIEWER_DIALOG_ID,
        lambda _app: SnapshotViewerScreen("snap-1"),
        check,
    )


def test_help_screen_is_framed_centered_and_large(tmp_path: Path) -> None:
    def check(screen, dialog) -> None:
        _check_framed_and_centered(screen, dialog)
        assert dialog.region.width == int(SCREEN_WIDTH * 0.8)
        assert dialog.region.height == int(SCREEN_HEIGHT * 0.8)

    _push_and_check(
        tmp_path / "lode.db",
        HELP_DIALOG_ID,
        lambda app: HelpScreen(app.screen.active_bindings),
        check,
    )


def test_each_frame_declaration_appears_exactly_once() -> None:
    """Acceptance criterion 1, asserted mechanically -- not eyeballed.

    Deliberately counts the three declarations SEPARATELY rather than matching
    them as one contiguous block: a single-substring match would pin the
    stylesheet's exact declaration order and indentation (so an innocent
    reformat fails, reporting "duplication" that did not happen) while still
    letting a genuine re-duplication written in a different order slip
    through. Counting each declaration on its own is insensitive to ordering
    and whitespace inside the rule, and catches a partial copy too.
    """
    import lode.tui

    text = (Path(lode.tui.__file__).parent / "lode.tcss").read_text()
    for declaration in (
        "border: thick $primary;",
        "background: $panel;",
        "padding: 1 2;",
    ):
        assert text.count(declaration) == 1, (
            f"{declaration!r} must appear exactly once, in .confirm-dialog -- "
            "every other dialog must inherit the frame via that class"
        )
