"""Unit tests for the ``LodeDataTable`` seam widget (lode-3dz2).

Exercises the coercion mechanism directly, independent of any one screen --
the screen-level regression tests (``test_tui_config.py``,
``test_tui_browse_screen.py``) prove the seam is *wired in* correctly at each
site; these prove the *mechanism itself* is correct in isolation: a bare
``str`` cell is coerced to a literal ``Text`` (and therefore survives a real
``rich.Console`` render unmangled), while an already-``Text`` (or otherwise
non-``str``) value passes through untouched -- never double-wrapped, never
mistaken for something that needs coercing.

A ``DataTable`` needs a live ``App`` to construct columns against (``add_column``
measures label width via ``self.app.console``), so every test here mounts the
table inside a minimal throwaway ``App``/``Screen``, the same
``run_test()`` pilot pattern every other TUI test in this repo uses.
"""

from __future__ import annotations

import asyncio
import io

from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.screen import Screen

from lode.tui.widgets.lode_data_table import LodeDataTable

_TABLE_ID = "test-table"


class _TableHarnessScreen(Screen[None]):
    def compose(self) -> ComposeResult:
        yield LodeDataTable(id=_TABLE_ID)


class _TableHarnessApp(App[None]):
    def on_mount(self) -> None:
        self.push_screen(_TableHarnessScreen())


def _render(cell: object, width: int = 80) -> str:
    buffer = io.StringIO()
    Console(file=buffer, width=width, legacy_windows=False).print(cell)
    return buffer.getvalue().strip()


def test_add_row_coerces_a_bare_str_cell_and_renders_it_literally() -> None:
    """The exact lode-3dz2 repro: a redaction-pattern character class."""
    pattern = "gh[pousr]_[0-9A-Za-z]{36}"
    app = _TableHarnessApp()

    async def _drive() -> object:
        async with app.run_test() as pilot:
            table = pilot.app.screen.query_one(f"#{_TABLE_ID}", LodeDataTable)
            table.add_column("Value")
            table.add_row(pattern)
            return table.get_row_at(0)[0]

    cell = asyncio.run(_drive())

    assert isinstance(cell, Text)
    assert _render(cell) == pattern


def test_add_row_leaves_an_already_text_cell_untouched() -> None:
    """A caller that already wrapped its own value in ``Text`` isn't re-wrapped."""
    original = Text("[x] already safe")
    app = _TableHarnessApp()

    async def _drive() -> object:
        async with app.run_test() as pilot:
            table = pilot.app.screen.query_one(f"#{_TABLE_ID}", LodeDataTable)
            table.add_column("Value")
            table.add_row(original)
            return table.get_row_at(0)[0]

    cell = asyncio.run(_drive())

    assert cell is original  # identity preserved -- never double-processed
    assert _render(cell) == "[x] already safe"


def test_add_row_leaves_a_non_str_non_text_cell_untouched() -> None:
    """A cell that is neither ``str`` nor ``Text`` (e.g. a plain ``int``) is
    left exactly as ``DataTable`` would have handled it natively -- the
    coercion only ever touches bare ``str``."""
    app = _TableHarnessApp()

    async def _drive() -> object:
        async with app.run_test() as pilot:
            table = pilot.app.screen.query_one(f"#{_TABLE_ID}", LodeDataTable)
            table.add_column("Value")
            table.add_row(42)
            return table.get_row_at(0)[0]

    cell = asyncio.run(_drive())

    assert cell == 42
    assert not isinstance(cell, Text)


def test_add_rows_coerces_every_bare_str_cell_too() -> None:
    """``add_rows`` delegates to ``add_row`` per row (stock Textual behaviour)
    -- confirms the override is inherited through that delegation rather than
    needing its own separate override."""
    app = _TableHarnessApp()

    async def _drive() -> list[object]:
        async with app.run_test() as pilot:
            table = pilot.app.screen.query_one(f"#{_TABLE_ID}", LodeDataTable)
            table.add_column("Value")
            table.add_rows([("xox[baprs]-[0-9A-Za-z-]{10,}",), ("plain",)])
            return [table.get_row_at(i)[0] for i in range(table.row_count)]

    cells = asyncio.run(_drive())

    assert all(isinstance(c, Text) for c in cells)
    assert _render(cells[0]) == "xox[baprs]-[0-9A-Za-z-]{10,}"
    assert _render(cells[1]) == "plain"


def test_empty_message_renders_when_row_count_is_zero() -> None:
    """lode-t7pw: setting ``empty_message`` on a table with no rows paints
    it into the table's own canvas, below the header -- no row is added."""
    app = _TableHarnessApp()

    async def _drive() -> tuple[int, str]:
        async with app.run_test() as pilot:
            table = pilot.app.screen.query_one(f"#{_TABLE_ID}", LodeDataTable)
            table.add_column("Value")
            table.empty_message = "Nothing here yet."
            await pilot.pause()
            line = table.render_line(table.header_height).text
            return table.row_count, line

    row_count, line = asyncio.run(_drive())

    assert row_count == 0  # no sentinel row was added
    assert "Nothing here yet." in line


def test_empty_message_is_not_shown_once_a_real_row_exists() -> None:
    """A stale ``empty_message`` from a previous (empty) reload never bleeds
    into a table that now has real rows -- the guard is on ``row_count``,
    not on whether the attribute happens to be set."""
    app = _TableHarnessApp()

    async def _drive() -> str:
        async with app.run_test() as pilot:
            table = pilot.app.screen.query_one(f"#{_TABLE_ID}", LodeDataTable)
            table.add_column("Value")
            table.empty_message = "Nothing here yet."
            table.add_row("real data")
            await pilot.pause()
            return table.render_line(table.header_height).text

    line = asyncio.run(_drive())

    assert "Nothing here yet." not in line
    assert "real data" in line


def test_update_cell_coerces_a_bare_str_value() -> None:
    """``update_cell`` (and by delegation ``update_cell_at``) get the same
    coercion an initial ``add_row`` does -- a later in-place edit can't
    reintroduce the hazard."""
    app = _TableHarnessApp()

    async def _drive() -> object:
        async with app.run_test() as pilot:
            table = pilot.app.screen.query_one(f"#{_TABLE_ID}", LodeDataTable)
            table.add_column("Value")
            row_key = table.add_row("placeholder")
            column_key = table.ordered_columns[0].key
            table.update_cell(row_key, column_key, "AIza[0-9A-Za-z_-]{35}")
            return table.get_row_at(0)[0]

    cell = asyncio.run(_drive())

    assert isinstance(cell, Text)
    assert _render(cell) == "AIza[0-9A-Za-z_-]{35}"
