"""``LodeDataTable`` -- closes the markup-eating bug class at the seam (lode-3dz2).

**THE BUG.** ``textual.widgets._data_table.default_cell_formatter`` parses any
bare ``str`` cell through ``Text.from_markup`` at render time -- a plain
string containing a literal ``[bracket]`` substring that happens to parse as
a Rich console style tag is silently eaten (``"gh[pousr]_..."`` renders as
``"gh_..."``). A cell that is already a ``rich.text.Text`` (or any other
Rich renderable) bypasses that branch entirely and renders verbatim.

Per-site fixes wrapping individual ``add_row``/``add_rows`` call sites in
``Text(...)`` closed this four separate times (lode-7abi, lode-ix4i x3) and a
fifth live instance (``ConfigScreen``, lode-3dz2) turned up two files away
from the most recent pass -- a focused per-site sweep does not converge,
because every *new* screen starts undefended and the failure mode is silent
(no exception, just quietly wrong text).

**THE SEAM.** ``add_row``/``add_rows``/``update_cell``/``update_cell_at`` are
the only entry points a ``DataTable`` cell's value can ever reach the widget
through. Coercing a bare ``str`` to a literal ``Text`` right here -- once --
makes every current and future call site defended automatically, with
nothing for a screen author to remember. ``add_rows`` and ``update_cell_at``
are not overridden separately: both already delegate to ``add_row``/
``update_cell`` respectively (``textual.widgets._data_table``), so overriding
those two covers all four.

A value that is already a ``Text`` (or any other already-renderable object,
e.g. a synthetic id/date/enum ``str`` some call sites still pass bare) is
left untouched -- this coercion only ever turns a plain ``str`` into a
literal ``Text``, never re-wraps or double-processes an already-correct
value.

**Sibling precedent.** ``lode.cli.SafeTable`` (lode-9tmd) already closed the
identical hazard for the CLI's ``rich.table.Table`` the same way -- coerce a
bare ``str`` cell to ``Text`` inside ``add_row`` itself, once, so no call
site can reintroduce it. ``LodeDataTable`` is that same fix for the TUI's
``DataTable`` side of the same bug class.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable
from textual.widgets.data_table import CellType, ColumnKey, RowKey


def _coerce(value: CellType) -> CellType:
    """Turn a bare ``str`` cell into a literal ``Text``; pass anything else through."""
    if isinstance(value, str):
        return Text(value)
    return value


class LodeDataTable(DataTable):
    """A ``DataTable`` that never lets a bare ``str`` reach Rich's markup parser.

    Drop-in replacement for ``textual.widgets.DataTable`` -- every screen
    under ``src/lode/tui/screens/`` constructs this instead (enforced by
    ``tests/test_tui_widget_seam_guard.py``).
    """

    def add_row(
        self,
        *cells: CellType,
        height: int | None = 1,
        key: str | None = None,
        label: object = None,
    ) -> RowKey:
        return super().add_row(
            *(_coerce(cell) for cell in cells), height=height, key=key, label=label
        )

    def update_cell(
        self,
        row_key: RowKey | str,
        column_key: ColumnKey | str,
        value: CellType,
        *,
        update_width: bool = False,
    ) -> None:
        super().update_cell(
            row_key, column_key, _coerce(value), update_width=update_width
        )
