"""Tests for the TUI config/diagnostics screen (lode-3r4, widened lode-juz8.6).

Drives the real widgets end to end via Textual's ``run_test`` pilot: reaching
the screen from the capture screen via the app-level ``Ctrl+O`` binding, and
confirming the resolved paths + runtime/tune knob table shown are the exact
values ``lode.config``'s resolvers report (and the CLI's ``lode config``
already surfaces) — never re-derived here (docs/configuration.md). Since
lode-u5gh collapsed the CLI's and this screen's path-row lists onto the ONE
shared builder (:func:`lode.config.config_lines`), and lode-juz8.6 did the
same for the knob table (:func:`lode.config.knob_rows`), the two now render
identically — ``test_cli_and_tui_render_identical_rows`` and
``test_cli_and_tui_render_identical_knob_rows`` are the anti-drift tests that
keep it that way: they fail if either surface ever re-grows its own row list.
"""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static
from typer.testing import CliRunner

from lode.cli import app as cli_app
from lode.config import (
    Kind,
    Settings,
    config_path,
    default_db_path,
    knob_rows,
    lance_dir,
    lock_path,
    log_dir,
    model_cache_dir,
)
from lode.tui.app import LodeApp
from lode.tui.screens.config import KNOB_TABLE_ID, ROWS_ID, ConfigScreen

runner = CliRunner()


def _cli_knob_lines(stdout: str) -> list[str]:
    """Split ``lode config`` stdout at the blank line into just the knob rows.

    ``config_lines`` (paths) come first, then a blank separator, then the
    knob table's header + data rows (:func:`lode.cli._format_knob_table`) --
    mirrors the exact split the ``config`` command itself renders.
    """
    lines = stdout.splitlines()
    return lines[lines.index("") + 1 :]


def test_app_registers_config_screen() -> None:
    assert LodeApp.SCREENS["config"] is ConfigScreen


def test_ctrl_o_reaches_the_config_screen_with_resolved_paths(
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
            await pilot.press("ctrl+o")
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
            await pilot.press("ctrl+o")
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
            await pilot.press("ctrl+o")
            assert isinstance(app.screen, ConfigScreen)
            await pilot.press("escape")
            assert not isinstance(app.screen, ConfigScreen)

    asyncio.run(_drive())


def test_cli_and_tui_render_identical_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE ANTI-DRIFT TEST (lode-u5gh): the CLI's `lode config` and the TUI's
    # Ctrl+O screen must render the exact same PATH row set for the same
    # $LODE_HOME/db_path -- not "the same fields, independently maintained"
    # (that was the pre-u5gh state, and it already drifted once, lode-ak6). A
    # row added to only one surface's list can no longer happen because there
    # is only one list (lode.config.config_lines) -- this test would catch a
    # regression back to two independently-built row sets even if neither
    # list itself changed. (lode-juz8.6 widened `lode config`'s stdout with a
    # knob table below a blank line -- that section is compared separately,
    # see test_cli_and_tui_render_identical_knob_rows, so this test still
    # isolates just the paths block.)
    #
    # Both sides are fed the SAME input on purpose: one $LODE_HOME (monkeypatch,
    # which the CliRunner subprocess-less invoke inherits) and, for the TUI, the
    # very db_path the no---db CLI resolves to. Any difference in the output is
    # then a difference in the RENDERING -- i.e. real drift -- and never an
    # artifact of the two sides having been handed different paths.
    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))
    db_path = default_db_path()

    cli_result = runner.invoke(cli_app, ["config"])
    assert cli_result.exit_code == 0
    cli_lines = cli_result.stdout.splitlines()
    cli_path_lines = cli_lines[: cli_lines.index("")]

    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            text = str(app.screen.query_one(f"#{ROWS_ID}", Static).content)
            return text.splitlines()

    tui_lines = asyncio.run(_drive())
    assert tui_lines == cli_path_lines


def test_cli_and_tui_render_identical_knob_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE KNOB-TABLE ANTI-DRIFT TEST (lode-juz8.6): the CLI's knob table and
    # the TUI's DataTable must show the same (name, value, kind) rows, since
    # both are fed by the ONE shared builder (lode.config.knob_rows) rather
    # than each surface re-deriving its own knob list.
    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))

    cli_result = runner.invoke(cli_app, ["config"])
    assert cli_result.exit_code == 0
    knob_lines = _cli_knob_lines(cli_result.stdout)
    assert knob_lines[0].split() == ["Knob", "Value", "Kind"]  # header row
    cli_data_lines = knob_lines[1:]

    expected = knob_rows(Settings())
    assert len(cli_data_lines) == len(expected)
    for (name, value, kind), line in zip(expected, cli_data_lines, strict=True):
        assert line.startswith(name)
        assert line.rstrip().endswith(kind)
        assert value in line

    app = LodeApp(db_path=default_db_path())

    async def _drive() -> list[tuple[object, ...]]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            table = app.screen.query_one(f"#{KNOB_TABLE_ID}", DataTable)
            return [tuple(table.get_row_at(i)) for i in range(table.row_count)]

    tui_rows = asyncio.run(_drive())
    assert tui_rows == expected


def test_ctrl_o_knob_table_shows_runtime_and_tune_knobs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Acceptance: every runtime+tune Settings knob appears with its current
    # resolved value and kind; build-kind knobs (imply a rebuild/migration,
    # e.g. embedding_model, content_hash) are excluded from the UI surface.
    monkeypatch.setenv("LODE_HOME", str(tmp_path / "home"))
    app = LodeApp(db_path=tmp_path / "home" / "lode.db")

    async def _drive() -> DataTable:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            return app.screen.query_one(f"#{KNOB_TABLE_ID}", DataTable)

    table = asyncio.run(_drive())
    rows = [tuple(table.get_row_at(i)) for i in range(table.row_count)]
    names = {name for name, _, _ in rows}
    kinds = {kind for _, _, kind in rows}

    # A representative runtime and a representative tune knob, with their
    # documented defaults, since no config.toml is present.
    assert ("retrieval_top_k", "20", Kind.TUNE.value) in rows
    assert ("rerank_enabled", "True", Kind.RUNTIME.value) in rows
    # Build-kind knobs never reach either UI surface.
    assert "embedding_model" not in names
    assert "content_hash" not in names
    assert Kind.BUILD.value not in kinds


def test_config_knob_table_works_with_no_config_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Acceptance: works with no config.toml present (shows defaults) -- the
    # common first-run state.
    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))
    assert not (home / "config.toml").exists()

    result = runner.invoke(cli_app, ["config"])
    assert result.exit_code == 0
    assert knob_rows(Settings())  # sanity: the shared builder is non-empty
    for name, value, kind in knob_rows(Settings()):
        assert name in result.stdout
        assert kind in result.stdout
