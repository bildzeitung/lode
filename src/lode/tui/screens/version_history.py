"""A note's version chain, newest (the head) first (lode-0wj.7, extracted lode-s5kp.1).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed from :class:`~lode.tui.screens.edit.
EditScreen` via ``Ctrl+H`` (moved from the now-retired read-only note view,
lode-olmi.2). Each row is one version (Date | Version | Op, mirroring
:class:`~lode.tui.screens.browse.BrowseScreen`'s own column style); selecting
one pushes :class:`~lode.tui.screens.version_view.VersionViewScreen`, a
read-only view of that exact version's body
(:func:`lode.notes_read.version_body`).

Escape pops back to :class:`~lode.tui.screens.edit.EditScreen`, the same "one
level at a time" contract every other browse-family screen uses.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header

from lode.notes_read import list_versions
from lode.tui.dates import format_adaptive_date
from lode.tui.widgets.lode_data_table import LodeDataTable
from lode.tui.widgets.lode_footer import LodeFooter
from lode.tui.screens.version_view import VersionViewScreen

#: The version-history table's widget id -- read back in tests.
HISTORY_TABLE_ID = "version-history-table"


class VersionHistoryScreen(Screen[None]):
    """A note's version chain, newest (the head) first (lode-0wj.7).

    Pushed from :class:`~lode.tui.screens.edit.EditScreen` via ``Ctrl+H``
    (moved from the now-retired read-only note view, lode-olmi.2). Each row is
    one version (Date | Version | Op, mirroring
    :class:`~lode.tui.screens.browse.BrowseScreen`'s own column style);
    selecting one pushes :class:`~lode.tui.screens.version_view.
    VersionViewScreen`, a read-only view of that exact version's body --
    deliberately every row, including the current head, rather than filtering
    it out: picking the head row just shows the same body ``EditScreen``
    already has loaded, which is harmless and avoids an off-by-one special
    case for no real benefit.

    Escape pops back to :class:`~lode.tui.screens.edit.EditScreen`, the same
    "one level at a time" contract every other browse-family screen uses.
    """

    # escape/Back uses the APP-NAMESPACED "app.pop_screen" -- the bare
    # "pop_screen" silently fails on a Screen. See docs/keybindings.md.
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, note_id: str) -> None:
        super().__init__()
        self.note_id = note_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield LodeDataTable(id=HISTORY_TABLE_ID, cursor_type="row")
        yield LodeFooter()

    def on_mount(self) -> None:
        table = self.query_one(f"#{HISTORY_TABLE_ID}", LodeDataTable)
        table.add_columns("Date", "Version", "Op")
        for row in list_versions(self.app.db_path, self.note_id):
            table.add_row(
                format_adaptive_date(row.created),
                f"v{row.seq}",
                row.op,
                key=row.version_id,
            )
        table.focus()

    def on_data_table_row_selected(self, event: LodeDataTable.RowSelected) -> None:
        version_id = event.row_key.value
        if version_id is not None:
            self.app.push_screen(VersionViewScreen(self.note_id, version_id))
