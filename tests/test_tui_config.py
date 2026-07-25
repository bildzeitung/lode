"""Tests for the TUI config/diagnostics screen (lode-3r4, widened lode-juz8.6).

Drives the real widgets end to end via Textual's ``run_test`` pilot: reaching
the screen from the capture screen via the app-level ``Ctrl+O`` binding, and
confirming the resolved paths + runtime/tune knob table shown are the exact
values ``lode.config``'s resolvers report (and the CLI's ``lode config``
already surfaces) — never re-derived here (docs/configuration.md). Since
lode-u5gh collapsed the CLI's and this screen's path-row lists onto the ONE
shared computation (:func:`lode.config.config_lines` for this screen,
:func:`lode.config.config_rows` for the CLI's rich Table — lode-l38d.4), and
lode-juz8.6 did the same for the knob table (:func:`lode.config.knob_rows`),
the two surfaces render the same DATA — ``test_cli_and_tui_render_same_path_data``
and ``test_cli_and_tui_render_same_knob_data`` are the anti-drift tests that
keep it that way: they fail if either surface ever re-grows its own row list.
(Pre-lode-l38d.4 these compared literal rendered TEXT, not just data — the CLI
side moved to a terminal-width-aware wrapping rich Table, which the TUI's
plain ``Static``/``DataTable`` widgets do not do, so byte-identical output
between the two surfaces is no longer the invariant; the shared row data
still is.)
"""

import asyncio
import io
from collections.abc import Callable
from pathlib import Path

import pytest
from rich.console import Console
from textual.widgets import DataTable, Footer, Header, Static
from typer.testing import CliRunner

from lode.cli import app as cli_app
from lode.config import (
    _SECRET_SEED_PATTERNS,
    Kind,
    Settings,
    config_path,
    config_rows,
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


def test_cli_and_tui_render_same_path_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    set_console_width: Callable[[int], None],
) -> None:
    # THE ANTI-DRIFT TEST (lode-u5gh, reshaped by lode-l38d.4): the CLI's
    # `lode config` and the TUI's Ctrl+O screen must still show the exact same
    # PATH DATA for the same $LODE_HOME/db_path -- not "the same fields,
    # independently maintained" (that was the pre-u5gh state, and it already
    # drifted once, lode-ak6). Both are fed by the ONE row computation
    # (lode.config.config_rows -- rendered directly into the CLI's rich Table,
    # and via config_lines' text shape for the TUI's Static) -- this
    # test would catch a regression back to two independently-built row sets
    # even if neither builder itself changed.
    #
    # lode-l38d.4 moved the CLI to a terminal-width-aware rich Table (to fix
    # the whitespace-overflow bug), so the two surfaces' exact rendered TEXT
    # is no longer byte-identical -- the CLI's Table can wrap a long value,
    # the TUI's Static text never does. What must still hold, and what this
    # test asserts, is that every row's DATA reaches both surfaces. Forcing
    # the CLI's shared console wide (see the set_console_width fixture) keeps its own
    # table from wrapping so this stays a plain substring check
    # (dedicated wrap-without-data-loss coverage lives in tests/test_cli.py's
    # test_config_wraps_long_knob_values_without_losing_characters).
    set_console_width(1000)
    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))
    db_path = default_db_path()

    cli_result = runner.invoke(cli_app, ["config"])
    assert cli_result.exit_code == 0
    cli_out = cli_result.stdout

    app = LodeApp(db_path=db_path)

    async def _drive() -> str:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            return str(app.screen.query_one(f"#{ROWS_ID}", Static).content)

    tui_text = asyncio.run(_drive())

    expected = config_rows(db_path)
    for label, value, note in expected:
        assert label in cli_out
        assert label in tui_text
        assert value in cli_out
        assert value in tui_text
        if note:
            assert f"({note})" in cli_out
            assert f"({note})" in tui_text

    # ROW COUNTS, not just presence: a per-row substring check above can only
    # catch a row that went MISSING from a surface -- it is structurally blind
    # to an EXTRA row that only one surface renders, which is the other half of
    # the drift this test exists to catch (lode-ak6 was an asymmetry in BOTH
    # directions). The pre-lode-l38d.4 `tui_lines == cli_path_lines` caught
    # both for free; exact text parity is gone (the CLI wraps, the TUI does
    # not), so the count is pinned explicitly instead. Both surfaces render one
    # line per row here: the CLI's path Table is box-less and forced wide
    # enough not to wrap, and console.print() emits the truly-blank line that
    # terminates the block before the knob table.
    cli_lines = cli_out.splitlines()
    cli_path_lines = cli_lines[: cli_lines.index("")]
    assert len(cli_path_lines) == len(expected)
    assert len(tui_text.splitlines()) == len(expected)


def test_cli_and_tui_render_same_knob_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    set_console_width: Callable[[int], None],
) -> None:
    # THE KNOB-TABLE ANTI-DRIFT TEST (lode-juz8.6, reshaped by lode-l38d.4):
    # the CLI's knob table and the TUI's DataTable must still show the same
    # (name, value, kind) DATA, since both are fed by the ONE shared builder
    # (lode.config.knob_rows). lode-l38d.4 moved the CLI to a rich Table
    # (header + separator rule, wrapping long values instead of inflating
    # every row to the single widest value), so literal-line parity with the
    # CLI's own stdout is no longer meaningful -- this compares DATA instead,
    # exactly like the path-table test above.
    set_console_width(1000)
    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))

    cli_result = runner.invoke(cli_app, ["config"])
    assert cli_result.exit_code == 0
    assert "Knob" in cli_result.stdout
    assert "Value" in cli_result.stdout
    assert "Kind" in cli_result.stdout

    expected = knob_rows(Settings())
    for name, value, _kind in expected:
        assert name in cli_result.stdout
        assert value in cli_result.stdout

    # ROW COUNT, not just presence -- see the path-table test above for why
    # (a substring check cannot see an EXTRA row the CLI grew on its own).
    # The knob block is everything past the blank line separating it from the
    # paths block; box.SIMPLE_HEAD contributes exactly two non-data lines (the
    # header and its separator rule) ahead of the one-line-per-knob rows, which
    # do not wrap at the width forced above.
    cli_lines = cli_result.stdout.splitlines()
    knob_block = cli_lines[cli_lines.index("") + 1 :]
    assert len(knob_block) == len(expected) + 2

    app = LodeApp(db_path=default_db_path())

    async def _drive() -> list[tuple[str, ...]]:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            table = app.screen.query_one(f"#{KNOB_TABLE_ID}", DataTable)
            # str(...) each cell: LodeDataTable (lode-3dz2) coerces every
            # bare-str cell to a rich.text.Text at add_row time so it can
            # never reach Rich's markup parser unguarded -- get_row_at
            # returns that stored Text, not the original str, so a bare
            # equality against `expected` (plain str tuples from the shared
            # knob_rows() builder) would fail on type alone.
            return [
                tuple(str(v) for v in table.get_row_at(i))
                for i in range(table.row_count)
            ]

    tui_rows = asyncio.run(_drive())
    assert tui_rows == expected


def test_config_knob_table_renders_secret_patterns_literally_not_as_rich_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VERIFIED LIVE INSTANCE #5 (lode-3dz2), now fixed at the seam.

    Two of the eight stock ``_SECRET_SEED_PATTERNS`` (``gh[pousr]_...``,
    ``xox[baprs]-...``, among others) are regexes whose ``[...]`` character
    classes parsed as Rich console markup before this ticket -- reproduced
    against a real ``rich.Console`` in the ticket description, corrupt on
    DEFAULT settings, no user input required. ``config.py``'s knob table was
    the one ``DataTable`` construction site lode-ix4i's per-site pass never
    reached (it was fixing ``ask``/``browse``/``tags``/``external_picker``).

    This asserts on the RENDER, through a real ``rich.Console`` -- the same
    "get_row_at/.content returns the stored value, not the render" distinction
    every sibling regression test in this ticket's lineage makes (lode-7abi,
    lode-ix4i). Exercised via the ``redact_before_egress_patterns`` knob (the
    ``Value`` column comma-joins all eight patterns onto one row --
    :func:`lode.config.knob_rows`), so a single row's render proves all eight
    at once.
    """
    monkeypatch.setenv("LODE_HOME", str(tmp_path / "home"))
    app = LodeApp(db_path=tmp_path / "home" / "lode.db")

    async def _drive() -> object:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            table = app.screen.query_one(f"#{KNOB_TABLE_ID}", DataTable)
            for i in range(table.row_count):
                name_cell, value_cell, _kind_cell = table.get_row_at(i)
                if str(name_cell) == "redact_before_egress_patterns":
                    return value_cell
            raise AssertionError("redact_before_egress_patterns row not found")

    cell = asyncio.run(_drive())

    buffer = io.StringIO()
    Console(file=buffer, width=1000, legacy_windows=False).print(cell)
    rendered = buffer.getvalue().strip()
    for pattern in _SECRET_SEED_PATTERNS:
        assert pattern in rendered, (
            f"{pattern!r} did not survive the render literally -- "
            f"markup-eaten in {rendered!r}"
        )


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
    # str(...) each cell -- see test_cli_and_tui_render_same_knob_data's
    # comment: LodeDataTable (lode-3dz2) stores every bare-str cell as a
    # rich.text.Text, so get_row_at returns Text, not str.
    rows = [tuple(str(v) for v in table.get_row_at(i)) for i in range(table.row_count)]
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


def test_knob_table_scrolls_within_its_own_pane_not_the_whole_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards lode-l38d.2: the knob table scrolls internally; the Screen doesn't.

    config_lines() + knob_rows(Settings()) is more content than fits a
    normal-but-short 80x24 terminal. Before the fix (no height rule reaching
    #config-knobs in lode.tcss), the table fell back to DataTable's own
    DEFAULT_CSS -- ``height: auto; max-height: 100%`` -- and that 100%
    resolves against the parent's height, not the space left after the
    parent's other children. So the table claimed the full height of the
    Vertical it shares with a 7-row Static, and its region ran several rows
    past the docked Footer -- the literal "scrolls past the bottom" of the
    ticket title, leaving the last knobs unreachable. After the fix (the
    blanket ``DataTable { height: 1fr; }`` rule -- lode-efn2 collapsed the
    former per-id #config-knobs/#browse-table/#tags-notes-table rules into
    it), the table is bounded at the Footer's row and scrolls its rows
    internally, because 1fr resolves against the space that remains.

    The table-region assertion is the discriminating one -- verified to fail
    against the pre-fix stylesheet. The Screen never scrolls (max_scroll_y
    == 0) in EITHER state, because the containing Vertical is not a scroll
    container: the overflow is unreachable, not scrolled-away. So the
    max_scroll_y/Header/Footer assertions pass pre-fix too; they are kept
    because they encode the ticket's stated acceptance criteria as
    regression guards, not because they catch this particular bug.
    """
    monkeypatch.setenv("LODE_HOME", str(tmp_path / "home"))
    app = LodeApp(db_path=tmp_path / "home" / "lode.db")

    async def _drive() -> tuple[object, ...]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+o")
            await pilot.pause()
            screen = app.screen
            table = screen.query_one(f"#{KNOB_TABLE_ID}", DataTable)
            header = screen.query_one(Header)
            footer = screen.query_one(Footer)
            return (
                screen.size,
                screen.max_scroll_y,
                header.region,
                footer.region,
                table.region,
                table.virtual_size,
            )

    (
        screen_size,
        screen_max_scroll_y,
        header_region,
        footer_region,
        table_region,
        table_virtual_size,
    ) = asyncio.run(_drive())

    # The knob table has more content than fits its allotted space -- it
    # genuinely needs to scroll internally, so this test would be vacuous
    # without it.
    assert table_virtual_size.height > table_region.height

    # The screen itself never scrolls...
    assert screen_max_scroll_y == 0
    # ...and Header/Footer -- both docked -- stay on-screen.
    assert header_region.y == 0
    assert footer_region.y + footer_region.height == screen_size.height

    # THE assertion that catches the regression: the table's own region ends
    # at or above the Footer's row, so it never extends past the visible
    # window (pre-fix, its auto-computed region ran 7 rows past the Footer).
    # Bounding it against the Footer implies bounding it against the screen,
    # since the Footer is asserted flush with the bottom just above.
    assert table_region.y + table_region.height <= footer_region.y
