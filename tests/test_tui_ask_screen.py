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
import threading
from collections.abc import Iterable
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static, TextArea
from textual.widgets._footer import FooterKey

from lode.answer import Claim, Support
from lode.cited_answer import CitedAnswer
from lode.egress import WithheldCitation
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.ask import CITATION_STATUS_ID, QUESTION_ID, RESULTS_ID, AskScreen
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.edit import EditScreen
from lode.tui.screens.save_as_note_confirm import (
    SAVE_AS_NOTE_PREVIEW_ID,
    SaveAsNoteConfirmScreen,
)
from lode.tui.screens.snapshot_viewer import SnapshotViewerScreen
from lode.tui.screens.version_view import VersionViewScreen
from lode.tui.services.ask import STAGE_RETRIEVING, AskResult, CitationIdentity
from lode.versions import save


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


def test_ctrl_l_from_edit_screen_opens_ask_scoped_to_this_note(
    tmp_path: Path,
) -> None:
    """lode-35nu.11.3: EditScreen's own Screen-level ``ctrl+l`` shadows the
    App-level one and opens the note-scoped ask flow, not the corpus-wide one.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, str | None]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            await pilot.press("ctrl+l")
            await pilot.pause()
            is_ask = isinstance(app.screen, AskScreen)
            note_id = app.screen._note_id if is_ask else None
            return is_ask, note_id

    is_ask, note_id = asyncio.run(_drive())

    assert is_ask
    assert note_id == "note-a"


def test_ctrl_l_from_version_view_screen_opens_ask_scoped_to_this_note(
    tmp_path: Path,
) -> None:
    """lode-35nu.11.3: same shadowing on VersionViewScreen -- pins the NOTE
    (its live head), not the specific (possibly non-head) version being read.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "v1 body").version_id
        save(conn, "note-a", "v2 body", parent=head)
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, str | None]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+h")
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, VersionViewScreen)
            await pilot.press("ctrl+l")
            await pilot.pause()
            is_ask = isinstance(app.screen, AskScreen)
            note_id = app.screen._note_id if is_ask else None
            return is_ask, note_id

    is_ask, note_id = asyncio.run(_drive())

    assert is_ask
    assert note_id == "note-a"


def test_ctrl_l_from_default_screen_is_still_corpus_wide_unaffected(
    tmp_path: Path,
) -> None:
    """Acceptance: "Corpus-wide Ask behaviour is unchanged" -- the App-level
    ``ctrl+l`` (no note pinned) still reaches Ask from a screen that doesn't
    shadow it, e.g. CaptureScreen (the app's default screen).
    """
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> tuple[bool, str | None]:
        async with app.run_test() as pilot:
            assert isinstance(app.screen, CaptureScreen)
            await pilot.press("ctrl+l")
            await pilot.pause()
            is_ask = isinstance(app.screen, AskScreen)
            note_id = app.screen._note_id if is_ask else None
            return is_ask, note_id

    is_ask, note_id = asyncio.run(_drive())

    assert is_ask
    assert note_id is None


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


def test_citation_and_withheld_brackets_survive_the_actual_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The citation/withheld lines' literal ``[...]`` groups survive the RENDER
    (lode-ix4i), not just the widget's stored ``.content``.

    ``Static.content`` (asserted against above) is the original string passed
    to ``update()`` -- it is never markup-parsed, so a test against it cannot
    see this bug, the same structural blind spot ``DataTable.get_cell_at``
    has (``test_tui_tags_screen.py``'s own docstring on this, lode-7abi).
    ``Static.visual`` is what the compositor actually draws: it is computed by
    ``textual.visual.visualize(widget, content, markup=widget._render_markup)``,
    which for a plain ``str`` is ``Content.from_markup(...)`` (eats ``[...]``
    as Textual markup) when ``markup=True`` (the ``Static`` default) or a
    literal ``Content(...)`` when ``markup=False`` -- the fix this screen now
    sets. Reading ``.visual.plain`` exercises that exact real path.
    """
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
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask",
        lambda db_path, question, **kwargs: canned,
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
            results = app.screen.query_one(f"#{RESULTS_ID}", Static)
            return results.visual.plain

    rendered = asyncio.run(_drive())

    assert "[version v1, as of 2026-06-18T00:00:00.000Z]" in rendered
    assert "[withheld] v9:" in rendered


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


def test_asking_shows_an_animated_spinner_with_the_current_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-35nu.5: while in flight, the results pane shows a spinner frame
    plus the current pipeline stage -- not the old static "Thinking..."
    string -- and the frame changes over time (proves it is animated, not a
    single static substitute string).
    """
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    reached_retrieving = threading.Event()
    release_worker = threading.Event()

    def _stub_run_ask(db_path, question, *, on_stage=None, **kwargs):
        if on_stage is not None:
            on_stage(STAGE_RETRIEVING)
        reached_retrieving.set()
        release_worker.wait(timeout=5)
        return AskResult(answer=CitedAnswer(claims=(), withheld_citations=()))

    monkeypatch.setattr("lode.tui.screens.ask.run_ask", _stub_run_ask)

    async def _drive() -> tuple[str, str]:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "what did we decide about auth?"
            await pilot.press("enter")
            await asyncio.get_event_loop().run_in_executor(
                None, reached_retrieving.wait, 5
            )
            await pilot.pause()
            first = app.screen.query_one(f"#{RESULTS_ID}").content
            # Poll until the animation timer ticks rather than sleeping a
            # fixed multiple of the interval -- exits as soon as the frame
            # advances, and does not assume exactly one tick landed.
            second = first
            for _ in range(50):
                await asyncio.sleep(0.02)
                await pilot.pause()
                second = app.screen.query_one(f"#{RESULTS_ID}").content
                if second != first:
                    break
            release_worker.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return first, second

    first, second = asyncio.run(_drive())

    assert STAGE_RETRIEVING in first
    assert first != "Thinking..."
    assert STAGE_RETRIEVING in second
    # Same stage text throughout, but the spinner glyph itself must have
    # advanced -- the frame-prefix differs even though the stage text is
    # identical.
    assert first != second


def test_a_superseded_asks_late_write_never_overwrites_the_newer_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-35nu.5 / the ``/challenge`` hazard: ``AskScreen._ask`` is
    ``@work(thread=True, exclusive=True)``, but Textual cannot preempt a
    blocking call already running inside a stale worker's thread -- it can
    only cancel the *next* worker it starts. So a slow first question can
    still finish and call ``results.update`` *after* a second, faster
    question has already shown its own answer. The generation-token guard
    (``AskScreen._finish``) is what stops that late write from landing.
    """
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    release_first = threading.Event()
    calls: list[str] = []

    def _stub_run_ask(db_path, question, *, on_stage=None, **kwargs):
        calls.append(question)
        if question == "first (slow)":
            release_first.wait(timeout=5)
            return AskResult(
                answer=CitedAnswer(
                    claims=(
                        Claim(
                            text="STALE ANSWER",
                            support=[Support(version_id="v1", quoted_span="x")],
                        ),
                    ),
                    withheld_citations=(),
                )
            )
        return AskResult(
            answer=CitedAnswer(
                claims=(
                    Claim(
                        text="FRESH ANSWER",
                        support=[Support(version_id="v2", quoted_span="y")],
                    ),
                ),
                withheld_citations=(),
            )
        )

    monkeypatch.setattr("lode.tui.screens.ask.run_ask", _stub_run_ask)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")

            question_input.value = "first (slow)"
            await pilot.press("enter")
            await pilot.pause()

            question_input.value = "second (fast)"
            await pilot.press("enter")
            # Not ``app.workers.wait_for_complete()`` -- the STALE first
            # worker is still deliberately blocked on ``release_first`` at
            # this point, and waiting for *every* worker would deadlock on
            # it. Poll for the fast worker's own result instead.
            fresh = ""
            for _ in range(50):
                await pilot.pause()
                await asyncio.sleep(0.02)
                fresh = app.screen.query_one(f"#{RESULTS_ID}").content
                if "FRESH ANSWER" in fresh:
                    break

            # Now let the stale first worker finish and try to write.
            release_first.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            after_stale_completes = app.screen.query_one(f"#{RESULTS_ID}").content
            return fresh + "|" + after_stale_completes

    rendered = asyncio.run(_drive())
    fresh, after_stale = rendered.split("|", 1)

    assert "FRESH ANSWER" in fresh
    assert "FRESH ANSWER" in after_stale
    assert "STALE ANSWER" not in after_stale


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

    assert descriptions == [
        "Back",
        "Open citation",
        "Save as note",
        "Cfg",
        "Browse",
        "Tags",
        "Ask",
        "Help",
    ]
    # "Quit" is hidden from the footer entirely now (show=False, lode-2bt3.2
    # -- see docs/keybindings.md), so there is no "duplicate Quit" question
    # left to ask; the invariant this test pins is now simply its absence.
    assert descriptions.count("Quit") == 0


# ---------------------------------------------------------------------------
# Citation navigation (lode-35nu.4) -- Up/Down step the focused citation,
# Ctrl+J opens its exact cited version/snapshot, Escape returns with the
# answer intact (not re-queried).
# ---------------------------------------------------------------------------


def test_down_then_up_steps_the_citation_status_line_and_wraps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    canned = AskResult(
        answer=CitedAnswer(
            claims=(
                Claim(
                    text="claim one",
                    support=[Support(version_id="v1", quoted_span="a")],
                ),
                Claim(
                    text="claim two",
                    support=[Support(version_id="v2", quoted_span="b")],
                ),
            ),
            withheld_citations=(),
        ),
        identities={
            "v1": CitationIdentity(note_id="n1", title="Note One", is_head=True),
            "v2": CitationIdentity(note_id="n2", title="Note Two", is_head=True),
        },
    )
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask", lambda db_path, question, **kwargs: canned
    )

    async def _drive() -> tuple[str, str, str]:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "which notes?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen.query_one(f"#{CITATION_STATUS_ID}")
            first = status.content
            await pilot.press("down")
            second = status.content
            # Wraps back to the first citation.
            await pilot.press("down")
            third = status.content
            return first, second, third

    first, second, third = asyncio.run(_drive())

    assert "1/2" in first and "Note One" in first
    assert "2/2" in second and "Note Two" in second
    assert "1/2" in third and "Note One" in third


def test_down_still_scrolls_the_answer_pane_when_the_pane_has_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The citation cursor must not steal scrolling from a long answer.

    ``up``/``down`` are SCREEN-level bindings, so they only fire when the
    focused widget doesn't consume them first. ``#ask-results-pane`` is a
    focusable ``VerticalScroll`` with its own arrow bindings, so tabbing to it
    keeps scrolling intact and simply parks the citation cursor. ``ctrl+j`` is
    bound on neither widget, so it still reaches the screen from the pane.
    """
    app = LodeApp(db_path=tmp_path / "lode.db")

    canned = AskResult(
        answer=CitedAnswer(
            claims=(
                # Long enough to overflow the pane and give it something to
                # scroll.
                Claim(
                    text="claim one " * 400,
                    support=[Support(version_id="v1", quoted_span="a")],
                ),
                Claim(
                    text="claim two " * 400,
                    support=[Support(version_id="v2", quoted_span="b")],
                ),
            ),
            withheld_citations=(),
        ),
        identities={
            "v1": CitationIdentity(note_id="n1", title="Note One", is_head=True),
            "v2": CitationIdentity(note_id="n2", title="Note Two", is_head=True),
        },
    )
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask", lambda db_path, question, **kwargs: canned
    )

    async def _drive() -> tuple[int, int, str, str]:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            screen = app.screen
            screen.query_one(f"#{QUESTION_ID}").value = "which notes?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            pane = screen.query_one("#ask-results-pane", VerticalScroll)
            assert pane.max_scroll_y > 0, (
                "answer must overflow for this to mean anything"
            )
            await pilot.press("tab")
            await pilot.pause()
            assert screen.focused is pane

            before_y = pane.scroll_offset.y
            before_status = screen.query_one(f"#{CITATION_STATUS_ID}").content
            await pilot.press("down")
            await pilot.pause()
            after_status = screen.query_one(f"#{CITATION_STATUS_ID}").content
            return before_y, pane.scroll_offset.y, before_status, after_status

    before_y, after_y, before_status, after_status = asyncio.run(_drive())

    assert after_y > before_y, "down must still scroll the answer pane"
    assert after_status == before_status, "and must not move the citation cursor"


def test_ctrl_j_opens_the_exact_cited_version_not_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-35nu.4's core acceptance line: opens the VERSION actually cited."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        old = save(conn, "n1", "original body")
        save(
            conn, "n1", "edited body", parent=old.version_id
        )  # supersedes old -- new head
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    canned = AskResult(
        answer=CitedAnswer(
            claims=(
                Claim(
                    text="claim",
                    support=[
                        Support(version_id=old.version_id, quoted_span="original")
                    ],
                ),
            ),
            withheld_citations=(),
        ),
        identities={
            old.version_id: CitationIdentity(
                note_id="n1", title="original body", is_head=False
            )
        },
    )
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask", lambda db_path, question, **kwargs: canned
    )

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "what was the original?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            ask_screen = app.screen
            results_before = app.screen.query_one(f"#{RESULTS_ID}").content

            await pilot.press("ctrl+j")
            await pilot.pause()
            assert isinstance(app.screen, VersionViewScreen)
            assert app.screen.note_id == "n1"
            assert app.screen.version_id == old.version_id

            await pilot.press("escape")
            await pilot.pause()
            # Back on the same AskScreen instance, answer untouched -- not
            # re-queried.
            assert app.screen is ask_screen
            assert app.screen.query_one(f"#{RESULTS_ID}").content == results_before

    asyncio.run(_drive())


def test_ctrl_j_opens_the_snapshot_viewer_for_an_external_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type, no_egress) "
                "VALUES (?, ?, ?)",
                ("e1", "web", 0),
            )
            conn.execute(
                "INSERT INTO snapshots "
                "(snapshot_id, external_id, body, raw_payload, status, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("s1", "e1", "status: open", None, "ok", "2026-07-08T00:00:00.000000Z"),
            )
            conn.execute(
                "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
                ("s1", "e1"),
            )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    canned = AskResult(
        answer=CitedAnswer(
            claims=(
                Claim(
                    text="claim",
                    support=[Support(snapshot_id="s1", quoted_span="status: open")],
                ),
            ),
            withheld_citations=(),
        ),
        identities={
            "s1": CitationIdentity(external_id="e1", title="Ticket", is_head=True)
        },
    )
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask", lambda db_path, question, **kwargs: canned
    )
    # Wraps the real ``push_screen`` (rather than replacing it) so the actual
    # screen switch still happens -- this only records what gets pushed,
    # letting the assertion inspect the *instance* Ctrl+J built without
    # needing a real ``snapshots``/``externals`` row in the DB for
    # ``SnapshotViewerScreen.on_mount`` to read back.
    pushed: list[object] = []
    original_push_screen = app.push_screen

    def _record_push(screen: object, *args: object, **kwargs: object) -> object:
        pushed.append(screen)
        return original_push_screen(screen, *args, **kwargs)

    monkeypatch.setattr(app, "push_screen", _record_push)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            pushed.clear()  # drop the "ask" push captured above
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "is the ticket open?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("ctrl+j")
            await pilot.pause()

    asyncio.run(_drive())

    assert len(pushed) == 1
    assert isinstance(pushed[0], SnapshotViewerScreen)
    assert pushed[0].snapshot_id == "s1"


def test_ctrl_j_with_nothing_asked_yet_notifies_instead_of_crashing(
    tmp_path: Path,
) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            await pilot.press("ctrl+j")
            await pilot.pause()
            # Still on the ask screen -- no crash, nothing pushed.
            assert isinstance(app.screen, AskScreen)

    asyncio.run(_drive())


def test_a_new_question_clears_the_previous_answers_citation_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    canned = AskResult(
        answer=CitedAnswer(
            claims=(
                Claim(
                    text="claim",
                    support=[Support(version_id="v1", quoted_span="a")],
                ),
            ),
            withheld_citations=(),
        ),
        identities={"v1": CitationIdentity(note_id="n1", title="Note", is_head=True)},
    )
    # The second question's worker is held open with a threading.Event so the
    # test can observe the status line cleared synchronously on submit --
    # before that (stubbed) worker has any chance to repopulate it.
    release_second = threading.Event()

    def _stub_run_ask(db_path, question, **kwargs):
        if question == "second question":
            release_second.wait(timeout=5)
        return canned

    monkeypatch.setattr("lode.tui.screens.ask.run_ask", _stub_run_ask)

    async def _drive() -> tuple[str, str]:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "first question"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen.query_one(f"#{CITATION_STATUS_ID}").content != ""

            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "second question"
            await pilot.press("enter")
            await pilot.pause()
            # Cleared synchronously on submit -- the second worker is still
            # blocked on ``release_second`` at this point.
            cleared = app.screen.query_one(f"#{CITATION_STATUS_ID}").content

            release_second.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            repopulated = app.screen.query_one(f"#{CITATION_STATUS_ID}").content
            return cleared, repopulated

    cleared, repopulated = asyncio.run(_drive())

    assert cleared == ""
    assert repopulated != ""


# ---------------------------------------------------------------------------
# Save as note (lode-35nu.11.4) -- accepting an ask answer creates a fresh
# note through the standard capture path, linked back to the source note by
# a note->note edge; the source note itself is never touched. Reachable only
# from a per-note ask (there is no source note to link back to otherwise).
# ---------------------------------------------------------------------------


def _canned_answer() -> AskResult:
    return AskResult(
        answer=CitedAnswer(
            claims=(
                Claim(
                    text="We chose OAuth for service auth.",
                    support=[Support(version_id="v1", quoted_span="use OAuth")],
                ),
            ),
            withheld_citations=(),
        ),
        as_of={"v1": "2026-06-18T00:00:00.000Z"},
        identities={
            "v1": CitationIdentity(note_id="n1", title="Note One", is_head=True)
        },
    )


def test_ctrl_s_with_no_source_note_is_a_no_op_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corpus-wide Ask (no pinned note) has nothing to link an edge back to."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask",
        lambda db_path, question, **kwargs: _canned_answer(),
    )

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "what did we decide about auth?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("ctrl+s")
            await pilot.pause()
            # Still on AskScreen -- nothing pushed, no crash.
            assert isinstance(app.screen, AskScreen)

    asyncio.run(_drive())


def test_ctrl_s_with_no_answer_yet_is_a_no_op_notification(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen(AskScreen(note_id="note-a"))
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, AskScreen)

    asyncio.run(_drive())


def test_ctrl_s_with_an_abstained_answer_is_a_no_op_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    abstained = AskResult(answer=CitedAnswer(claims=(), withheld_citations=()))
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask", lambda db_path, question, **kwargs: abstained
    )

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen(AskScreen(note_id="note-a"))
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "anything at all?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, AskScreen)

    asyncio.run(_drive())


def test_ctrl_s_from_a_per_note_ask_pushes_the_confirm_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask",
        lambda db_path, question, **kwargs: _canned_answer(),
    )

    async def _drive() -> str:
        async with app.run_test() as pilot:
            app.push_screen(AskScreen(note_id="note-a"))
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "what did we decide about auth?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            results_text = app.screen.query_one(f"#{RESULTS_ID}").content

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, SaveAsNoteConfirmScreen)
            preview = app.screen.query_one(f"#{SAVE_AS_NOTE_PREVIEW_ID}").content
            return results_text + "|" + preview

    rendered = asyncio.run(_drive())
    results_text, preview = rendered.split("|", 1)
    # The preview is exactly what was on screen -- byte-for-byte.
    assert preview == results_text


def test_confirming_save_as_note_creates_a_new_note_linked_to_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask",
        lambda db_path, question, **kwargs: _canned_answer(),
    )

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen(AskScreen(note_id="note-a"))
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "what did we decide about auth?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, SaveAsNoteConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            # Back on AskScreen, the modal popped itself via dismiss.
            assert isinstance(app.screen, AskScreen)

    asyncio.run(_drive())

    conn = init_db(db_path)
    try:
        (source_head,) = conn.execute(
            "SELECT head_version_id FROM notes WHERE note_id = ?", ("note-a",)
        ).fetchone()
        (source_body,) = conn.execute(
            "SELECT body FROM versions WHERE version_id = ?", (source_head,)
        ).fetchone()
        (note_count,) = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        edges = conn.execute(
            "SELECT from_id, to_id, source, status FROM edges WHERE to_id = ?",
            ("note-a",),
        ).fetchall()
    finally:
        conn.close()

    # Source note untouched.
    assert source_body == "hello world"
    # Exactly one new note created (the source + the new one).
    assert note_count == 2
    # Exactly one note->note edge, back to the source.
    assert len(edges) == 1
    from_id, to_id, edge_source, status = edges[0]
    assert from_id != "note-a"
    assert to_id == "note-a"
    assert edge_source == "user"
    assert status == "fresh"


def test_declining_save_as_note_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "hello world")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    monkeypatch.setattr(
        "lode.tui.screens.ask.run_ask",
        lambda db_path, question, **kwargs: _canned_answer(),
    )

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen(AskScreen(note_id="note-a"))
            await pilot.pause()
            question_input = app.screen.query_one(f"#{QUESTION_ID}")
            question_input.value = "what did we decide about auth?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, SaveAsNoteConfirmScreen)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, AskScreen)

    asyncio.run(_drive())

    conn = init_db(db_path)
    try:
        (note_count,) = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        (edge_count,) = conn.execute("SELECT COUNT(*) FROM edges").fetchone()
    finally:
        conn.close()

    assert note_count == 1, "no new note on decline"
    assert edge_count == 0, "no edge on decline"
