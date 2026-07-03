"""Tests for the Textual TUI shell + capture screen (lode-mkc.1).

Drives the real widgets end to end via Textual's ``run_test`` pilot: typing
into the capture screen's text area, pressing Ctrl+S, and asserting the note
actually landed via the same ``Repository.save`` seam ``lode add`` uses — the
screen-level twin of ``tests/test_tui_capture.py``'s direct unit coverage of
:func:`lode.tui.capture.save_capture`. Also covers the shell's screen
registration (``LodeApp.SCREENS``) and the discard-without-saving path.
"""

import asyncio
import sqlite3
from pathlib import Path

from lode.tui.app import LodeApp
from lode.tui.screens.capture import BODY_ID, CaptureScreen


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_app_registers_capture_as_the_starting_screen(tmp_path: Path) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["capture"] is CaptureScreen


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


def test_escape_discards_without_saving(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "never saved"
            await pilot.press("escape")

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
