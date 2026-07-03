"""Tests for the TUI config/diagnostics screen (lode-3r4).

Drives the real widgets end to end via Textual's ``run_test`` pilot: reaching
the screen from the capture screen via the app-level ``F2`` binding, and
confirming the resolved paths shown are the exact values ``lode.config``'s
resolvers report (and the CLI's ``lode config`` already surfaces) — never
re-derived here (docs/configuration.md).
"""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Static

from lode.config import config_path, lance_dir, lode_home, log_dir
from lode.tui.app import LodeApp
from lode.tui.screens.config import ROWS_ID, ConfigScreen


def test_app_registers_config_screen() -> None:
    assert LodeApp.SCREENS["config"] is ConfigScreen


def test_f2_reaches_the_config_screen_with_resolved_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Acceptance: resolved $LODE_HOME, DB, vector store, and log dir are all
    # reachable in the TUI, read from lode.config rather than re-derived.
    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))
    db_path = home / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("f2")
            assert isinstance(app.screen, ConfigScreen)
            text = str(app.screen.query_one(f"#{ROWS_ID}", Static).content)
            assert str(lode_home()) in text
            assert str(db_path) in text
            assert str(lance_dir(db_path)) in text
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
