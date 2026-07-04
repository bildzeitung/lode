"""The browse screen (lode-0wj.5) -- list live notes, pick one to view.

``docs/design.md``'s post-E11 feedback: a way to see what you've captured
without leaving the terminal. Reached from :class:`~lode.tui.screens.capture.
CaptureScreen` via the app-level ``F3`` binding (:mod:`lode.tui.app`, the same
"reachable from anywhere" convention ``F2``'s config screen already uses).
This screen owns no read logic of its own -- it only renders the rows
:func:`lode.tui.browse.list_notes` returns into a ``DataTable`` (Date |
Version | Summary, newest-first, live notes only) and reacts to a row select.

Selecting a row pushes :class:`NoteViewScreen`, a read-only view of that
note's live head body (:func:`lode.tui.browse.note_body`) -- mirroring how
:class:`~lode.tui.screens.reconcile.ReconcileScreen` shows a read-only
``TextArea`` for its diff. ``NoteViewScreen`` needs a ``note_id`` to push, so
(like ``ReconcileScreen`` and ``CaptureScreen``'s own ``DiscardConfirmScreen``)
it is not itself an entry in :data:`~lode.tui.app.LodeApp.SCREENS` -- only
this screen is, per the app shell's registration convention.

Escape pops back one level at a time -- note view to list, list to capture --
which falls out of Textual's own screen stack for free: both screens' Escape
is a plain :meth:`~textual.app.App.pop_screen`, so "note -> list -> capture"
is just "pop whatever is on top," never a hardcoded target.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, TextArea

from lode.tui.browse import list_notes, note_body

#: The notes table's widget id -- read back in tests.
TABLE_ID = "browse-table"
#: The read-only note body's widget id -- read back in tests.
NOTE_BODY_ID = "note-view-body"


class NoteViewScreen(Screen[None]):
    """A read-only view of one note's live head body."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def __init__(self, note_id: str) -> None:
        super().__init__()
        self.note_id = note_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield TextArea("", read_only=True, id=NOTE_BODY_ID)
        yield Footer()

    def on_mount(self) -> None:
        body = note_body(self.app.db_path, self.note_id)
        self.query_one(f"#{NOTE_BODY_ID}", TextArea).text = body or ""

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class BrowseScreen(Screen[None]):
    """Date | Version | Summary, newest-first, over every live note."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id=TABLE_ID, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        table.add_columns("Date", "Version", "Summary")
        for row in list_notes(self.app.db_path):
            table.add_row(row.created, f"v{row.version}", row.summary, key=row.note_id)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        note_id = event.row_key.value
        if note_id is not None:
            self.app.push_screen(NoteViewScreen(note_id))

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()
