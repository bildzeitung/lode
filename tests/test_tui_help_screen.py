"""Keybinding help overlay (lode-2bt3.2).

Drives the real Textual pilot per the repo's TUI test idiom
(``tests/test_tui_browse_screen.py``, ``tests/test_tui_app.py``). Covers: the
overlay opens from every screen (including the three text-entry screens,
where ``?`` alone cannot reach it -- the ticket's own "open problem"),
dismisses on Escape/``?`` and returns focus to the underlying screen
unchanged, and -- the anti-drift gate -- that its snapshot genuinely covers
every live ``Binding`` on a representative screen plus the app, INCLUDING
``show=False`` ones, so a future binding added without touching
``lode.tui.screens.help``/``lode.tui.app`` fails this suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.keys import _character_to_key

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.ask import AskScreen
from lode.tui.screens.browse import BrowseScreen
from lode.tui.screens.capture import BODY_ID, CaptureScreen
from lode.tui.screens.edit import EditScreen
from lode.tui.screens.help import HelpScreen
from lode.versions import save


def test_ctrl_underscore_opens_the_overlay_from_the_default_capture_screen(
    tmp_path: Path,
) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            assert isinstance(app.screen, CaptureScreen)
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

    asyncio.run(_drive())


def test_question_mark_opens_the_overlay_when_no_text_entry_holds_focus(
    tmp_path: Path,
) -> None:
    """The convenience '?' binding (freed by lode-2bt3.1) works from a
    non-text-entry screen -- Browse's DataTable, not a TextArea/Input."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

    asyncio.run(_drive())


def test_question_mark_is_swallowed_as_a_literal_character_on_capture(
    tmp_path: Path,
) -> None:
    """The ticket's own "open problem": '?' is printable, so a focused
    TextArea consumes it as a literal character rather than ever reaching
    the App-level binding -- this is why Ctrl+_ exists at all."""
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> str:
        async with app.run_test() as pilot:
            assert isinstance(app.screen, CaptureScreen)
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = ""
            await pilot.press("?")
            await pilot.pause()
            return text_area.text

    body_text = asyncio.run(_drive())

    assert body_text == "?"  # typed literally, not intercepted


def test_ctrl_underscore_reaches_the_overlay_from_every_text_entry_screen(
    tmp_path: Path,
) -> None:
    """Capture, Edit, and Ask are exactly the three screens where '?' is
    unreachable (the ticket's own acceptance criterion) -- Ctrl+_ survives
    TextArea/Input's is_printable filter and reaches all three."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, bool, bool]:
        async with app.run_test() as pilot:
            # CaptureScreen (the default screen).
            assert isinstance(app.screen, CaptureScreen)
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            on_capture = isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()

            # EditScreen.
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            on_edit = isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("escape")  # back to browse
            await pilot.pause()

            # AskScreen.
            await pilot.press("ctrl+l")
            await pilot.pause()
            assert isinstance(app.screen, AskScreen)
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            on_ask = isinstance(app.screen, HelpScreen)

            return on_capture, on_edit, on_ask

    on_capture, on_edit, on_ask = asyncio.run(_drive())

    assert on_capture, "Ctrl+_ did not open the overlay from CaptureScreen"
    assert on_edit, "Ctrl+_ did not open the overlay from EditScreen"
    assert on_ask, "Ctrl+_ did not open the overlay from AskScreen"


def test_escape_dismisses_and_restores_the_underlying_screen_unchanged(
    tmp_path: Path,
) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "unaffected by the overlay"
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, CaptureScreen)
            body = app.screen.query_one(f"#{BODY_ID}").text
            return body, app.is_running

    body_text, still_running = asyncio.run(_drive())

    assert body_text == "unaffected by the overlay"
    assert still_running


def test_question_mark_also_dismisses_the_overlay(tmp_path: Path) -> None:
    """'?' is bound on HelpScreen itself too (its own Close binding), not
    only Escape."""
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, CaptureScreen)

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Anti-drift gate (the ticket's own acceptance criterion): a test fails if a
# binding exists that the overlay does not surface. BrowseScreen is the
# representative screen -- 6 non-priority Screen-level bindings, none
# show=False, exercising the ordinary case; LodeApp contributes the
# show=False case (Ctrl+Q, hidden from the footer since lode-2bt3.2 but
# still fully live) and the shadow case is exercised separately below.
# ---------------------------------------------------------------------------


def test_overlay_snapshot_covers_every_browse_and_app_binding_incl_hidden(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> dict:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert isinstance(app.screen, BrowseScreen)
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            return dict(app.screen.active_bindings)

    snapshot = asyncio.run(_drive())

    # Punctuation keys are stored on Binding.key verbatim ("?") but
    # active_bindings' dict keys are Textual's own normalized key names
    # ("question_mark") -- the same single-character normalization
    # Binding.__post_init__ applies internally (textual.binding, via
    # textual.keys._character_to_key). Multi-character key names (ctrl+q,
    # escape, ...) are untouched by it.
    def _normalized(key: str) -> str:
        return _character_to_key(key) if len(key) == 1 else key

    expected_keys = {_normalized(binding.key) for binding in BrowseScreen.BINDINGS} | {
        _normalized(binding.key) for binding in LodeApp.BINDINGS
    }

    missing = expected_keys - snapshot.keys()
    assert not missing, (
        f"the help overlay's snapshot is missing bindings for: {sorted(missing)} "
        "-- a binding was added without the overlay picking it up (lode-2bt3.2's "
        "anti-drift gate)"
    )

    # show=False bindings MUST still be listed -- that's the entire point
    # (the ticket's own words). Ctrl+Q's footer entry was hidden by this
    # same ticket (lode-2bt3.2), but the binding itself is unchanged.
    assert "ctrl+q" in snapshot
    assert snapshot["ctrl+q"].binding.show is False
    assert snapshot["ctrl+q"].binding.action == "quit"


def test_edit_screen_shadow_hides_the_shadowed_app_level_ask_entry(
    tmp_path: Path,
) -> None:
    """docs/keybindings.md's shadow rule: EditScreen declares its own
    Screen-level Ctrl+L ("Ask about this note"), which resolves before --
    and therefore hides -- the App-level Ctrl+L it shares the key with. The
    overlay must reflect the winning (Screen-level) binding only, per its
    own "must not list the shadowed app binding as reachable" acceptance
    criterion -- never both."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> dict:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            return dict(app.screen.active_bindings)

    snapshot = asyncio.run(_drive())

    active = snapshot["ctrl+l"]
    assert active.node is EditScreen or isinstance(active.node, EditScreen), (
        "the App-level ctrl+l should be shadowed by EditScreen's own"
    )


def test_help_screen_is_footerless(tmp_path: Path) -> None:
    """docs/tui.md's modal rule (lode-ev5j.3): dismisses on escape/'?' and
    has no other standing action, so it must NOT compose a LodeFooter."""
    from lode.tui.widgets.lode_footer import LodeFooter

    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            return bool(app.screen.query(LodeFooter))

    has_footer = asyncio.run(_drive())

    assert not has_footer


def test_help_screen_registered_in_screens_for_discoverability(
    tmp_path: Path,
) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["help"] is HelpScreen
