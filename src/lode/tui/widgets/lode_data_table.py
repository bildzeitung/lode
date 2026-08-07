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

**Empty state lives here too (lode-t7pw), not as a sentinel row.**
``TagsScreen`` (lode-35nu.7) originally represented "zero results" as a real
row (``key=None``, an explanatory ``Text`` cell) so a legitimately-empty
AND/intersection filter didn't read as a silent bug. That works, but it has
real costs: the cursor lands on and highlights a row that isn't data,
``row_count`` reports 1 for a table with nothing in it (an idiom
:mod:`lode.tui.screens.browse` already relies on for its own real emptiness
check), and every future screen that wants the same "explain why this is
blank" treatment would have to re-invent the ``key=None`` guard in its own
``on_data_table_row_selected``. Setting :attr:`empty_message` instead paints
the text directly into the table's own empty canvas -- no row is added, so
``row_count`` stays ``0``, the cursor has nothing to land on, and no
row-selected handler needs a sentinel guard. Screens that want no message at
all (the common case: a table that's just plain empty, no explanation
needed) simply never set it, and get exactly ``DataTable``'s stock blank
render.
"""

from __future__ import annotations

from rich.segment import Segment
from rich.text import Text
from textual.reactive import reactive
from textual.strip import Strip
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

    #: Explanatory text painted into the table's canvas -- below the header,
    #: where the first row would otherwise go -- whenever ``row_count`` is
    #: 0. ``None`` (the default) renders nothing extra: a genuinely empty
    #: table with no explanation needed (most screens, most of the time)
    #: looks exactly like a stock ``DataTable``. Never shown while any real
    #: row exists, no matter what it's set to.
    empty_message: reactive[str | None] = reactive(None)

    def render_line(self, y: int) -> Strip:
        if self.empty_message is not None and self.row_count == 0:
            header_height = self.header_height if self.show_header else 0
            if y < header_height:
                return super().render_line(y)
            width = self.size.width
            if y == header_height:
                return self._empty_message_strip(width)
            return Strip.blank(width, self.rich_style)
        return super().render_line(y)

    def _empty_message_strip(self, width: int) -> Strip:
        """The centered ``empty_message`` line, padded/cropped to *width*."""
        message = self.empty_message
        assert message is not None  # only called when set -- see render_line
        strip = Strip([Segment(message, self.rich_style)])
        return strip.text_align(width, "center")

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
