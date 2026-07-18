"""Screen-level tests for the capture screen's Escape confirm guard (lode-0wj.1).

Escape used to discard a dirty buffer silently -- an easy vi-muscle-memory
footgun. Now a non-empty/non-whitespace buffer's Escape pops
:class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`
(Save/Discard/Cancel) instead of exiting straight away; an empty/whitespace
buffer still exits
immediately (covered in ``tests/test_tui_app.py``, alongside the Discard leg
of this same confirm flow). This file drives the remaining two legs -- Save
and Cancel -- plus that the dialog actually appears and leaves the buffer
untouched, all through the real Textual pilot the same way
``tests/test_tui_reconcile_screen.py`` drives the CAS-reject confirm flow.

**Popup-over-the-editor coverage (lode-1i8.4).**
``test_confirm_dialog_is_an_overlay_over_the_still_mounted_capture_screen``
pins the "popup, not a blank full screen" acceptance criterion at the level
these tests can actually check without a real terminal: the dialog is
pushed (not switched), so :class:`~lode.tui.screens.capture.CaptureScreen`
stays on the app's screen stack underneath it rather than being torn down.
The dialog's sizing/centering/border live in ``lode.tcss`` and aren't
asserted here -- that's a rendered-style concern outside a pilot test's
normal reach.
"""

import asyncio
import sqlite3
from pathlib import Path

from lode.tui.app import LodeApp
from lode.tui.screens.capture import BODY_ID, CaptureScreen
from lode.tui.screens.discard_confirm import CONFIRM_MESSAGE_ID, DiscardConfirmScreen


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_escape_on_dirty_buffer_shows_the_confirm_dialog(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "a note in progress"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DiscardConfirmScreen)
            still_running = app.is_running
            content = str(app.screen.query_one(f"#{CONFIRM_MESSAGE_ID}").content)
            return content, still_running

    message, still_running = asyncio.run(_drive())

    assert "ave" in message  # "(S)ave"
    assert "iscard" in message  # "(D)iscard"
    assert "ancel" in message  # "(C)ancel"
    # The dialog is still up -- nothing exited yet.
    assert still_running


def test_confirm_dialog_is_an_overlay_over_the_still_mounted_capture_screen(
    tmp_path: Path,
) -> None:
    """lode-1i8.4: a popup over the editor, not a screen that replaces it."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[type]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "a note in progress"
            await pilot.press("escape")
            await pilot.pause()
            return [type(screen) for screen in app.screen_stack]

    stack_types = asyncio.run(_drive())

    # The dialog is on top, but CaptureScreen is still mounted underneath it
    # rather than having been popped/replaced -- an overlay, not a navigation.
    assert stack_types[-1] is DiscardConfirmScreen
    assert CaptureScreen in stack_types[:-1]


def test_escape_on_whitespace_only_buffer_skips_the_confirm(tmp_path: Path) -> None:
    """Whitespace-only counts as empty -- same immediate-exit path (lode-0wj.1)."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "   \n  "
            await pilot.press("escape")

    asyncio.run(_drive())

    assert app.return_value is None
    assert not db_path.exists()


def test_confirm_save_saves_the_buffer_and_exits(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "saved via the confirm dialog"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("s")

    asyncio.run(_drive())

    note_id = app.return_value
    assert note_id is not None
    assert _rows(
        db_path,
        "SELECT body, op FROM versions WHERE note_id = ?",
        (note_id,),
    ) == [("saved via the confirm dialog", "create")]


def test_confirm_discard_exits_without_saving(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "discarded via the confirm dialog"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("d")

    asyncio.run(_drive())

    assert app.return_value is None
    assert not db_path.exists()


def test_confirm_cancel_returns_to_editing_with_buffer_intact(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "do not lose this"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, CaptureScreen)
            return app.screen.query_one(f"#{BODY_ID}").text, app.is_running

    body_text, still_running = asyncio.run(_drive())

    assert body_text == "do not lose this"
    assert still_running
    assert not db_path.exists()


def test_confirm_escape_also_cancels_back_to_editing(tmp_path: Path) -> None:
    """Escape inside the confirm dialog itself is a Cancel, not a second discard."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "still here"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, CaptureScreen)
            return app.is_running

    still_running = asyncio.run(_drive())

    assert still_running
