"""Keybinding help overlay (lode-2bt3.2).

Drives the real Textual pilot per the repo's TUI test idiom
(``tests/test_tui_browse_screen.py``, ``tests/test_tui_app.py``). Covers: the
overlay opens from every screen (including the three text-entry screens,
where ``?`` alone cannot reach it -- the ticket's own "open problem"),
dismisses on Escape/``?`` and returns focus to the underlying screen
unchanged, and -- the anti-drift gate -- that its snapshot genuinely covers
every live ``Binding`` on a representative screen plus the app, INCLUDING
``show=False`` ones. Content is *derived*, so drift cannot take the shape of
a missed transcription; what the gate actually pins is the snapshot
mechanism itself -- if the pre-push capture ever regresses (say a refactor
drops ``HelpScreen.active_bindings``), the overlay silently narrows to its
own dismiss key and this suite fails.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.binding import BindingsMap

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
# binding exists that the overlay does not surface. LodeApp contributes the
# App-level show=False case (Ctrl+Q, hidden from the footer since lode-2bt3.2
# but still fully live); the shadow case is exercised separately below.
#
# PARAMETRIZED OVER BOTH HIDING SCREENS (lode-2bt3.3's technical review).
# This gate ran against BrowseScreen alone as "the representative screen",
# which was sound while no Screen-level binding anywhere was show=False.
# lode-2bt3.3 ends that: it hides "Expand" on BrowseScreen and "View
# content"/"Link" on EditScreen, and -- crucially -- it DELETED those three
# entries from the footer tests' `descriptions` asserts, citing this gate as
# what still guarantees they stay reachable. For BrowseScreen that citation
# was true; for EditScreen it was not, since this gate never visited that
# screen, so ctrl+r/ctrl+n would have been asserted by nothing at all. Both
# hiding screens are now covered here, which is the altitude the guarantee
# belongs at -- a per-screen assertion duplicated back into the footer tests
# is not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("screen_cls", "open_keys", "consumed_by_focus"),
    [
        (BrowseScreen, ("ctrl+b",), frozenset()),
        # EditScreen focuses a TextArea, which consumes every printable key
        # (TextArea.check_consume_key) -- so the App-level '?' convenience
        # binding genuinely is not reachable there and Textual correctly
        # drops it from active_bindings. That is lode-2bt3.2's own "open
        # problem", verified empirically by that ticket and the whole reason
        # Ctrl+_ exists as the reachable-everywhere binding; the overlay
        # omitting an unreachable binding is correct, not drift. Asserted
        # positively rather than merely excluded, below.
        (EditScreen, ("ctrl+b", "enter"), frozenset({"question_mark"})),
    ],
    ids=["browse", "edit"],
)
def test_overlay_snapshot_covers_every_screen_and_app_binding_incl_hidden(
    tmp_path: Path,
    screen_cls: type,
    open_keys: tuple[str, ...],
    consumed_by_focus: frozenset[str],
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
            for key in open_keys:
                await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, screen_cls)
            await pilot.press("ctrl+underscore")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            return dict(app.screen.active_bindings)

    snapshot = asyncio.run(_drive())

    # Punctuation keys are stored on Binding.key verbatim ("?") but
    # active_bindings' dict keys are Textual's own normalized key names
    # ("question_mark"). Rather than re-implement that normalization here,
    # feed each BINDINGS list through the same public BindingsMap Textual
    # builds internally and read its already-normalized keys back.
    expected_keys = (
        set(BindingsMap(iter(screen_cls.BINDINGS)).key_to_bindings)
        | set(BindingsMap(iter(LodeApp.BINDINGS)).key_to_bindings)
    ) - consumed_by_focus

    missing = expected_keys - snapshot.keys()
    assert not missing, (
        f"the help overlay's snapshot is missing bindings for: {sorted(missing)} "
        "-- a binding was added without the overlay picking it up (lode-2bt3.2's "
        "anti-drift gate)"
    )

    # show=False bindings MUST still be listed -- that's the entire point
    # (the ticket's own words). '?' is this ticket's own permanently hidden
    # binding, so it carries that assertion; ctrl+q is asserted on membership
    # and action only, deliberately NOT on show=False, since lode-2bt3.3 may
    # legitimately restore its footer entry and that must not fail a
    # help-overlay drift test (the footer tests own that decision).
    if "question_mark" not in consumed_by_focus:
        assert snapshot["question_mark"].binding.show is False
        assert snapshot["question_mark"].binding.action == "show_help"
    assert "ctrl+q" in snapshot
    assert snapshot["ctrl+q"].binding.action == "quit"

    # The other half of the consumed-by-focus story, asserted rather than
    # merely excluded above: a binding the focused widget swallows really is
    # absent from the snapshot (so the exemption is load-bearing, not a
    # vacuous subtraction), and Ctrl+_ -- the binding that exists precisely
    # because '?' cannot survive a focused TextArea -- is present on every
    # screen either way.
    for key in consumed_by_focus:
        assert key not in snapshot, (
            f"{key} was expected to be consumed by the focused widget on "
            f"{screen_cls.__name__}, but the overlay listed it as reachable"
        )
    assert "ctrl+underscore" in snapshot


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

    assert isinstance(snapshot["ctrl+l"].node, EditScreen), (
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


def test_ctrl_shift_minus_opens_the_overlay(tmp_path: Path) -> None:
    """lode-av50: under the Kitty keyboard protocol, a terminal that does
    not report associated text for Ctrl+Underscore (confirmed on iTerm2
    3.5+/macOS) never sends the legacy 0x1f byte -- it sends a CSI-u
    sequence Textual decodes to the key name ``ctrl+shift+minus`` instead
    (see the parser-level test below, and docs/keybindings.md's
    "Protocol-level failure" section for the full derivation). This proves
    the binding itself is wired to the same action -- it does NOT prove a
    real terminal sends this key name; that's inherently untestable without
    a live terminal, which the parser-level test below is honest about."""
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            assert isinstance(app.screen, CaptureScreen)
            await pilot.press("ctrl+shift+minus")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

    asyncio.run(_drive())


def test_kitty_protocol_csi_u_sequence_decodes_to_ctrl_shift_minus() -> None:
    """lode-av50's actual empirical finding, pinned at the source: feed the
    raw Kitty-protocol CSI-u byte sequence iTerm2 sends for Ctrl+Shift+-
    (codepoint 45 = '-', modifier byte 6 = ctrl+shift, no associated-text
    component) straight through Textual's own parser and assert it decodes
    to the key name this ticket binds, not to 'ctrl+underscore'.

    HONEST LIMIT: this exercises textual._xterm_parser.XTermParser in
    isolation -- it proves what Textual's installed 8.2.8 does with that
    exact byte sequence, which is the mechanism this ticket diagnosed and
    fixed. It does NOT prove iTerm2 (or any other terminal) actually puts
    that sequence on the wire for this key combo -- no interactive terminal
    is available in this build/CI environment, so that leg of verification
    cannot be automated here. See docs/keybindings.md's "Protocol-level
    failure: the Kitty keyboard protocol" section for the full account,
    including why the table-level check that covered the legacy byte could
    never have caught this."""
    from textual._xterm_parser import XTermParser

    parser = XTermParser(debug=False)
    events = list(parser.feed("\x1b[45;6u"))

    assert len(events) == 1
    assert events[0].key == "ctrl+shift+minus"
