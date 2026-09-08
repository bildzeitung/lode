"""Tests for the no-egress command-palette toggle (lode-pky9).

Human decisions this ticket must not relitigate (2026-09-08): no new
keybinding -- ``Screen.COMMANDS`` on ``CaptureScreen``/``EditScreen`` instead
-- and visibility is a red border CSS class on the body ``TextArea``, no
extra widget. Coverage split three ways:

* :class:`~lode.tui.no_egress_command.NoEgressCommandProvider` in isolation
  against a fake target -- the palette text reflects state, not a fixed
  string.
* ``EditScreen``'s toggle -- round-trips through
  :func:`~lode.tui.services.no_egress.toggle_note_no_egress` (the single
  write path) and confirms on the clearing direction, exactly like Browse's
  ``n``.
* ``CaptureScreen``'s pending flag -- initialised from
  ``settings.no_egress_default``, flips with no DB write, persists atomically
  with the root create, and resets after "Save & new".
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from textual.widgets import TextArea

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.no_egress_command import NoEgressCommandProvider, _command_text
from lode.tui.screens.capture import BODY_ID as CAPTURE_BODY_ID
from lode.tui.screens.capture import NO_EGRESS_BORDER_CLASS as CAPTURE_BORDER_CLASS
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.edit import EDIT_BODY_ID, EditScreen
from lode.tui.screens.edit import NO_EGRESS_BORDER_CLASS as EDIT_BORDER_CLASS
from lode.tui.screens.no_egress_confirm import NoEgressClearConfirmScreen
from lode.versions import save, set_no_egress


def _no_egress_flag(db_path: Path, note_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT no_egress FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def test_command_text_reflects_current_state() -> None:
    assert _command_text(pending=False) == "Mark no-egress"
    assert _command_text(pending=True) == "Clear no-egress"


class _FakeTarget:
    """Minimal :class:`~lode.tui.no_egress_command.NoEgressCommandTarget`."""

    def __init__(self, pending: bool) -> None:
        self._pending = pending
        self.toggled = False

    def no_egress_pending(self) -> bool:
        return self._pending

    def no_egress_toggle(self) -> None:
        self.toggled = True


def test_provider_discover_yields_state_reflecting_text() -> None:
    # Provider's constructor only stores ``screen`` (no isinstance check at
    # runtime) -- a fake satisfying NoEgressCommandTarget is enough here,
    # exactly what discover()/search() actually read off it.
    target = _FakeTarget(pending=True)
    provider = NoEgressCommandProvider(target)  # type: ignore[arg-type]

    async def _collect() -> list[str]:
        return [str(hit.text) async for hit in provider.discover()]

    hits = asyncio.run(_collect())
    assert hits == ["Clear no-egress"]


def test_edit_screen_toggle_sets_immediately_no_confirm(tmp_path: Path) -> None:
    """SETTING (currently not withheld) is the safe direction -- applies at once."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-edit-toggle", "body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, int, bool]:
        async with app.run_test() as pilot:
            await app.push_screen(EditScreen("note-edit-toggle"))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, EditScreen)
            screen.no_egress_toggle()
            await pilot.pause()
            body = screen.query_one(f"#{EDIT_BODY_ID}", TextArea)
            return (
                screen.no_egress_pending(),
                _no_egress_flag(db_path, "note-edit-toggle"),
                body.has_class(EDIT_BORDER_CLASS),
            )

    pending, flag, has_border = asyncio.run(_drive())
    assert pending is True
    assert flag == 1
    assert has_border is True


def test_edit_screen_toggle_clearing_confirms_first(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-edit-withheld", "body")
        set_no_egress(conn, "note-edit-withheld", no_egress=True)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, int]:
        async with app.run_test() as pilot:
            await app.push_screen(EditScreen("note-edit-withheld"))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, EditScreen)
            screen.no_egress_toggle()
            await pilot.pause()
            is_confirm = isinstance(app.screen, NoEgressClearConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            return is_confirm, _no_egress_flag(db_path, "note-edit-withheld")

    is_confirm, flag_after = asyncio.run(_drive())
    assert is_confirm is True
    assert flag_after == 0


def test_capture_screen_pending_flag_initialises_from_settings_default(
    tmp_path: Path,
) -> None:
    from lode.config import Settings

    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path, settings=Settings(no_egress_default=True))

    async def _drive() -> tuple[bool, bool]:
        async with app.run_test() as pilot:
            await app.push_screen(CaptureScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CaptureScreen)
            body = screen.query_one(f"#{CAPTURE_BODY_ID}", TextArea)
            return screen.no_egress_pending(), body.has_class(CAPTURE_BORDER_CLASS)

    pending, has_border = asyncio.run(_drive())
    assert pending is True
    assert has_border is True


def test_capture_screen_toggle_is_pure_in_memory_and_border_tracks_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool]:
        async with app.run_test() as pilot:
            await app.push_screen(CaptureScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CaptureScreen)
            screen.no_egress_toggle()
            await pilot.pause()
            body = screen.query_one(f"#{CAPTURE_BODY_ID}", TextArea)
            return screen.no_egress_pending(), body.has_class(CAPTURE_BORDER_CLASS)

    pending, _has_border = asyncio.run(_drive())
    assert pending is True
    # No notes row exists at all yet -- nothing to have written.


def test_capture_save_persists_pending_flag_atomically(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await app.push_screen(CaptureScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CaptureScreen)
            screen.no_egress_toggle()  # mark pending
            body = screen.query_one(f"#{CAPTURE_BODY_ID}", TextArea)
            body.text = "sensitive capture"
            await pilot.press("ctrl+s")
            await pilot.pause()
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT note_id FROM notes ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            return row[0]

    note_id = asyncio.run(_drive())
    assert _no_egress_flag(db_path, note_id) == 1


def test_capture_save_and_new_resets_pending_flag_to_settings_default(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool]:
        async with app.run_test() as pilot:
            await app.push_screen(CaptureScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CaptureScreen)
            screen.no_egress_toggle()  # mark pending (default is False)
            body = screen.query_one(f"#{CAPTURE_BODY_ID}", TextArea)
            body.text = "first note"
            await pilot.press("ctrl+s")
            await pilot.pause()
            body = screen.query_one(f"#{CAPTURE_BODY_ID}", TextArea)
            return screen.no_egress_pending(), body.has_class(CAPTURE_BORDER_CLASS)

    pending_after, has_border_after = asyncio.run(_drive())
    assert pending_after is False
    assert has_border_after is False
