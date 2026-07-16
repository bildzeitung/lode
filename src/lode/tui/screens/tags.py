"""The Tags screen: multi-select tag filter over the notes list (lode-olmi.6).

``specs/06-ux-improvements.md`` "Tag view": no TUI surface today exposes tag
data at all (tags live in the derived ``annotations`` table, never in
:func:`lode.notes_read.list_notes`). This screen splits into a top panel
(every distinct tag, multi-select) and a bottom panel (the live notes list,
narrowed to notes carrying **every** currently-selected tag -- AND/
intersection, decided with the user 2026-07-14; no selection shows every
live note, same rows :class:`~lode.tui.screens.browse.BrowseScreen` itself
lists).

Reached app-level via ``Ctrl+T`` (:mod:`lode.tui.app`), the same "reachable
from anywhere" convention ``Ctrl+O``/``Ctrl+B`` already use for
config/browse. Originally specified as the function key ``F5`` (itself a
land-time rekey off a colliding ``F4``: sibling ``lode-olmi.9`` landed a
Screen-level ``F4`` ("focus related-notes panel") on
``CaptureScreen``/``EditScreen`` first, and since ``CaptureScreen`` is the
app's own default screen, Textual's Screen-shadows-App resolution made an
App-level ``F4`` here unreachable on startup), then remapped off function
keys entirely to ``Ctrl+T`` by lode-juz8.1's no-function-key policy
(``docs/keybindings.md``).

**Read side (lode-olmi.6).** :func:`lode.notes_read.list_tags` and
:func:`lode.notes_read.list_notes_with_all_tags` are this screen's only two
reads -- both live in :mod:`lode.notes_read`, not here, matching that
module's existing "screens own no read logic of their own" convention
(:mod:`lode.tui.screens.browse`'s own docstring).

**Selecting a note opens the editor (consistent with lode-olmi.2's intent).**
Row-select on the bottom panel pushes :class:`~lode.tui.screens.browse.
EditScreen` directly -- this ticket's own acceptance criterion, independent
of whether/when lode-olmi.2 itself (retiring Browse's own read-only
``NoteViewScreen``) lands; Escape from the pushed ``EditScreen`` pops back to
this screen via Textual's ordinary screen-stack pop, same as everywhere else
in the TUI.

**Tag selection survives a round-trip to the editor.** Both panels reload on
:meth:`~textual.screen.Screen.on_screen_resume` (fires on the initial
``Ctrl+T`` push and every time this screen becomes the top screen again, mirroring
:meth:`~lode.tui.screens.browse.BrowseScreen.on_screen_resume`'s own
"stale after an edit" rationale) -- picking a note, editing it, and
returning must not silently drop the filter you had just built, so the
currently-selected tag set is kept on the screen instance
(:attr:`TagsScreen._selected`) across that reload rather than reset to
empty; the tag *list* itself is still rebuilt every resume (a tag can appear
or disappear between visits), dropping only a selected value that no longer
exists.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, SelectionList

from lode.notes_read import list_notes_with_all_tags, list_tags, short_note_id
from lode.tui.dates import format_adaptive_date
from lode.tui.screens.browse import EditScreen

#: The top panel's tag multi-select widget id -- read back in tests.
TAG_LIST_ID = "tags-tag-list"
#: The bottom panel's filtered notes table widget id -- read back in tests.
NOTES_TABLE_ID = "tags-notes-table"


class TagsScreen(Screen[None]):
    """Top panel: every tag, multi-select. Bottom: notes carrying ALL of them."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        #: Currently selected tag values. Kept on the screen instance (not
        #: re-derived from the widget) so a tag-list reload -- every
        #: ``on_screen_resume``, including the one after popping back from a
        #: pushed ``EditScreen`` -- can restore it instead of resetting the
        #: filter to empty.
        self._selected: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            SelectionList(id=TAG_LIST_ID),
            DataTable(id=NOTES_TABLE_ID, cursor_type="row"),
        )
        yield Footer()

    def on_mount(self) -> None:
        # Both panels are (re)populated in on_screen_resume, not here (see its
        # docstring) -- on_mount only needs to take focus, the same split
        # BrowseScreen.on_mount uses.
        self.query_one(f"#{TAG_LIST_ID}", SelectionList).focus()

    def on_screen_resume(self) -> None:
        """(Re)load both panels every time this screen becomes visible.

        Textual fires this on the initial push too (after ``on_mount``), so
        this is the one place that populates either panel -- mirroring
        :meth:`~lode.tui.screens.browse.BrowseScreen.on_screen_resume`'s own
        "reload every time this becomes the top screen again" contract, for
        the same reason: a note edited on the pushed ``EditScreen`` can
        change which tags apply to it, so the filtered table is stale the
        moment that happens.
        """
        self._reload_tags()
        self._reload_notes()

    def _reload_tags(self) -> None:
        """Rebuild the tag multi-select, preserving any still-valid selection."""
        tag_list = self.query_one(f"#{TAG_LIST_ID}", SelectionList)
        tags = list_tags(self.app.db_path)
        self._selected &= set(tags)  # drop selections for tags that vanished
        # Restoring a preselected option makes SelectionList post a
        # SelectedChanged per selected tag (add_options -> _make_selection ->
        # _select), each of which would drive on_selection_list_selected_changed
        # -> _reload_notes again -- N redundant table rebuilds on every resume
        # that has an active filter. on_screen_resume already calls
        # _reload_notes() explicitly right after, so suppress the programmatic
        # restore's messages (the same prevent() Textual itself uses to batch
        # bulk selection changes) and let that one explicit call be the reload.
        with tag_list.prevent(SelectionList.SelectedChanged):
            tag_list.clear_options()
            tag_list.add_options([(tag, tag, tag in self._selected) for tag in tags])

    def _reload_notes(self) -> None:
        """Rebuild the notes table against the current tag selection (AND/intersection)."""
        table = self.query_one(f"#{NOTES_TABLE_ID}", DataTable)
        table.clear(columns=True)
        table.add_columns("Id", "Date", "Version", "Summary")
        for row in list_notes_with_all_tags(self.app.db_path, self._selected):
            table.add_row(
                short_note_id(row.note_id),
                format_adaptive_date(row.created),
                f"v{row.version}",
                row.summary,
                key=row.note_id,
            )

    def on_selection_list_selected_changed(
        self, event: SelectionList.SelectedChanged
    ) -> None:
        """A tag was toggled -- re-narrow the notes panel, tag list untouched."""
        if event.selection_list.id != TAG_LIST_ID:
            return
        self._selected = set(event.selection_list.selected)
        self._reload_notes()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Selecting a note opens its editor directly (this ticket's own acceptance
        criterion; see the module docstring)."""
        note_id = event.row_key.value
        if note_id is not None:
            self.app.push_screen(EditScreen(note_id))

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()
