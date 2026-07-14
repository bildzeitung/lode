"""Tests for the TUI config/diagnostics screen (lode-3r4).

Drives the real widgets end to end via Textual's ``run_test`` pilot: reaching
the screen from the capture screen via the app-level ``F2`` binding, and
confirming the resolved paths shown are the exact values ``lode.config``'s
resolvers report (and the CLI's ``lode config`` already surfaces) — never
re-derived here (docs/configuration.md). Since lode-u5gh collapsed the CLI's
and this screen's row lists onto the ONE shared builder
(:func:`lode.config.config_lines`), the two now render identically —
``test_cli_and_tui_render_identical_rows`` is the anti-drift test that keeps
it that way: it fails if either surface ever re-grows its own row list.
"""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Static
from typer.testing import CliRunner

from lode.cli import app as cli_app
from lode.config import config_path, lance_dir, lock_path, log_dir, model_cache_dir
from lode.tui.app import LodeApp
from lode.tui.screens.config import ROWS_ID, ConfigScreen

runner = CliRunner()


def test_app_registers_config_screen() -> None:
    assert LodeApp.SCREENS["config"] is ConfigScreen


def test_f2_reaches_the_config_screen_with_resolved_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Acceptance: resolved $LODE_HOME, DB, db lock, vector store, model cache,
    # and log dir are all reachable in the TUI, read from lode.config rather
    # than re-derived. The "db lock" row and the "($LODE_HOME)" source
    # annotation are lode-u5gh's user-visible gain -- the screen now renders
    # via the same shared builder the CLI does (lode.config.config_lines), so
    # it can no longer be narrower (lode-ak6 added the model cache row to the
    # CLI by hand and this screen missed it, which is the drift this ticket
    # closes).
    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))
    db_path = home / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("f2")
            assert isinstance(app.screen, ConfigScreen)
            text = str(app.screen.query_one(f"#{ROWS_ID}", Static).content)
            assert str(home) in text
            assert "($LODE_HOME)" in text
            assert str(db_path) in text
            assert str(lock_path(db_path)) in text
            assert str(lance_dir(db_path)) in text
            assert str(model_cache_dir()) in text
            assert str(log_dir()) in text
            assert str(config_path()) in text
            assert "(absent)" in text

    asyncio.run(_drive())


def test_config_file_present_is_reflected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("LODE_HOME", str(home))
    app = LodeApp(db_path=home / "lode.db")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("f2")
            text = str(app.screen.query_one(f"#{ROWS_ID}", Static).content)
            assert "(present)" in text

    asyncio.run(_drive())


def test_escape_returns_to_the_previous_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path / "home"))
    app = LodeApp(db_path=tmp_path / "home" / "lode.db")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("f2")
            assert isinstance(app.screen, ConfigScreen)
            await pilot.press("escape")
            assert not isinstance(app.screen, ConfigScreen)

    asyncio.run(_drive())


def test_cli_and_tui_render_identical_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE ANTI-DRIFT TEST (lode-u5gh): the CLI's `lode config` and the TUI's F2
    # screen must render the exact same row set for the same $LODE_HOME/db_path
    # -- not "the same fields, independently maintained" (that was the pre-u5gh
    # state, and it already drifted once, lode-ak6). A row added to only one
    # surface's list can no longer happen because there is only one list
    # (lode.config.config_lines) -- this test would catch a regression back to
    # two independently-built row sets even if neither list itself changed.
    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))
    db_path = home / "lode.db"

    cli_result = runner.invoke(cli_app, ["config"], env={"LODE_HOME": str(home)})
    assert cli_result.exit_code == 0
    cli_lines = cli_result.stdout.splitlines()

    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("f2")
            text = str(app.screen.query_one(f"#{ROWS_ID}", Static).content)
            return text.splitlines()

    tui_lines = asyncio.run(_drive())
    assert tui_lines == cli_lines
