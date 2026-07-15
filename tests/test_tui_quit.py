"""App-level Ctrl+Q confirm-if-dirty (lode-0wj.8, lode-b14).

Ctrl+Q is a *global* ``App``-priority binding (``LodeApp.BINDINGS``,
``src/lode/tui/app.py``) that reaches the app from any screen, bypassing that
screen's own bindings — unlike Escape, no individual screen can intercept it.
So the guard lives on ``LodeApp.action_quit``, which asks the *current*
screen (via an optional ``confirm_quit()`` method) whether it has unsaved
state before exiting. This drives that override end to end through the real
Textual pilot, covering the ticket's three cases: a dirty capture buffer
confirms (reusing lode-0wj.1's Save/Discard/Cancel dialog), a clean capture
buffer quits immediately, and a non-capture screen (config, reached via F2)
quits immediately regardless of what the capture screen underneath is
holding.

**lode-b14** extends the same guard to :class:`~lode.tui.screens.browse.EditScreen`
(lode-0wj.6): Ctrl+Q there must end in ``self.app.exit()`` /
``self.app.exit(note_id)`` -- quitting the whole app -- never
``pop_screen`` (Escape's "back to browse" contract), and its dirty check is
"changed since loaded," not "non-empty" (a loaded existing version is never
empty).
"""

import asyncio
import sqlite3
from pathlib import Path

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.browse import EDIT_BODY_ID, EditScreen
from lode.tui.screens.capture import BODY_ID, CONFIRM_MESSAGE_ID, DiscardConfirmScreen
from lode.tui.screens.config import ConfigScreen
from lode.tui.screens.reconcile import ReconcileScreen
from lode.versions import save


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_ctrl_q_on_dirty_capture_shows_the_confirm_dialog(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "a note in progress"
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert isinstance(app.screen, DiscardConfirmScreen)
            still_running = app.is_running
            content = str(app.screen.query_one(f"#{CONFIRM_MESSAGE_ID}").content)
            return content, still_running

    message, still_running = asyncio.run(_drive())

    assert "ave" in message  # "(S)ave"
    assert "iscard" in message  # "(D)iscard"
    assert "ancel" in message  # "(C)ancel"
    assert still_running  # nothing exited yet -- still waiting on the answer


def test_ctrl_q_confirm_save_saves_the_buffer_and_exits(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "saved via ctrl+q"
            await pilot.press("ctrl+q")
            await pilot.pause()
            await pilot.press("s")

    asyncio.run(_drive())

    note_id = app.return_value
    assert note_id is not None
    assert _rows(
        db_path,
        "SELECT body, op FROM versions WHERE note_id = ?",
        (note_id,),
    ) == [("saved via ctrl+q", "create")]


def test_ctrl_q_confirm_discard_exits_without_saving(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "discarded via ctrl+q"
            await pilot.press("ctrl+q")
            await pilot.pause()
            await pilot.press("d")

    asyncio.run(_drive())

    assert app.return_value is None
    assert not db_path.exists()


def test_ctrl_q_confirm_cancel_returns_to_editing_with_buffer_intact(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "do not lose this"
            await pilot.press("ctrl+q")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            return app.screen.query_one(f"#{BODY_ID}").text, app.is_running

    body_text, still_running = asyncio.run(_drive())

    assert body_text == "do not lose this"
    assert still_running
    assert not db_path.exists()


def test_ctrl_q_on_clean_capture_quits_immediately(tmp_path: Path) -> None:
    """An empty/whitespace-only buffer has nothing to lose -- no confirm."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+q")

    asyncio.run(_drive())

    assert app.return_value is None
    assert not db_path.exists()


def test_ctrl_q_on_non_capture_screen_quits_immediately_even_if_capture_is_dirty(
    tmp_path: Path,
) -> None:
    """Ctrl+Q asks the CURRENT screen, not the whole stack.

    Reach the config screen (F2, pushed on top of capture) with a dirty
    capture buffer underneath, then Ctrl+Q: config has no unsaved state of
    its own (no ``confirm_quit``), so it quits right away -- the dirty
    capture buffer one level down never gets a say.
    """
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "dirty underneath, but not on this screen"
            await pilot.press("f2")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            await pilot.press("ctrl+q")

    asyncio.run(_drive())

    assert app.return_value is None
    assert not db_path.exists()


def test_ctrl_q_on_unchanged_edit_buffer_quits_immediately(tmp_path: Path) -> None:
    """The lode-0wj.6 distinction carries over: unchanged != empty, no confirm."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "untouched body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("ctrl+q")

    asyncio.run(_drive())

    assert app.return_value is None
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", ("note-a",)
    ) == [("untouched body",)]


def test_ctrl_q_on_dirty_edit_buffer_shows_the_confirm_dialog(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "a real edit"
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert isinstance(app.screen, DiscardConfirmScreen)
            still_running = app.is_running
            content = str(app.screen.query_one(f"#{CONFIRM_MESSAGE_ID}").content)
            return content, still_running

    message, still_running = asyncio.run(_drive())

    assert "ave" in message
    assert "iscard" in message
    assert "ancel" in message
    assert still_running


def test_ctrl_q_confirm_save_saves_the_edit_and_quits_the_app(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "saved via ctrl+q"
            await pilot.press("ctrl+q")
            await pilot.pause()
            await pilot.press("s")

    asyncio.run(_drive())

    assert app.return_value == "note-a"
    assert _rows(
        db_path,
        "SELECT body, op FROM versions WHERE note_id = ? ORDER BY rowid",
        ("note-a",),
    ) == [("original body", "create"), ("saved via ctrl+q", "update")]


def test_ctrl_q_confirm_discard_quits_without_saving(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "discarded via ctrl+q"
            await pilot.press("ctrl+q")
            await pilot.pause()
            await pilot.press("d")

    asyncio.run(_drive())

    assert app.return_value is None
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", ("note-a",)
    ) == [("original body",)]


def test_ctrl_q_confirm_cancel_returns_to_editing_with_buffer_intact_edit_screen(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "original body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "do not lose this"
            await pilot.press("ctrl+q")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            return app.screen.query_one(f"#{EDIT_BODY_ID}").text, app.is_running

    body_text, still_running = asyncio.run(_drive())

    assert body_text == "do not lose this"
    assert still_running


def test_ctrl_q_confirm_save_with_cas_conflict_shows_reconcile_then_quits_on_resolve(
    tmp_path: Path,
) -> None:
    """A conflict on Ctrl+Q's save still ends in quitting the app, not browse.

    ``ReconcileScreen`` is pushed with no ``on_resolved`` override here (see
    :meth:`~lode.tui.screens.browse.EditScreen._on_quit_confirm`), so its
    default -- exit the app -- is exactly Ctrl+Q's own "quit the whole app"
    contract, unlike the Ctrl+S/Escape save path which pops back to browse.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        original_head = save(conn, "note-a", "original body").version_id
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            conn = init_db(db_path)
            try:
                save(
                    conn,
                    "note-a",
                    "someone else's concurrent edit",
                    parent=original_head,
                )
            finally:
                conn.close()
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.text = "my conflicting edit"
            await pilot.press("ctrl+q")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, ReconcileScreen)
            await pilot.press("d")

    asyncio.run(_drive())

    assert app.return_value is None
    # Discard: the concurrent edit's head is untouched, no clobber.
    assert _rows(
        db_path,
        "SELECT body FROM versions WHERE note_id = ? ORDER BY rowid",
        ("note-a",),
    ) == [("original body",), ("someone else's concurrent edit",)]
