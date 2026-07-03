"""Screen-level tests for the ask screen (lode-mkc.2).

Drives the real widgets end to end via Textual's ``run_test`` pilot -- the
screen-level twin of ``tests/test_tui_ask.py``'s direct unit coverage of
:mod:`lode.tui.ask`. The pipeline itself is monkeypatched at
``lode.tui.screens.ask.run_ask`` (it does network I/O), the same way
``tests/test_cli.py`` keeps ``lode ask`` offline -- this file only proves the
screen wires question input -> background worker -> results pane, that the
result renders with its citation/provenance/withheld/abstention content, and
that the screen is registered in ``LodeApp.SCREENS`` per the shared app-shell
pattern (lode-mkc.1).
"""

import asyncio
from pathlib import Path

import pytest

from lode.answer import Claim, Support
from lode.cited_answer import CitedAnswer
from lode.egress import WithheldCitation
from lode.tui.app import LodeApp
from lode.tui.ask import AskResult
from lode.tui.screens.ask import QUESTION_ID, RESULTS_ID, AskScreen


def test_app_registers_ask_screen(tmp_path: Path) -> None:
    app = LodeApp(db_path=tmp_path / "lode.db")
    assert app.SCREENS["ask"] is AskScreen


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


def test_escape_exits_the_app(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen("ask")
            await pilot.pause()
            await pilot.press("escape")

    asyncio.run(_drive())

    assert app.return_value is None
