"""List a note's external edges so the user can pick one to view (lode-0sjj, extracted lode-s5kp.1).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed by
:func:`~lode.tui.screens._content_view._view_note_external_content` only when a note
has more than one external edge -- the "many" branch of the zero/one/many
addressing rule shared with ``lode dump-html`` (lode-olmi.7).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Header

from lode.enrichment_view import ExternalView
from lode.ids import short_version_id
from lode.tui.lode_footer import LodeFooter
from lode.tui.screens.snapshot_viewer import SnapshotViewerScreen

#: The many-externals picker table's widget id -- read back in tests.
EXTERNAL_PICKER_TABLE_ID = "external-picker-table"


class ExternalPickerScreen(Screen[None]):
    """List a note's external edges so the user can pick one to view (lode-0sjj).

    Pushed by :func:`~lode.tui.screens._content_view._view_note_external_content`
    only when a note has more than one external edge -- the "many" branch of
    the zero/one/many addressing rule shared with ``lode dump-html``
    (lode-olmi.7). Each row is one :class:`~lode.enrichment_view.
    ExternalView` (Source | Snapshot | Fetched | State -- the same fields
    :func:`~lode.tui.screens._browse_render._external_text` already renders
    beneath an edge line in :class:`~lode.tui.screens.enrichment_modal.
    EnrichmentModalScreen`); selecting one pushes
    :class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` for that
    row's ``snapshot_id``.

    Mirrors :class:`~lode.tui.screens.version_history.VersionHistoryScreen`'s
    DataTable-then-select shape exactly (a plain, non-modal ``Screen``, not a
    ``ModalScreen`` -- there is no "dimmed screen underneath" need here, just
    a list to pick from). Escape pops back one level, the same contract every
    other screen in this module uses.
    """

    # escape/Back uses the APP-NAMESPACED "app.pop_screen" -- the bare
    # "pop_screen" silently fails on a Screen. See docs/keybindings.md.
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, externals: list[ExternalView]) -> None:
        super().__init__()
        self._externals = externals

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id=EXTERNAL_PICKER_TABLE_ID, cursor_type="row")
        yield LodeFooter()

    def on_mount(self) -> None:
        table = self.query_one(f"#{EXTERNAL_PICKER_TABLE_ID}", DataTable)
        table.add_columns("Source", "Snapshot", "Fetched", "State")
        for external in self._externals:
            table.add_row(
                external.source_type,
                short_version_id(external.snapshot_id),
                external.fetched_at,
                external.state,
                key=external.snapshot_id,
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        snapshot_id = event.row_key.value
        if snapshot_id is not None:
            self.app.push_screen(SnapshotViewerScreen(snapshot_id))
