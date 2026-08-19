"""The browse screen (lode-0wj.5) -- list live notes, pick one to edit.

``docs/design.md``'s post-E11 feedback: a way to see what you've captured without
leaving the terminal. Reached from :class:`~lode.tui.screens.capture.CaptureScreen` via
the app-level ``Ctrl+B`` binding (:mod:`lode.tui.app`, the same "reachable from
anywhere" convention ``Ctrl+O``'s config screen already uses). This screen owns no read
logic of its own -- it only renders the rows :func:`lode.notes_read.list_notes` returns
into a ``DataTable`` (Id | Date | Version | Summary, newest-first, live notes only) and
reacts to a row select.

Selecting a row -- Enter, or a mouse click -- pushes
:class:`~lode.tui.screens.edit.EditScreen` directly (lode-olmi.2): editing an
existing note *is* the point of opening it, so there is no separate read-only
detour first. ``EditScreen`` needs a ``note_id`` to push, so (like
``ReconcileScreen`` and ``CaptureScreen``'s own ``DiscardConfirmScreen``) it
is not itself an entry in :data:`~lode.tui.app.LodeApp.SCREENS` -- only this
screen is, per the app shell's registration convention.

Escape pops back one level at a time -- editor to list, list to capture --
which falls out of Textual's own screen stack for free: both screens' Escape
is a plain :meth:`~textual.app.App.pop_screen` (``EditScreen``'s own, dirty-
buffer-aware version of it), so "edit -> list -> capture" is just "pop
whatever is on top," never a hardcoded target.

**Retire the read-only note view (lode-olmi.2).** Before this, row-select
pushed a separate read-only ``NoteViewScreen`` and a distinct ``e`` keypress
was needed to reach the editor -- now removed along with the now-redundant
``e`` binding, since row-select opens the editor directly. Saving there
appends a new version onto that note's chain via the CAS head path
(:mod:`lode.tui.services.edit`), never a new note; the table is stale the moment that
happens (its Version/Summary columns no longer match the just-written head),
so :meth:`BrowseScreen.on_screen_resume` -- Textual's hook for "this screen is
visible again" -- reloads the table every time browse becomes the top screen
again (a fresh ``Ctrl+B``, or popping back from ``EditScreen``), not only on
first mount.

**View prior versions (lode-0wj.7, moved lode-olmi.2).** ``Ctrl+H`` on ``EditScreen``
pushes :class:`~lode.tui.screens.version_history.VersionHistoryScreen` -- a Date |
Version | Op table over the note's whole chain, newest (the head) first; selecting a row
there pushes :class:`~lode.tui.screens.version_view.VersionViewScreen`, a read-only view
of that exact version's body. See those two modules' own docstrings for the full detail
(extracted lode-s5kp.1; previously part of this module).

**Expose the note id (lode-1gr.2, moved lode-olmi.2).** Before this, nothing
in the TUI showed a note's id, so a user could see a note in Browse but not
``purge`` it. The table's leading Id column shows
:func:`lode.notes_read.short_note_id`'s 8-char prefix (the same shared
abbreviation :func:`lode.cli` will use for ``lode show``, lode-1gr.5) --
enough to feed ``lode purge <prefix>`` (lode-1gr.3) unambiguously in
practice, without widening the table for a 36-char id most rows never need in
full. ``EditScreen`` shows the *full* id instead, in its header's
``sub_title`` -- selectable/copyable there, where there is no width budget to
protect (moved from the retired ``NoteViewScreen``).

**Adaptive dates (lode-1gr.8).** This screen's Date column renders
:func:`lode.tui.dates.format_adaptive_date` instead of the raw ISO-8601
``created`` string -- today's time, this week's weekday+time, this year's
month+day, or an older plain ISO date. Shorter on every bucket but the last,
which is what frees more of :meth:`BrowseScreen._reload_rows`'s natural-width
budget for Summary. ``VersionHistoryScreen`` renders the same helper for its
own Date column.

**Enrichment inspector modal (lode-ay5.2).** ``i`` on a highlighted row pushes
:class:`~lode.tui.screens.enrichment_modal.EnrichmentModalScreen` -- a
glance-and-dismiss popup (``Esc`` pops it, same one-level-at-a-time contract
as everywhere else in this module) showing that note's enrichment. See that
module's own docstring for the full detail (extracted lode-s5kp.1). Registered
like :class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`: a bare
``ModalScreen`` pushed over the still-visible table rather than a
``SCREENS``-registry entry, dimming the table underneath for free via
``ModalScreen``'s own ``DEFAULT_CSS``.

**External-snapshot introspection (lode-8d2).** When an edge draws down a web
link, its line in the enrichment modal's Edges block gains a second, indented
line showing that external's :class:`~lode.enrichment_view.ExternalView` --
rendered by :func:`~lode.tui.screens._browse_render._external_text`. No
second DB read, no second policy -- see that leaf module's own docstring.

**Content viewer + 'v' addressing flow (lode-olmi.8's decision, lode-0sjj).** Neither
this screen nor ``EditScreen`` could show a note's actually-retrieved external content
before this -- the enrichment modal carries only
:class:`~lode.enrichment_view.ExternalView` metadata (source_type, snapshot id,
fetched_at, state), never the snapshot's stored ``body``/``raw_payload``.
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` is the modal that reads
them, keyed to one ``snapshot_id``.
:func:`~lode.tui.screens._content_view._resolve_externals` +
:func:`~lode.tui.screens._content_view._view_note_external_content` implement the shared
zero/one/many addressing rule both this screen's and ``EditScreen``'s bindings call into
-- mirroring ``lode dump-html``'s CLI disambiguation (lode-olmi.7) on purpose, so the
CLI and TUI can't drift onto two different rules for the same question: zero externals
notifies ``'no retrieved content for this note'``; exactly one pushes the viewer
directly; more than one pushes
:class:`~lode.tui.screens.external_picker.ExternalPickerScreen` first, a
DataTable-then-select list (mirroring ``VersionHistoryScreen``'s own pattern above)
showing each candidate's source_type/snapshot id/fetched_at/state -- the same fields
:func:`~lode.tui.screens._browse_render._external_text` already renders -- before the
chosen row pushes the viewer.

**Dissolved the browse<->edit import cycle (lode-2zj0).** These two
functions used to stay in this module rather than moving with the other
seven screens (lode-s5kp.1/lode-s5kp.4) because they push
``SnapshotViewerScreen``/``ExternalPickerScreen`` directly -- navigation
glue, not a pure render helper -- and this screen was one of their two
callers, the other being ``EditScreen``. That left them the one thing
``EditScreen`` had to reach back into this module for, forcing a
method-local (not top-level) import there to avoid a cycle
(``BrowseScreen`` importing ``EditScreen`` to push it on row-select, vs.
``EditScreen`` importing this module's ``_view_note_external_content``).
Both functions are leaf-eligible on their own terms -- a generic
``Screen[None]`` signature, depending only on ``enrichment_view``,
``SnapshotViewerScreen``, and ``ExternalPickerScreen``, none of which reach
back into ``browse``/``edit`` -- so they now live in their own leaf module,
:mod:`lode.tui.screens._content_view`, which both this module and
``edit.py`` import at top level. See that module's own docstring for the
full detail.

This screen's binding is bare ``v`` (``action_view_content``): the focused
widget here is the notes ``DataTable``, not an editable ``TextArea``, so a
bare printable key reaches a Screen-level binding fine -- the same reason
``i``/``d`` are already bare on this screen. ``EditScreen``'s own binding for
the identical action is **not** the same key -- see that class's docstring
and ``docs/keybindings.md`` for why an editable-body screen can't reuse a
bare letter here, and which non-printable key it uses instead.

**Delete from browse (lode-d32.1).** ``d`` on a highlighted row soft-deletes
that note via :func:`~lode.tui.services.edit.delete_note` -- the CAS-guarded
``op='delete'`` tombstone (:func:`lode.versions.delete`, routed through
:class:`~lode.repository.Repository` so the FTS/lexical cache leg is evicted
too) that :func:`lode.versions.recover` can later undo. It reuses the
*pattern* :class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`
established (a small bordered popup dialog, lode-1i8.4) rather than that exact
class -- its fixed Save/Discard/Cancel prompt doesn't fit "delete this note,"
so :class:`~lode.tui.screens.delete_confirm.DeleteConfirmScreen` is its own
small Yes/No modal (extracted lode-s5kp.1). A delete has no buffer to
preserve, so a CAS reject (:class:`~lode.versions.HeadConflictError` --
someone else changed or deleted the note first) is not routed through
:class:`~lode.tui.screens.reconcile.ReconcileScreen` the way a save conflict
is; it is simplest to notify and reload the table, which already reflects the
current state either way. Declining the confirm, or an empty table, is a
no-op.

**Expand the highlighted row's summary (lode-juz8.4).** ``x`` on a row toggles
it between the 1-line-capped summary the whole list otherwise shows
(lode-juz8.3) and its full, untruncated text -- highlighted row only, so the
rest of the list stays scannable while one row is read in full.
:meth:`BrowseScreen._reload_rows` renders that one row with
:func:`~lode.tui.screens._browse_render._wrap_summary_full` (no line cap,
``height=`` its actual wrapped line count) instead of
:func:`~lode.tui.screens._browse_render._clip_summary_to_row_height`; every
other row is unaffected. Tracked as :attr:`BrowseScreen._expanded_note_id`, a
plain ``note_id | None`` rather than a set, since only one row can be
expanded at a time. Unlike the cursor (preserved across a reload,
lode-olmi.1), expansion does **not** survive a ``_reload_rows`` triggered by
:meth:`on_screen_resume` or :meth:`on_resize` -- both reset
:attr:`_expanded_note_id` to ``None`` before reloading, so tabbing away to
edit and back, or resizing the terminal, collapses an expanded row (confirmed
acceptable by the user 2026-07-16, over a `challenge` finding that raised the
inconsistency with the cursor's own preservation). The toggle action itself
calls ``_reload_rows`` too, but *after* setting/clearing
``_expanded_note_id`` -- that reload is what renders the just-toggled state,
not a reset.

**BM25 quick search (lode-35nu.6, rebound to '/' -- lode-wdm0).** ``/`` opens
a one-line ``Input`` (:data:`QUICK_SEARCH_INPUT_ID`) at the bottom of the
screen (hidden the rest of the time via ``display = False``, so it claims no
vertical space when closed), *narrowing* the table in place: every keystroke
re-runs :func:`~lode.notes_read.search_notes` -- offline, model-free BM25
over the existing ``passages_fts`` FTS5 index (no embedder, no network,
never touches the Ask path) -- and :meth:`BrowseScreen._reload_rows` renders
whatever it returns, relevance-ordered, instead of the full live-note list.
Clearing the box (backspacing to empty) is what restores the full list --
:meth:`BrowseScreen._current_rows` branches on an empty
:attr:`_quick_search_query`, not a separate "restore" action. Closing the box
(``Escape`` or Enter) keeps the current filter and selection rather than
reverting it (:meth:`_close_quick_search`); reopening it always starts blank.
See :func:`~lode.notes_read.search_notes`'s own docstring for what scopes the
search to live *notes* only (excluding externals' own passages in the same
FTS5 table) and its one documented coverage gap (notes saved before the
lexical leg landed, or before any lexical reindex -- no such command exists
yet).

**Retired the progressive summary-scan search (lode-olmi.4, retired
lode-wdm0).** ``/`` used to open a *second*, distinct box that re-scanned the
table from row 0 on every keystroke for the first row whose Summary cell
contained the typed query as a case-insensitive substring, highlighting
within the already-loaded rows rather than changing which rows were loaded.
User decision 2026-08-10: the BM25 quick search above is strictly more
useful (it searches the whole note, not just the truncated Summary column)
and carries the same "keep the current selection" contract, so the seek
search added nothing the quick search doesn't already cover better. Retired
outright -- its action, its ``Input``, its :data:`SEARCH_INPUT_ID`, and its
``_search_open``/``_seek_match`` machinery are gone, and ``/`` now opens the
quick search directly. No next-match (``n``) semantics were introduced in
its place -- deliberately out of scope for this trade.

**Bare-blank table explained (lode-ligf).** An empty table used to give no
indication of *why* -- a fresh install with no notes yet and a quick search
that matched nothing looked identical. :meth:`_reload_rows` now sets
:attr:`~lode.tui.widgets.lode_data_table.LodeDataTable.empty_message` on
:data:`TABLE_ID` distinguishing the two: no live notes at all (fresh
install, or everything tombstoned) vs. :attr:`_quick_search_query` non-empty
with zero :func:`~lode.notes_read.search_notes` matches. Mirrors
:class:`~lode.tui.screens.tags.TagsScreen`'s own adoption of the same
mechanism (lode-t7pw) for its AND/intersection empty result.

**Search box stays on-screen with a long list (lode-juz8.2).** Before this,
the table had no height constraint from ``lode.tcss``, leaving it on
``DataTable``'s own ``DEFAULT_CSS`` of ``height: auto; max-height: 100%``.
That ``100%`` resolves against the *parent's* height -- not the space left
over after the parent's other children -- so with more notes than fit the
terminal the table claimed the Screen's entire content area, pushing this
non-docked search ``Input`` (laid out just below the table, in normal
document flow) below the visible area once opened. The always-docked
``Header``/``Footer`` stayed put, so nothing *looked* broken until '/' was
pressed. Note the ``Screen`` does **not** scroll to compensate
(``Screen.max_scroll_y`` stays ``0``); an earlier version of this docstring
mis-described the mechanism that way, and the correction is recorded with
the rule itself in ``lode.tcss`` and in ``docs/tui.md``.

``lode.tcss``'s blanket ``DataTable { height: 1fr; }`` rule (lode-efn2 --
formerly a per-id ``#browse-table`` rule) fixes it the same way
:class:`~lode.tui.screens.tags.TagsScreen` already solved the identical
problem for its own notes table: ``1fr`` resolves against the space
*remaining* after siblings, which ``max-height: 100%`` does not, so the
table (a ``ScrollView`` subclass) scrolls its own rows internally instead of
growing the layout, and the search box -- and the footer beneath it --
always land in-viewport.

**Split into one module per screen (lode-s5kp.1).** The other seven
top-level ``Screen``/``ModalScreen`` classes this module used to hold --
``VersionHistoryScreen``, ``VersionViewScreen``, ``ExternalPickerScreen``,
``SnapshotViewerScreen``, ``EnrichmentModalScreen``, ``DeleteConfirmScreen``,
``EditScreen`` -- now live in their own modules under
:mod:`lode.tui.screens`, per the one-Screen-per-module fiat
(``docs/conventions.md``). Pure move -- no behavior change. At the time this
module also kept ``_resolve_externals``/``_view_note_external_content``, the
navigation glue both this screen and ``EditScreen`` pushed through; those two
have since moved to their own leaf module (lode-2zj0 -- see the "Dissolved
the browse<->edit import cycle" section above for the cycle it broke), so
this module now keeps only :class:`BrowseScreen`, and every cross-screen
``push_screen`` reference below is a plain top-level import.
"""

from __future__ import annotations

from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Input
from textual.widgets.data_table import RowDoesNotExist

from lode.notes_read import (
    NO_EGRESS_MARKER,
    NoteRow,
    list_notes,
    search_notes,
    short_note_id,
)
from lode.tui.dates import format_adaptive_date
from lode.tui.screens._browse_render import (
    _SUMMARY_ROW_HEIGHT,
    _clip_summary_to_row_height,
    _wrap_summary_full,
)
from lode.tui.screens._content_view import _view_note_external_content
from lode.tui.screens.delete_confirm import DeleteConfirmScreen
from lode.tui.screens.edit import EditScreen
from lode.tui.screens.enrichment_modal import EnrichmentModalScreen
from lode.tui.screens.no_egress_confirm import NoEgressClearConfirmScreen
from lode.tui.services.edit import delete_note, load_head
from lode.tui.services.no_egress import note_no_egress, toggle_note_no_egress
from lode.tui.widgets.lode_data_table import LodeDataTable
from lode.tui.widgets.lode_footer import LodeFooter
from lode.versions import HeadConflictError

#: The notes table's widget id -- read back in tests.
TABLE_ID = "browse-table"
#: The BM25 quick-search one-line input's widget id (lode-35nu.6, bound to
#: '/' -- lode-wdm0) -- read back in tests.
QUICK_SEARCH_INPUT_ID = "browse-quick-search-input"

#: Left+right cell padding a ``DataTable`` adds *per column* -- used to work out
#: how much horizontal room the Summary column may claim without pushing the
#: table past the terminal width (which is what caused the horizontal scroll).
_CELL_PADDING = 2
#: Floor for the computed Summary width -- purely a crash guard so a very narrow
#: terminal can't hand ``add_column`` a zero/negative width.
_MIN_SUMMARY_WIDTH = 10


class BrowseScreen(Screen[None]):
    """Id | Date | Version | Summary, newest-first, over every live note."""

    # All of these plus the 5 shown App-level bindings (LodeApp.BINDINGS)
    # render in one footer line via the shared LodeFooter (lode-uczx).
    #
    # A hidden binding (show=False) stays fully live and one keypress
    # (Ctrl+_ or '?') away via the keybinding help overlay, which lists every
    # binding whether shown or not -- hiding one from the footer line is
    # honest rather than lossy.
    #
    # "Expand" (toggle_summary) is hidden here as the least-needed reminder
    # of the six: it is a pure display toggle (collapse/expand the Summary
    # column), reversible, non-destructive, and used far less often than
    # Inspect/View content/Delete/Search once a user has learned it once
    # -- unlike those, forgetting the key costs a glance at the overlay, not
    # a workflow it silently breaks.
    #
    # '/' is the only search left on this screen, so its label needs no
    # distinguishing from a second search box.
    #
    # Footer width is gated by tests/test_tui_footer_width_corpus.py
    # (consumed <= 100 columns, hscroll False), not a hand-tracked number.
    # "No-egress" (lode-82wt) is hidden for the same reason "Expand" is: a
    # pure, reversible flag toggle, used far less often than
    # Inspect/View content/Delete/Search once learned once -- an overlay-only
    # lookup rather than a permanent footer line item.
    BINDINGS: ClassVar = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("i", "inspect_selected", "Inspect"),
        Binding("v", "view_content", "View content"),
        Binding("d", "delete_selected", "Delete"),
        Binding("x", "toggle_summary", "Expand", show=False),
        Binding("n", "toggle_no_egress", "No-egress", show=False),
        Binding("slash", "quick_search", "Search"),
    ]

    def __init__(self) -> None:
        super().__init__()
        #: The ``note_id`` of the one row currently showing its full,
        #: untruncated summary (lode-juz8.4), or ``None`` when every row is
        #: 1-line-capped. Set/cleared by :meth:`action_toggle_summary`; reset
        #: to ``None`` by :meth:`on_screen_resume`/:meth:`on_resize` before
        #: their own reload, so expansion does not survive tabbing away or a
        #: resize (see the module docstring's lode-juz8.4 section).
        self._expanded_note_id: str | None = None
        #: The BM25 quick-search box's current text (lode-35nu.6) -- "" means
        #: no filter (the full live-note list). Read by :meth:`_current_rows`
        #: on every reload, so it persists across a resize/screen-resume the
        #: same way the table's own content would; only becomes "" again by
        #: the user clearing the box (or reopening it, which always starts
        #: blank -- see :meth:`_open_quick_search`).
        self._quick_search_query = ""
        #: Whether the quick-search box is currently open -- read by
        #: :meth:`action_dismiss_screen` to decide what Escape means right now
        #: (close the search box vs. pop back to capture).
        self._quick_search_open = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield LodeDataTable(id=TABLE_ID, cursor_type="row")
        yield Input(id=QUICK_SEARCH_INPUT_ID, placeholder="Quick search notes...")
        yield LodeFooter()

    def on_mount(self) -> None:
        # Columns are (re)built in _reload_rows, not here: the Summary column's
        # width depends on the current terminal width, which _reload_rows reads
        # back off the laid-out table. on_mount only needs to take focus.
        self.query_one(f"#{TABLE_ID}", LodeDataTable).focus()
        # Closed by default (lode-35nu.6) -- display=False claims no vertical
        # space, so the one-line input box at the bottom only appears once
        # '/' is pressed.
        self.query_one(f"#{QUICK_SEARCH_INPUT_ID}", Input).display = False

    def on_resize(self, event: events.Resize) -> None:
        """Rebuild on resize so the wrapped Summary column re-fills the new width.

        The Summary column is bounded to whatever horizontal room is left after
        the Date/Version columns (see :meth:`_reload_rows`); when the terminal
        grows or shrinks that budget changes, so rows must be re-laid-out to
        wrap at the new width. Reuses the same single load path as
        :meth:`on_screen_resume`. Resets :attr:`_expanded_note_id` first
        (lode-juz8.4): a resize-triggered reload collapses an expanded row,
        the same "does not survive this reload" contract
        :meth:`on_screen_resume` applies below.
        """
        self._expanded_note_id = None
        self._reload_rows()

    def on_screen_resume(self) -> None:
        """(Re)load the rows every time this screen becomes visible.

        Textual fires this on the *initial* push too (after ``on_mount``),
        so this is the one place that populates the table -- ``on_mount``
        never needs its own copy. That single load path is what makes
        lode-0wj.6's edit flow correct: an ``EditScreen`` pushed on top and
        popped back leaves the previously-loaded rows stale (an edit's
        Version/Summary columns no longer match the just-written head), and
        this hook reloads them every time browse becomes the top screen
        again, not only on first mount.

        Also resets :attr:`_expanded_note_id` to ``None`` (lode-juz8.4)
        before reloading -- tabbing away to edit a note and popping back
        collapses any row the user had expanded (the "does not survive this
        reload" contract; rationale in the module docstring's lode-juz8.4
        section).
        """
        self._expanded_note_id = None
        self._reload_rows()

    def _current_rows(self) -> list[NoteRow]:
        """The rows :meth:`_reload_rows` should render right now (lode-35nu.6).

        An empty :attr:`_quick_search_query` (the default, and what a cleared
        quick-search box leaves it as) is every live note, same as before this
        ticket. A non-empty query instead narrows to :func:`search_notes`'
        BM25-ranked, relevance-ordered matches -- "clearing it restores the
        full list" (acceptance criteria) falls out of this branch, not a
        separate restore path.
        """
        if self._quick_search_query:
            return search_notes(self.app.db_path, self._quick_search_query)
        return list_notes(self.app.db_path)

    def _reload_rows(self) -> None:
        """Rebuild the whole table so the Summary column wraps instead of scrolling.

        A ``DataTable`` sizes an unbounded column to its widest cell, so a long
        Summary used to push the table wider than the terminal and force an
        inconvenient horizontal scroll. Instead the Id/Date/Version columns
        keep their natural (content) widths and the Summary column is capped
        to the room left over.

        **Fixed-height cap (lode-olmi.3), tightened to one line (lode-juz8.3).**
        Rows used to be added with ``height=None`` (auto height), so a long
        summary wrapped down over as many lines as it needed and a busy list
        became hard to scan. Every row is now a fixed :data:`_SUMMARY_ROW_HEIGHT`
        tall -- one line -- and the summary text is pre-wrapped and ellipsized
        to that budget by :func:`~lode.tui.screens._browse_render._clip_summary_to_row_height` before it ever
        reaches the table -- overflow is truncated, not wrapped further.
        Summaries are prompted lede-first (lode-juz8.5) so the truncated line
        still carries the note's point.

        **One row can opt out (lode-juz8.4).** When :attr:`_expanded_note_id`
        names a row still present in *rows*, that one row is rendered with
        :func:`~lode.tui.screens._browse_render._wrap_summary_full` instead -- the full, untruncated summary,
        at its own actual wrapped ``height=`` rather than the fixed
        :data:`_SUMMARY_ROW_HEIGHT` -- while every other row keeps the
        1-line-capped rendering above.

        Rebuilt in full (``clear(columns=True)``) each time because the cap is a
        function of the current terminal width, recomputed on every
        :meth:`on_resize` and :meth:`on_screen_resume`.

        **Cursor preservation (lode-olmi.1).** ``clear(columns=True)`` also
        discards the ``DataTable``'s cursor position -- without this, leaving
        a highlighted row to view/edit a note and popping back (which fires
        :meth:`on_screen_resume`, and so this reload) always snapped the
        cursor back to the top row. This captures the highlighted row's
        ``note_id`` (the row key) *before* the rebuild and restores the
        cursor to that same key afterward, falling back to the top row only
        when the note is gone (deleted/tombstoned in the meantime, or the
        table is now empty). The same logic covers :meth:`on_resize` too,
        since it shares this one reload path -- a resize-triggered rebuild
        never loses the selection either.
        """
        table = self.query_one(f"#{TABLE_ID}", LodeDataTable)
        selected_note_id: str | None = None
        if table.row_count > 0:
            selected_note_id = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key.value
        rows = self._current_rows()
        table.clear(columns=True)
        # Two structurally distinct empty outcomes share this one table
        # (lode-ligf): no live notes at all (fresh install, or everything
        # tombstoned) vs. a non-empty quick-search query whose BM25
        # search_notes matched nothing. Each gets its own explanation rather
        # than one message trying to cover both. Set unconditionally -- the
        # widget itself only paints it when row_count == 0, so there is no
        # populated-table case to clear it for.
        table.empty_message = (
            f"No notes match '{self._quick_search_query}'."
            if self._quick_search_query
            else "No notes yet."
        )

        # Id/Date/Version keep their natural widths -- the same max(header,
        # widest cell) a DataTable auto-column would pick. Date is the short
        # adaptive form (lode-1gr.8), not the full ISO-8601 timestamp --
        # shorter, so it frees more of that natural width for Summary below;
        # Id is the shared 8-char note-id abbreviation (lode-1gr.2), not the
        # full id -- EditScreen shows that instead, where there's no width
        # budget to protect.
        # No-egress marker (lode-82wt): a withheld note's Id cell is prefixed
        # with NO_EGRESS_MARKER in brackets so it reads at a glance, without
        # adding a whole new column (which would shift every other column's
        # index for every existing caller/test). A plain note's Id cell is
        # unchanged.
        id_cells = [
            f"{f'[{NO_EGRESS_MARKER}] ' if row.no_egress else ''}"
            f"{short_note_id(row.note_id)}"
            for row in rows
        ]
        id_width = max([len("Id"), *(len(cell) for cell in id_cells)])
        date_cells = [format_adaptive_date(row.created) for row in rows]
        date_width = max([len("Date"), *(len(cell) for cell in date_cells)])
        version_cells = [f"v{row.version}" for row in rows]
        version_width = max([len("Version"), *(len(cell) for cell in version_cells)])
        remaining = (
            table.size.width - id_width - date_width - version_width - _CELL_PADDING * 4
        )
        summary_width = max(_MIN_SUMMARY_WIDTH, remaining)

        table.add_column("Id", width=id_width)
        table.add_column("Date", width=date_width)
        table.add_column("Version", width=version_width)
        table.add_column("Summary", width=summary_width)
        for row, id_cell, date_cell, version_cell in zip(
            rows, id_cells, date_cells, version_cells
        ):
            if row.note_id == self._expanded_note_id:
                summary_cell, row_height = _wrap_summary_full(
                    row.summary, summary_width
                )
            else:
                summary_cell = _clip_summary_to_row_height(row.summary, summary_width)
                row_height = _SUMMARY_ROW_HEIGHT
            table.add_row(
                id_cell,
                date_cell,
                version_cell,
                summary_cell,
                key=row.note_id,
                height=row_height,
            )

        if table.row_count == 0:
            return
        restored_index = 0
        if selected_note_id is not None:
            try:
                restored_index = table.get_row_index(selected_note_id)
            except RowDoesNotExist:
                # The previously-highlighted note is gone (deleted/tombstoned)
                # -- fall back to the top row (restored_index is already 0).
                pass
        table.move_cursor(row=restored_index)

    def on_data_table_row_selected(self, event: LodeDataTable.RowSelected) -> None:
        """Enter/select on a row opens that note's editor directly (lode-olmi.2)."""
        note_id = event.row_key.value
        if note_id is not None:
            self.app.push_screen(EditScreen(note_id))

    def action_inspect_selected(self) -> None:
        """``i``: open the highlighted row's enrichment inspector modal."""
        table = self.query_one(f"#{TABLE_ID}", LodeDataTable)
        if table.row_count == 0:
            return
        note_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if note_id is not None:
            self.app.push_screen(EnrichmentModalScreen(note_id))

    def action_view_content(self) -> None:
        """``v``: view the highlighted row's retrieved external content, if any (lode-0sjj)."""
        table = self.query_one(f"#{TABLE_ID}", LodeDataTable)
        if table.row_count == 0:
            return
        note_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if note_id is not None:
            _view_note_external_content(self, note_id)

    def action_toggle_summary(self) -> None:
        """``x``: toggle the highlighted row between its 1-line and full summary (lode-juz8.4).

        Highlighted row only -- at most one row is ever expanded. Pressing
        ``x`` again on the already-expanded row collapses it back to the
        1-line cap (no row expanded); pressing ``x`` on a *different* row
        moves the expansion there -- the previously-expanded row collapses
        and the newly-highlighted one expands. Delegates the actual rendering
        to :meth:`_reload_rows`, which reads :attr:`_expanded_note_id` on
        every rebuild -- setting it here and reloading is the whole toggle.
        """
        table = self.query_one(f"#{TABLE_ID}", LodeDataTable)
        if table.row_count == 0:
            return
        note_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if note_id is None:
            return
        if self._expanded_note_id == note_id:
            self._expanded_note_id = None
        else:
            self._expanded_note_id = note_id
        self._reload_rows()

    def action_toggle_no_egress(self) -> None:
        """``n``: flip the highlighted row's no_egress flag (lode-82wt, lode-a50f).

        Withholds (or reinstates) this note from cloud egress -- the note-side
        mirror of ``lode no-egress --note``. Goes through
        :func:`~lode.tui.services.no_egress.toggle_note_no_egress`, the single
        write path onto ``notes.no_egress`` this screen uses; no second code
        path reaches the column.

        SETTING it (the note is currently NOT withheld) is the safe
        direction and applies immediately, same as before lode-a50f.
        CLEARING it is the dangerous direction -- it makes an explicitly
        withheld note cloud-eligible again -- so it first confirms via
        :class:`~lode.tui.screens.no_egress_confirm.NoEgressClearConfirmScreen`,
        mirroring ``d``'s :class:`~lode.tui.screens.delete_confirm.DeleteConfirmScreen`.
        Either way, a :meth:`notify` gives feedback beyond the marker itself.
        """
        table = self.query_one(f"#{TABLE_ID}", LodeDataTable)
        if table.row_count == 0:
            return
        note_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if note_id is None:
            return
        if note_no_egress(self.app.db_path, note_id):
            self.app.push_screen(
                NoEgressClearConfirmScreen(),
                lambda confirmed: self._on_no_egress_clear_confirm(confirmed, note_id),
            )
            return
        self._apply_no_egress_toggle(note_id)

    def _on_no_egress_clear_confirm(self, confirmed: bool | None, note_id: str) -> None:
        """Act on the clear-confirm dialog's answer: clear-then-reload, or leave untouched."""
        if not confirmed:
            return
        self._apply_no_egress_toggle(note_id)

    def _apply_no_egress_toggle(self, note_id: str) -> None:
        """Flip the flag, report the RESULTING state, and re-render.

        Both arms of ``n`` land here. The notify text comes from
        :func:`~lode.tui.services.no_egress.toggle_note_no_egress`'s return
        value -- the state actually written -- rather than from the direction
        the caller expected, so a note flipped elsewhere between the confirm
        popping up and this running is still described accurately.
        """
        if toggle_note_no_egress(self.app.db_path, note_id):
            self.notify(
                "Marked no-egress: this note is now withheld from cloud egress."
            )
        else:
            self.notify("Cleared no-egress: this note is cloud-eligible again.")
        self._reload_rows()

    def action_delete_selected(self) -> None:
        """``d``: soft-delete the highlighted row's note, after confirming (lode-d32.1)."""
        table = self.query_one(f"#{TABLE_ID}", LodeDataTable)
        if table.row_count == 0:
            return
        note_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if note_id is None:
            return
        self.app.push_screen(
            DeleteConfirmScreen(),
            lambda confirmed: self._on_delete_confirm(confirmed, note_id),
        )

    def _on_delete_confirm(self, confirmed: bool | None, note_id: str) -> None:
        """Act on the Yes/No dialog's answer: delete-then-reload, or leave untouched.

        A CAS reject (:class:`~lode.versions.HeadConflictError` -- the note
        changed or was already deleted between the confirm popping up and this
        running) has no buffer to preserve, so unlike a save conflict this
        doesn't route to :class:`~lode.tui.screens.reconcile.ReconcileScreen`
        -- it is simplest to notify and reload, since the table then reflects
        the current state either way.
        """
        if not confirmed:
            return
        head = load_head(self.app.db_path, note_id)
        if head is None:
            # Already gone by the time the confirm closed (e.g. deleted from
            # another session) -- nothing to CAS against; just resync.
            self._reload_rows()
            return
        head_version_id, _ = head
        try:
            delete_note(self.app.db_path, note_id, parent=head_version_id)
        except HeadConflictError:
            self.notify(
                "This note changed before the delete went through -- reloading.",
                severity="warning",
            )
        self._reload_rows()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Every keystroke re-narrows the table (lode-35nu.6)."""
        if event.input.id == QUICK_SEARCH_INPUT_ID:
            self._quick_search_query = event.value
            self._reload_rows()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter: confirm and close the box, keeping its result."""
        if event.input.id == QUICK_SEARCH_INPUT_ID:
            self._close_quick_search()

    def action_quick_search(self) -> None:
        """``/``: open the BM25 quick-search box (lode-35nu.6).

        Offline, model-free -- no summarization, no network; narrows the
        visible list in place via the existing FTS5 ``passages_fts`` index.
        Available from Browse only.
        """
        self._quick_search_open = True
        quick_search_input = self.query_one(f"#{QUICK_SEARCH_INPUT_ID}", Input)
        # Always starts blank -- setting .value fires
        # Input.Changed, which clears any previous filter via
        # on_input_changed -> _current_rows, so reopening the box always
        # starts from the full list again rather than resuming a stale one.
        quick_search_input.value = ""
        quick_search_input.display = True
        quick_search_input.focus()

    def _close_quick_search(self) -> None:
        """Hide the box; the narrowed table (if any) stays as-is (lode-35nu.6).

        Closing the box keeps the current filter and selection -- not a
        revert-on-cancel. Clearing the query text (backspacing to empty), not
        closing the box, is what restores the full list (acceptance
        criteria); :meth:`_current_rows` already handles that branch on every
        keystroke.
        """
        quick_search_input = self.query_one(f"#{QUICK_SEARCH_INPUT_ID}", Input)
        quick_search_input.display = False
        self._quick_search_open = False
        self.query_one(f"#{TABLE_ID}", LodeDataTable).focus()

    def action_dismiss_screen(self) -> None:
        """Escape: close the search box if open, else pop back to capture.

        The same key means two different things depending on whether the
        quick-search box is open (lode-35nu.6) -- closing it keeps its
        current result rather than popping the whole screen out from under
        it.
        """
        if self._quick_search_open:
            self._close_quick_search()
            return
        self.app.pop_screen()
