"""Screen-level tests for the ask screen (lode-mkc.2, wired up by lode-11io).

Drives the real widgets end to end via Textual's ``run_test`` pilot -- the
screen-level twin of ``tests/test_tui_ask.py``'s direct unit coverage of
:mod:`lode.tui.services.ask`. The pipeline itself is monkeypatched at
``lode.tui.screens.ask.run_ask`` (it does network I/O), the same way
``tests/test_cli.py`` keeps ``lode ask`` offline -- this file only proves the
screen wires question input -> background worker -> results pane, that the
result renders with its citation/provenance/withheld/abstention content, and
that the screen is registered in ``LodeApp.SCREENS`` per the shared app-shell
pattern (lode-mkc.1).

Before lode-11io, ``AskScreen`` was registered but unreachable -- nothing
pushed it, so every test here drove it via ``app.push_screen("ask")``
directly, the same blind spot that let the gap ship. The reachability and
escape-pop tests below drive it the real way instead: pressing ``ctrl+l``.
"""

import asyncio
from collections.abc import Iterable
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.widgets import Footer, Input, TextArea
from textual.widgets._footer import FooterKey

from lode.answer import Claim, Support
from lode.cited_answer import CitedAnswer
from lode.egress import WithheldCitation
from lode.tui.app import LodeApp
from lode.tui.services.ask import AskResult
from lode.tui.screens.ask import QUESTION_ID, RESULTS_ID, AskScreen
from lode.tui.screens.capture import CaptureScreen


def test_app_registers_ask_screen(tmp_path: Path) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["ask"] is AskScreen


def test_ctrl_l_pushes_the_ask_screen_from_the_default_screen(tmp_path: Path) -> None:
    """lode-11io: ctrl+l is the App-level binding that reaches Ask -- nothing
    else did before this ticket. Presses the key rather than calling
    ``push_screen("ask")`` directly, the blind spot that let the gap ship.
    """
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            assert isinstance(app.screen, CaptureScreen)
            await pilot.press("ctrl+l")
            await pilot.pause()
            assert isinstance(app.screen, AskScreen)

    asyncio.run(_drive())


def test_ctrl_a_is_not_bound_app_level_because_text_widgets_swallow_it() -> None:
    """ctrl+a is NOT used for Ask (ctrl+l is) -- it is the mnemonic pick that
    doesn't work: both ``TextArea`` (CaptureScreen/EditScreen's body) and
    ``Input`` (AskScreen's question field) already claim ``ctrl+a`` as a
    builtin cursor-to-line-start binding (``home,ctrl+a`` in their own
    ``BINDINGS``), so an App-level ``ctrl+a`` never fires from any text-entry
    screen and does not even render in the footer (docs/keybindings.md).

    The last assert is the actual guard, and it is the point of this test: it
    asserts the RULE (``LodeApp`` must not bind ctrl+a), not merely the reason
    the rule exists. Asserting only the two widget builtins would leave a
    later ticket free to re-spring the exact trap this test is named for --
    binding ctrl+a App-level keeps every widget assert true, renders nothing
    in any footer, and so passes the whole suite while the action sits
    silently dead on Capture/Ask/Edit.
    """

    def _claims_ctrl_a(bindings: Iterable[Binding]) -> bool:
        return any("ctrl+a" in binding.key.split(",") for binding in bindings)

    # Why ctrl+a cannot work: the focused text widget claims the key first.
    assert _claims_ctrl_a(TextArea.BINDINGS)
    assert _claims_ctrl_a(Input.BINDINGS)
    # The rule that follows -- ctrl+l (test above) is the App-level route to Ask.
    assert not _claims_ctrl_a(LodeApp.BINDINGS), (
        "ctrl+a is bound App-level, but TextArea/Input swallow it: the action "
        "is silently dead on the text-entry screens (Capture/Ask/Edit) and "
        "renders in no footer. Use ctrl+l instead (docs/keybindings.md)."
    )


def test_asking_a_question_renders_the_cited_claim_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    canned = AskResult(
        answer=CitedAnswer(
            claims=(
                Claim(
                    text="We chose OAuth for service auth.",
                    support=[Support(version_id="v1", quoted_span="use OAuth")],
                ),
            ),
            withheld_citations=(WithheldCitation(target_id="v9"),),
        ),
        as_of={"v1": "2026-06-18T00:00:00.000Z"},
    )
    asked_with: list[str] = []
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask",
        lambda db_path, question, **kwargs: (asked_with.append(question), canned)[1],
    )

    async def _drive() -> str:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "what did we decide about auth?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return app.screen.query_one(f"#{RESULTS_ID}").content

    rendered = asyncio.run(_drive())

    assert asked_with == ["what did we decide about auth?"]
    assert "We chose OAuth for service auth." in rendered
    assert "version v1" in rendered
    assert "as of 2026-06-18T00:00:00.000Z" in rendered
    assert '"use OAuth"' in rendered
    assert "[withheld] v9" in rendered


def test_asking_an_ungrounded_question_renders_the_abstention_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    abstained = AskResult(answer=CitedAnswer(claims=(), withheld_citations=()))
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask", lambda db_path, question, **kwargs: abstained
    )

    async def _drive() -> str:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "anything at all?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return app.screen.query_one(f"#{RESULTS_ID}").content

    rendered = asyncio.run(_drive())

    assert "Your notes don't answer this." in rendered


def test_escape_pops_back_to_the_previous_screen(tmp_path: Path) -> None:
    """lode-11io: Escape now pops (matching every sibling screen), it no
    longer exits the whole app -- that was only coherent while Ask was
    unreachable/standalone. Reached via ``ctrl+l`` (the real route) rather
    than ``push_screen`` directly, so the round trip is exercised end to end.
    """
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            assert isinstance(app.screen, CaptureScreen)
            await pilot.press("ctrl+l")
            await pilot.pause()
            assert isinstance(app.screen, AskScreen)
            await pilot.press("escape")
            await pilot.pause()
            # Two complementary levers, both load-bearing: the screen assert
            # catches an escape that fails to pop back, is_running catches the
            # old ``self.app.exit()`` (which leaves AskScreen showing while the
            # app tears down, so the screen assert alone would misreport why).
            assert isinstance(app.screen, CaptureScreen)
            assert app.is_running, "escape exited the app instead of popping"

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# AskScreen's own footer (lode-11io, closing lode-s58y at the root) -- before
# this ticket, AskScreen's Escape called ``self.app.exit()`` directly, so its
# own Screen-level "Quit" collided with the App-level ctrl+q "Quit", and
# lode-s58y's proposed fix (relabel to "Back") would have been a bug: BOTH
# actually quit, so the duplicate label was telling the truth. Once Escape
# genuinely pops (this ticket), the duplicate ACTION disappears -- the label
# is honestly "Back" and there is exactly one "Quit" left (ctrl+q).
# ---------------------------------------------------------------------------


def test_footer_shows_each_action_once_with_no_duplicate_quit(
    tmp_path: Path,
) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> list[str]:
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("ctrl+l")
            await pilot.pause()
            assert isinstance(app.screen, AskScreen)
            footer = app.screen.query_one(Footer)
            keys = [c for c in footer.children if isinstance(c, FooterKey)]
            return [c.description for c in keys]

    descriptions = asyncio.run(_drive())

    assert descriptions == ["Back", "Quit", "Cfg", "Browse", "Tags", "Ask"]
    assert descriptions.count("Quit") == 1
