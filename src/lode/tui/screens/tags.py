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

**Multi-column grid, native scroll-paging (lode-l38d.9).** The tag panel
used to be a ``SelectionList`` -- a built-in checkbox multi-select, but
inherently single-column, one tag per row. With tag counts scaling with
corpus size (an LLM-enriched note can carry several, and ``list_tags`` --
:func:`lode.notes_read.list_tags` -- returns every distinct tag, unbounded),
a single narrow scrolling column wasted the terminal's width. ``SelectionList``
cannot do columns, so the tag panel is now a plain :class:`~textual.widgets.
DataTable` (``cursor_type="cell"``, ``show_header=False``) rendering
``"[x] tag"``/``"[ ] tag"`` cells in as many equal-width columns as the
terminal fits, laid out row-major (fill left-to-right, then down) and rebuilt
on both :meth:`on_screen_resume` and :meth:`on_resize` --
:meth:`~lode.tui.screens.browse.BrowseScreen.on_resize` is the established
pattern this mirrors.

Multi-select is hand-rolled on top of the plain grid (``SelectionList``'s own
toggle logic goes with it): ``space`` toggles the tag under the cursor
(:meth:`action_toggle_tag`), and DataTable's own native ``enter`` ->
``CellSelected`` binding does the same
(:meth:`on_data_table_cell_selected`) -- no new binding needed for it.
Selected state lives in :attr:`_selected`, same set as before, so it already
surviving a full tag-list reload (the round-trip-to-editor case above) covers
surviving a resize/re-column the same way; the two are the same code path.

**No discrete paging -- deliberate, not missing.** This ticket's title and an
earlier decision called for discrete "page 1/4"-style paging; both were
superseded once tags reflow into columns, since ``DataTable``'s *native*
``PgUp``/``PgDn`` scrolling then satisfies "page through them" on its own.
That binding is therefore left exactly as ``DataTable`` ships it -- never
suppressed or rebound -- and no page counter exists. Full rationale in
lode-l38d.9's bd notes.
"""

from __future__ import annotations

import math

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Header

from lode.notes_read import list_notes_with_all_tags, list_tags, short_note_id
from lode.tui.dates import format_adaptive_date
from lode.tui.lode_footer import LodeFooter
from lode.tui.screens.browse import EditScreen

#: The top panel's tag grid widget id -- read back in tests.
TAG_LIST_ID = "tags-tag-list"
#: The bottom panel's filtered notes table widget id -- read back in tests.
NOTES_TABLE_ID = "tags-notes-table"

#: Width of the "[x] "/"[ ] " checkbox prefix every tag cell is rendered with.
_CHECKBOX_PREFIX_WIDTH = 4
#: Per-column horizontal padding DataTable applies -- same constant
#: :mod:`lode.tui.screens.browse` uses for its own natural-width columns.
_CELL_PADDING = 2


def _tag_grid_layout(tags: list[str], available_width: int) -> tuple[int, int]:
    """How many equal-width columns of ``tags`` fit in ``available_width``.

    Every column is sized to the *widest* tag (plus the checkbox prefix) so
    the grid stays visually aligned -- the same "natural width, uniform
    across the row" choice :meth:`~lode.tui.screens.browse.BrowseScreen.
    _reload_rows` makes for its own columns. Always returns at least one
    column, even for an empty tag list or a not-yet-laid-out (zero-width)
    table -- a 0-column grid can't render.
    """
    max_tag_len = max((len(tag) for tag in tags), default=0)
    column_width = max_tag_len + _CHECKBOX_PREFIX_WIDTH
    footprint = column_width + _CELL_PADDING
    columns = max(1, available_width // footprint)
    return columns, column_width


class TagsScreen(Screen[None]):
    """Top panel: every tag, multi-column grid. Bottom: notes carrying ALL of them."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("space", "toggle_tag", "Toggle", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        #: Currently selected tag values. Kept on the screen instance (not
        #: re-derived from the widget) so a tag-grid reload -- every
        #: ``on_screen_resume``/``on_resize``, including the one after
        #: popping back from a pushed ``EditScreen`` -- can restore it
        #: instead of resetting the filter to empty.
        self._selected: set[str] = set()
        #: The tag grid's current contents, in the same row-major order the
        #: DataTable cells were built in -- lets a (row, column) cursor
        #: coordinate be mapped back to the tag it represents without
        #: round-tripping through the widget.
        self._tags: list[str] = []
        #: The tag grid's current column count -- needed alongside
        #: :attr:`_tags` to turn a cursor coordinate into a list index
        #: (``row * self._tag_columns + col``).
        self._tag_columns: int = 1

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            DataTable(id=TAG_LIST_ID, cursor_type="cell", show_header=False),
            DataTable(id=NOTES_TABLE_ID, cursor_type="row"),
        )
        yield LodeFooter()

    def on_mount(self) -> None:
        # Both panels are (re)populated in on_screen_resume, not here (see its
        # docstring) -- on_mount only needs to take focus, the same split
        # BrowseScreen.on_mount uses.
        self.query_one(f"#{TAG_LIST_ID}", DataTable).focus()

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

    def on_resize(self, event: events.Resize) -> None:
        """Rebuild the tag grid on resize so columns re-flow to the new width.

        Mirrors :meth:`~lode.tui.screens.browse.BrowseScreen.on_resize`: same
        reload path as :meth:`on_screen_resume`, so the column-count
        recompute lives in exactly one place
        (:meth:`_reload_tags`/:func:`_tag_grid_layout`). Selection
        (:attr:`_selected`) is untouched by a reload -- surviving resize is
        this ticket's main regression risk, tested explicitly in
        ``tests/test_tui_tags_screen.py``.
        """
        self._reload_tags()

    def _tag_at(self, row: int, col: int) -> str | None:
        """The tag at grid cell ``(row, col)``, or ``None`` if there isn't one.

        The single place the grid's row-major layout is expressed: it fills
        the cells (:meth:`_reload_tags`), resolves a toggle
        (:meth:`_toggle_tag_at`), and recovers the cursor's tag across a
        rebuild. Those three must agree on the same index math and the same
        bounds check, so they share one implementation instead of each
        carrying a copy that could drift.

        ``None`` covers a blank filler cell past the last tag on a short
        final row (the grid pads the last row rather than leaving a ragged
        column count), and an empty grid, where the index is out of range of
        an empty :attr:`_tags` either way.
        """
        idx = row * self._tag_columns + col
        return self._tags[idx] if 0 <= idx < len(self._tags) else None

    def _tag_cell_text(self, tag: str) -> str:
        prefix = "[x] " if tag in self._selected else "[ ] "
        return prefix + tag

    def _reload_tags(self) -> None:
        """Rebuild the tag grid, preserving selection and (best-effort) cursor.

        Column count is recomputed from the table's *current* width every
        call (:func:`_tag_grid_layout`) -- the one thing that changes on a
        resize. :attr:`_selected` is pruned against tags that no longer
        exist but otherwise left alone, so a tag selected on what is now a
        different page/row/column of the grid still counts.

        The cursor's tag is captured *before* the rebuild below clears it --
        mirrors :meth:`~lode.tui.screens.browse.BrowseScreen._reload_rows`'s
        own "capture the key before ``clear()`` discards it" cursor
        preservation (lode-olmi.1), so a resize/reload doesn't visually snap
        the cursor back to the top-left cell when the highlighted tag is
        still present afterward.
        """
        table = self.query_one(f"#{TAG_LIST_ID}", DataTable)
        previous_tag = self._tag_at(*table.cursor_coordinate)

        tags = list_tags(self.app.db_path)
        self._selected &= set(tags)  # drop selections for tags that vanished
        columns, column_width = _tag_grid_layout(tags, table.size.width)
        self._tags = tags
        self._tag_columns = columns

        table.clear(columns=True)
        for col in range(columns):
            table.add_column("", width=column_width, key=f"col-{col}")
        # Filled through the same _tag_at mapping that reads a coordinate back,
        # against the _tags/_tag_columns just assigned above -- one row-major
        # layout rather than two that have to agree. A short final row's
        # trailing cells have no tag and render blank, keeping the column count
        # rectangular.
        for row in range(math.ceil(len(tags) / columns)):
            cells = [
                ""
                if (tag := self._tag_at(row, col)) is None
                else self._tag_cell_text(tag)
                for col in range(columns)
            ]
            table.add_row(*cells, key=f"row-{row}")

        if previous_tag is not None and previous_tag in tags:
            idx = tags.index(previous_tag)
            table.cursor_coordinate = Coordinate(idx // columns, idx % columns)

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

    def _toggle_tag_at(self, table: DataTable, row: int, col: int) -> None:
        """Flip the selected state of the tag at grid cell ``(row, col)``.

        A no-op on a cell that carries no tag (see :meth:`_tag_at`).
        """
        tag = self._tag_at(row, col)
        if tag is None:
            return
        if tag in self._selected:
            self._selected.discard(tag)
        else:
            self._selected.add(tag)
        table.update_cell(f"row-{row}", f"col-{col}", self._tag_cell_text(tag))
        self._reload_notes()

    def action_toggle_tag(self) -> None:
        """``space`` toggles the tag under the cursor (the checkbox-list
        mnemonic ``SelectionList`` used natively, kept on its replacement).

        Only acts when the tag grid itself has focus -- otherwise ``space``
        while the notes table is focused would silently toggle whatever tag
        the (unrelated, unfocused) grid's cursor happens to sit on.
        """
        table = self.query_one(f"#{TAG_LIST_ID}", DataTable)
        if self.focused is not table:
            return
        row, col = table.cursor_coordinate
        self._toggle_tag_at(table, row, col)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """``enter`` toggles the tag under the cursor -- DataTable's own
        native cell-select binding (``cursor_type="cell"``), reused instead
        of adding a redundant one."""
        if event.data_table.id != TAG_LIST_ID:
            return
        self._toggle_tag_at(
            event.data_table, event.coordinate.row, event.coordinate.column
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Selecting a note opens its editor directly (this ticket's own acceptance
        criterion; see the module docstring)."""
        if event.data_table.id != NOTES_TABLE_ID:
            return
        note_id = event.row_key.value
        if note_id is not None:
            self.app.push_screen(EditScreen(note_id))

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()
