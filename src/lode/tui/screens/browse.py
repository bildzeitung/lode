"""The browse screen (lode-0wj.5) -- list live notes, pick one to edit.

``docs/design.md``'s post-E11 feedback: a way to see what you've captured
without leaving the terminal. Reached from :class:`~lode.tui.screens.capture.
CaptureScreen` via the app-level ``Ctrl+B`` binding (:mod:`lode.tui.app`, the
same "reachable from anywhere" convention ``Ctrl+O``'s config screen already
uses). This screen owns no read logic of its own -- it only renders the rows
:func:`lode.notes_read.list_notes` returns into a ``DataTable`` (Id | Date |
Version | Summary, newest-first, live notes only) and reacts to a row select.

Selecting a row -- Enter, or a mouse click -- pushes :class:`EditScreen`
directly (lode-olmi.2): editing an existing note *is* the point of opening
it, so there is no separate read-only detour first. ``EditScreen`` needs a
``note_id`` to push, so (like ``ReconcileScreen`` and ``CaptureScreen``'s own
``DiscardConfirmScreen``) it is not itself an entry in
:data:`~lode.tui.app.LodeApp.SCREENS` -- only this screen is, per the app
shell's registration convention.

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
(:mod:`lode.tui.edit`), never a new note; the table is stale the moment that
happens (its Version/Summary columns no longer match the just-written head),
so :meth:`BrowseScreen.on_screen_resume` -- Textual's hook for "this screen is
visible again" -- reloads the table every time browse becomes the top screen
again (a fresh ``Ctrl+B``, or popping back from ``EditScreen``), not only on
first mount.

**View prior versions (lode-0wj.7, moved lode-olmi.2).** ``Ctrl+H`` on
:class:`EditScreen` pushes :class:`VersionHistoryScreen` -- a Date | Version |
Op table over the note's whole chain (:func:`lode.notes_read.list_versions`),
newest (the head) first. This used to be a bare ``h`` on the now-retired
``NoteViewScreen``; it moved into the editor as ``Ctrl+H`` (not bare ``h``)
because ``EditScreen``'s body is an editable ``TextArea``, which consumes
every printable keypress -- including bare ``h`` -- before a Screen-level
binding ever sees it (the same reason this screen's save binding is
``Ctrl+S``, not a bare letter). Selecting a row in the history table pushes
:class:`VersionViewScreen`, a read-only view of that exact version's body
(:func:`lode.notes_read.version_body`) -- a read-only ``TextArea``, keyed to
a specific ``version_id`` instead of always the live head. Escape pops one
level at a time here too: version body -> history list -> editor, falling out
of the same Textual screen-stack pop every other Escape in this module
already relies on.

**Expose the note id (lode-1gr.2, moved lode-olmi.2).** Before this, nothing
in the TUI showed a note's id, so a user could see a note in Browse but not
``purge`` it. The table's leading Id column shows
:func:`lode.notes_read.short_note_id`'s 8-char prefix (the same shared
abbreviation :func:`lode.cli` will use for ``lode show``, lode-1gr.5) --
enough to feed ``lode purge <prefix>`` (lode-1gr.3) unambiguously in
practice, without widening the table for a 36-char id most rows never need in
full. :class:`EditScreen` shows the *full* id instead, in its header's
``sub_title`` -- selectable/copyable there, where there is no width budget to
protect (moved from the retired ``NoteViewScreen``).

**Adaptive dates (lode-1gr.8).** Both this screen's Date column and
:class:`VersionHistoryScreen`'s render :func:`lode.tui.dates.
format_adaptive_date` instead of the raw ISO-8601 ``created`` string --
today's time, this week's weekday+time, this year's month+day, or an older
plain ISO date. Shorter on every bucket but the last, which is what frees more
of :meth:`BrowseScreen._reload_rows`'s natural-width budget for Summary.

**Enrichment inspector modal (lode-ay5.2).** ``i`` on a highlighted row pushes
:class:`EnrichmentModalScreen` -- a glance-and-dismiss popup (``Esc`` pops it,
same one-level-at-a-time contract as everywhere else in this module) showing
that note's enrichment: summary, tags, entities, inferred edges
(reason+confidence+stale), embed status, and the three-valued
``enrichment_state``. It renders :func:`lode.enrichment_view.enrichment_view`
directly -- the shared TUI+CLI view-model seam (lode-ay5.1, lode-0qc) -- and
holds **no** copy of the stale-display policy or any independent display
assembly; :mod:`lode.cli`'s ``show_`` (lode-ay5.3) consumes the same seam so
the two surfaces cannot drift. Registered like
:class:`~lode.tui.screens.capture.DiscardConfirmScreen`: a bare
``ModalScreen`` pushed over the still-visible list rather than a
``SCREENS``-registry entry, dimming the table underneath for free via
``ModalScreen``'s own ``DEFAULT_CSS``.

**External-snapshot introspection (lode-8d2).** When an edge draws down a web
link, its line in the Edges block gains a second, indented line showing that
external's :class:`~lode.enrichment_view.ExternalView` -- source_type,
snapshot id, ``fetched_at``, and its three-valued ``state``
(``un-refreshed``/``stale``/``withheld``, dimmed like a stale edge already
is). Renders :attr:`~lode.enrichment_view.EnrichmentEdge.external` verbatim,
already assembled by the same :func:`~lode.enrichment_view.enrichment_view`
call -- no second DB read, no second policy. The edge's own target label
switches from the truncated :func:`~lode.notes_read.short_note_id` prefix to
the bare ``to_id`` when it resolves to an external, since that ``to_id`` is
the full source URL, not a note id worth abbreviating.

**Content viewer + 'v' addressing flow (lode-olmi.8's decision, lode-0sjj).**
Neither this screen nor :class:`EditScreen` could show a note's actually-
retrieved external content before this -- :class:`EnrichmentModalScreen`
(above) carries only :class:`~lode.enrichment_view.ExternalView` metadata
(source_type, snapshot id, fetched_at, state), never the snapshot's stored
``body``/``raw_payload``. :class:`SnapshotViewerScreen` is the new modal that
reads them, keyed to one ``snapshot_id``: it shows the extracted ``body`` by
default in a read-only ``TextArea`` and ``t`` toggles to ``raw_payload``
(nullable -- a clean notify-and-stay when it isn't captured, never a blank
toggle). ``_resolve_externals`` + ``_view_note_external_content`` implement
the shared zero/one/many addressing rule both screens' bindings call into --
mirroring ``lode dump-html``'s CLI disambiguation (lode-olmi.7) on purpose,
so the CLI and TUI can't drift onto two different rules for the same
question: zero externals notifies ``'no retrieved content for this note'``;
exactly one pushes the viewer directly; more than one pushes
:class:`ExternalPickerScreen` first, a DataTable-then-select list (mirroring
:class:`VersionHistoryScreen`'s own pattern above) showing each candidate's
source_type/snapshot id/fetched_at/state -- the same fields
:func:`_external_text` already renders -- before the chosen row pushes the
viewer.

This screen's binding is bare ``v`` (``action_view_content``): the focused
widget here is the notes ``DataTable``, not an editable ``TextArea``, so a
bare printable key reaches a Screen-level binding fine -- the same reason
``i``/``d`` are already bare on this screen. :class:`EditScreen`'s own
binding for the identical action is **not** the same key -- see that class's
docstring and ``docs/keybindings.md`` for why an editable-body screen can't
reuse a bare letter here, and which non-printable key it uses instead.

**Delete from browse (lode-d32.1).** ``d`` on a highlighted row soft-deletes
that note via :func:`~lode.tui.edit.delete_note` -- the CAS-guarded
``op='delete'`` tombstone (:func:`lode.versions.delete`, routed through
:class:`~lode.repository.Repository` so the FTS/lexical cache leg is evicted
too) that :func:`lode.versions.recover` can later undo. It reuses the
*pattern* :class:`~lode.tui.screens.capture.DiscardConfirmScreen` established
(a small bordered popup dialog, lode-1i8.4) rather than that exact class --
its fixed Save/Discard/Cancel prompt doesn't fit "delete this note," so
:class:`DeleteConfirmScreen` is its own small Yes/No modal. A delete has no
buffer to preserve, so a CAS reject (:class:`~lode.versions.
HeadConflictError` -- someone else changed or deleted the note first) is not
routed through :class:`~lode.tui.screens.reconcile.ReconcileScreen` the way a
save conflict is; it is simplest to notify and reload the table, which
already reflects the current state either way. Declining the confirm, or an
empty table, is a no-op.

**Progressive incremental search (lode-olmi.4).** ``/`` opens a one-line
:class:`~textual.widgets.Input` at the bottom of the screen (hidden the rest
of the time via ``display = False``, so it claims no vertical space when
closed); each keystroke re-scans the table **from the cursor's current row**
for the next row whose Summary cell contains the typed query as a
case-insensitive substring -- the match target settled with the user
2026-07-14, deliberately the visible Summary text rather than the full note
body, since that's the same text the row already shows. ``?`` opens the same
box but scans upward instead of downward. Scanning from the *current* row
(not a remembered start-of-search anchor) on every keystroke, rather than
restarting from wherever the box was opened, is what makes "wrapping if
needed" alone sufficient to still reach an earlier match after the query has
grown past it: the wrap covers the whole table either way, so nothing further
back is ever unreachable. Escape closes the box and leaves the cursor at
whatever row the search last landed on (the same "keep the current
selection" contract, not a revert-on-cancel); Enter does the same, just
spelled as a confirm rather than a dismiss. An empty query is a no-op --
:meth:`BrowseScreen._seek_match` returns immediately rather than searching for
the empty string. Escape means two different things depending on whether the
box is open (:meth:`BrowseScreen.action_dismiss_screen` checks
``self._search_open`` first) -- close the search, or (box already closed) the
usual pop back to capture.

**Expand the highlighted row's summary (lode-juz8.4).** ``x`` on a row toggles
it between the 1-line-capped summary the whole list otherwise shows
(lode-juz8.3) and its full, untruncated text -- highlighted row only, so the
rest of the list stays scannable while one row is read in full.
:meth:`BrowseScreen._reload_rows` renders that one row with
:func:`_wrap_summary_full` (no line cap, ``height=`` its actual wrapped line
count) instead of :func:`_clip_summary_to_row_height`; every other row is
unaffected. Tracked as :attr:`BrowseScreen._expanded_note_id`, a plain
``note_id | None`` rather than a set, since only one row can be expanded at a
time. Unlike the cursor (preserved across a reload, lode-olmi.1), expansion
does **not** survive a ``_reload_rows`` triggered by :meth:`on_screen_resume`
or :meth:`on_resize` -- both reset :attr:`_expanded_note_id` to ``None``
before reloading, so tabbing away to edit and back, or resizing the
terminal, collapses an expanded row (confirmed acceptable by the user
2026-07-16, over a `challenge` finding that raised the inconsistency with
the cursor's own preservation). The toggle action itself calls
``_reload_rows`` too, but *after* setting/clearing ``_expanded_note_id`` --
that reload is what renders the just-toggled state, not a reset.

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
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Static, TextArea
from textual.widgets.data_table import RowDoesNotExist

from lode.enrichment_view import (
    EnrichmentEdge,
    EnrichmentItem,
    ExternalView,
    enrichment_view,
)
from lode.ids import short_version_id
from lode.notes_read import (
    SnapshotRow,
    list_notes,
    list_versions,
    read_snapshot,
    short_note_id,
    version_body,
)
from lode.tui.dates import format_adaptive_date
from lode.tui.edit import (
    EditConflict,
    EmptyEditError,
    delete_note,
    load_head,
    save_edit,
)
from lode.tui.related_notes_panel import RelatedNotesPanel
from lode.tui.screens.capture import DiscardConfirmScreen
from lode.tui.screens.reconcile import ReconcileScreen
from lode.versions import HeadConflictError, SaveResult

#: The notes table's widget id -- read back in tests.
TABLE_ID = "browse-table"
#: The editable note body's widget id -- read back in tests.
EDIT_BODY_ID = "note-edit-body"
#: The delete-confirm dialog's message widget id -- read back in tests.
DELETE_CONFIRM_MESSAGE_ID = "delete-confirm-message"
#: The progressive-search one-line input's widget id (lode-olmi.4) -- read
#: back in tests.
SEARCH_INPUT_ID = "browse-search-input"
#: The edit screen's passive related-notes panel widget id (lode-aoc) -- read
#: back in tests.
EDIT_RELATED_ID = "edit-related-notes"
#: The version-history table's widget id -- read back in tests.
HISTORY_TABLE_ID = "version-history-table"
#: The read-only prior-version body's widget id -- read back in tests.
VERSION_BODY_ID = "version-view-body"
#: The enrichment inspector modal's dialog container id -- read back in tests.
INSPECTOR_DIALOG_ID = "enrichment-inspector-dialog"
#: The inspector's ``enrichment_state`` line id -- read back in tests.
INSPECTOR_STATE_ID = "inspector-state"
#: The inspector's summary line id -- read back in tests.
INSPECTOR_SUMMARY_ID = "inspector-summary"
#: The inspector's tags line id -- read back in tests.
INSPECTOR_TAGS_ID = "inspector-tags"
#: The inspector's entities line id -- read back in tests.
INSPECTOR_ENTITIES_ID = "inspector-entities"
#: The inspector's inferred-edges block id -- read back in tests.
INSPECTOR_EDGES_ID = "inspector-edges"
#: The inspector's embed-status line id -- read back in tests.
INSPECTOR_EMBED_ID = "inspector-embed"
#: The content-viewer modal's body ``TextArea`` widget id (lode-0sjj) -- read
#: back in tests.
SNAPSHOT_VIEWER_BODY_ID = "snapshot-viewer-body"
#: The content-viewer modal's dialog container id -- read back in tests.
SNAPSHOT_VIEWER_DIALOG_ID = "snapshot-viewer-dialog"
#: The many-externals picker table's widget id -- read back in tests.
EXTERNAL_PICKER_TABLE_ID = "external-picker-table"
#: Placeholder text for an empty section -- never suppressed, just labeled.
_NONE_TEXT = "(none)"

#: Left+right cell padding a ``DataTable`` adds *per column* -- used to work out
#: how much horizontal room the Summary column may claim without pushing the
#: table past the terminal width (which is what caused the horizontal scroll).
_CELL_PADDING = 2
#: Floor for the computed Summary width -- purely a crash guard so a very narrow
#: terminal can't hand ``add_column`` a zero/negative width.
_MIN_SUMMARY_WIDTH = 10
#: Fixed row height (lode-olmi.3, tightened from 2 to 1 by lode-juz8.3) -- a
#: long summary used to grow the row (and so the whole list) as tall as it
#: needed via ``height=None``; every row is now capped to this many lines,
#: with overflow ellipsized instead of wrapped. Summaries are prompted
#: lede-first (lode-juz8.5) so the single visible line still carries the
#: note's point.
_SUMMARY_ROW_HEIGHT = 1


def _wrap_summary_full(summary: str, width: int) -> tuple[str, int]:
    """Wrap *summary* to *width* with no line cap -- the full untruncated text.

    Companion to :func:`_clip_summary_to_row_height`, used for the one
    highlighted row a user has expanded (lode-juz8.4) instead of the
    1-line-capped rendering every other row gets. Returns the wrapped text
    and its line count together since the caller needs both in the same
    ``add_row`` call: the cell content and the row's ``height=``.
    """
    lines = textwrap.wrap(summary, width=width) or [""]
    return "\n".join(lines), len(lines)


def _clip_summary_to_row_height(summary: str, width: int) -> str:
    """Wrap *summary* to *width* and cap it at :data:`_SUMMARY_ROW_HEIGHT` lines.

    A ``DataTable`` row given a fixed ``height`` doesn't ellipsize overflow on
    its own -- it just clips whatever doesn't fit, mid-word, with no visual
    cue that anything is missing. So the wrapping is done here instead: the
    text is pre-wrapped to *width* and, if that produces more than
    :data:`_SUMMARY_ROW_HEIGHT` lines, the last visible line is truncated and
    given a trailing ellipsis so the cut is visible rather than silent.
    """
    if width <= 0:
        return summary
    lines = textwrap.wrap(summary, width=width) or [""]
    if len(lines) <= _SUMMARY_ROW_HEIGHT:
        return "\n".join(lines)
    kept = lines[:_SUMMARY_ROW_HEIGHT]
    last = kept[-1][: max(width - 1, 0)].rstrip()
    kept[-1] = f"{last}\N{HORIZONTAL ELLIPSIS}"
    return "\n".join(kept)


class VersionHistoryScreen(Screen[None]):
    """A note's version chain, newest (the head) first (lode-0wj.7).

    Pushed from :class:`EditScreen` via ``Ctrl+H`` (moved from the now-retired
    read-only note view, lode-olmi.2). Each row is one version (Date | Version
    | Op, mirroring :class:`BrowseScreen`'s own column style); selecting one
    pushes :class:`VersionViewScreen`, a read-only view of that exact
    version's body -- deliberately every row, including the current head,
    rather than filtering it out: picking the head row just shows the same
    body :class:`EditScreen` already has loaded, which is harmless and
    avoids an off-by-one special case for no real benefit.

    Escape pops back to :class:`EditScreen`, the same "one level at a time"
    contract every other browse-family screen uses.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def __init__(self, note_id: str) -> None:
        super().__init__()
        self.note_id = note_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id=HISTORY_TABLE_ID, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(f"#{HISTORY_TABLE_ID}", DataTable)
        table.add_columns("Date", "Version", "Op")
        for row in list_versions(self.app.db_path, self.note_id):
            table.add_row(
                format_adaptive_date(row.created),
                f"v{row.seq}",
                row.op,
                key=row.version_id,
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        version_id = event.row_key.value
        if version_id is not None:
            self.app.push_screen(VersionViewScreen(self.note_id, version_id))

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class VersionViewScreen(Screen[None]):
    """A read-only view of one specific (possibly non-head) version's body.

    Pushed from :class:`VersionHistoryScreen` on row-select. Escape pops back
    to that history list -- one level at a time, same as everywhere else in
    this module.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def __init__(self, note_id: str, version_id: str) -> None:
        super().__init__()
        self.note_id = note_id
        self.version_id = version_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield TextArea("", read_only=True, id=VERSION_BODY_ID)
        yield Footer()

    def on_mount(self) -> None:
        body = version_body(self.app.db_path, self.note_id, self.version_id)
        self.query_one(f"#{VERSION_BODY_ID}", TextArea).text = body or ""

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


def _item_text(item: EnrichmentItem) -> Text:
    """One tag/entity/summary value, dimmed if stale.

    Styles the ``stale`` bit directly rather than printing a baked-in suffix
    (lode-0qc) -- the whole reason :class:`~lode.enrichment_view.
    EnrichmentItem` carries ``stale`` as a structured flag instead of a
    ``" [stale]"``-suffixed string is so a consumer that wants to *style* a
    stale item (as this modal does) never has to string-sniff for the marker.
    """
    return Text(item.value, style="dim" if item.stale else "")


def _items_line(items: list[EnrichmentItem]) -> Text:
    """Every tag/entity on one comma-separated line, each styled by its own bit."""
    if not items:
        return Text(_NONE_TEXT)
    line = Text()
    for index, item in enumerate(items):
        if index:
            line.append(", ")
        line.append_text(_item_text(item))
    return line


def _summary_text(summary: EnrichmentItem | None) -> Text:
    """The note's one summary line, or the placeholder when it has none at all."""
    if summary is None:
        return Text(_NONE_TEXT)
    return _item_text(summary)


def _external_text(external: ExternalView) -> Text:
    """One edge's external-snapshot introspection sub-line (lode-8d2).

    Rendered directly beneath its edge's own line in the same Edges block --
    the external analogue of a note's tags/entities, through the exact
    :class:`~lode.enrichment_view.ExternalView` fields
    :func:`~lode.enrichment_view.enrichment_view` already assembled (no
    second DB read, no re-derived policy). Dimmed for ``stale``/``withheld``
    the same way a stale tag/edge already is (lode-0qc); the default
    ``un-refreshed`` state renders plain, but is still printed explicitly so
    all three states are equally visible in the modal.
    """
    line = (
        f"     {external.source_type} · snapshot "
        f"{short_version_id(external.snapshot_id)} · as of {external.fetched_at} "
        f"[{external.state}]"
    )
    return Text(line, style="dim" if external.state != "un-refreshed" else "")


def _edges_text(edges: list[EnrichmentEdge]) -> Text:
    """One inferred edge per line: target, reason, confidence -- dimmed if stale.

    ``reason``/``confidence`` are nullable on the seam (a user-curated edge may
    carry neither); missing values render as an explicit placeholder rather
    than a blank so the line never reads as truncated. When an edge draws down
    an external (``edge.external`` is not ``None``, lode-8d2), the target
    label is the bare ``to_id`` (the source URL) rather than the truncated
    :func:`~lode.notes_read.short_note_id` prefix -- that prefix is an
    8-char slice meant for note ids, not a URL -- and a second, indented line
    shows that external's snapshot introspection (:func:`_external_text`).
    """
    if not edges:
        return Text(_NONE_TEXT)
    block = Text()
    for index, edge in enumerate(edges):
        if index:
            block.append("\n")
        reason = edge.reason if edge.reason is not None else "no reason recorded"
        confidence = f"{edge.confidence:.2f}" if edge.confidence is not None else "n/a"
        target = edge.to_id if edge.external is not None else short_note_id(edge.to_id)
        line = f"-> {target} ({reason}, {confidence})"
        block.append(line, style="dim" if edge.stale else "")
        if edge.external is not None:
            block.append("\n")
            block.append_text(_external_text(edge.external))
    return block


def _resolve_externals(db_path: Path, note_id: str) -> list[ExternalView]:
    """*note_id*'s drawn-down external edges, in edge order (lode-0sjj).

    The one place the "which externals does this note have" question is
    answered for the content-viewer feature -- both
    :meth:`BrowseScreen.action_view_content` and
    :meth:`EditScreen.action_view_content` resolve through this (via
    :func:`_view_note_external_content`) rather than each independently
    filtering :func:`~lode.enrichment_view.enrichment_view`'s edges, which
    would risk the two screens silently drifting onto different rules. A
    missing note (should never happen -- both callers only ever have a real,
    live ``note_id`` in hand) returns an empty list rather than raising; an
    empty result and "note exists but has no external edges" are
    indistinguishable to the caller, which is fine -- both mean "notify, don't
    view anything."
    """
    view = enrichment_view(db_path, note_id)
    if view is None:
        return []
    return [edge.external for edge in view.edges if edge.external is not None]


def _view_note_external_content(screen: Screen[None], note_id: str) -> None:
    """Resolve *note_id*'s externals and push the right viewer (lode-0sjj).

    Shared by :meth:`BrowseScreen.action_view_content` (bare ``v``) and
    :meth:`EditScreen.action_view_content` (a Ctrl-prefixed key) so the
    zero/one/many addressing rule lives in exactly one place -- mirroring
    ``lode dump-html``'s CLI disambiguation (lode-olmi.7) on purpose. Zero
    externals notifies cleanly; exactly one pushes
    :class:`SnapshotViewerScreen` directly; more than one pushes
    :class:`ExternalPickerScreen` first, which pushes the viewer itself once
    a row is chosen.
    """
    externals = _resolve_externals(screen.app.db_path, note_id)
    if not externals:
        screen.notify("no retrieved content for this note", severity="warning")
    elif len(externals) == 1:
        screen.app.push_screen(SnapshotViewerScreen(externals[0].snapshot_id))
    else:
        screen.app.push_screen(ExternalPickerScreen(externals))


class ExternalPickerScreen(Screen[None]):
    """List a note's external edges so the user can pick one to view (lode-0sjj).

    Pushed by :func:`_view_note_external_content` only when a note has more
    than one external edge -- the "many" branch of the zero/one/many
    addressing rule shared with ``lode dump-html`` (lode-olmi.7). Each row is
    one :class:`~lode.enrichment_view.ExternalView` (Source | Snapshot |
    Fetched | State -- the same fields :func:`_external_text` already renders
    beneath an edge line in :class:`EnrichmentModalScreen`); selecting one
    pushes :class:`SnapshotViewerScreen` for that row's ``snapshot_id``.

    Mirrors :class:`VersionHistoryScreen`'s DataTable-then-select shape
    exactly (a plain, non-modal ``Screen``, not a ``ModalScreen`` -- there is
    no "dimmed screen underneath" need here, just a list to pick from).
    Escape pops back one level, the same contract every other screen in this
    module uses.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def __init__(self, externals: list[ExternalView]) -> None:
        super().__init__()
        self._externals = externals

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id=EXTERNAL_PICKER_TABLE_ID, cursor_type="row")
        yield Footer()

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

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class SnapshotViewerScreen(ModalScreen[None]):
    """A retrieved external's stored content -- body by default, raw on toggle (lode-0sjj).

    Pushed keyed to one ``snapshot_id`` by :func:`_view_note_external_content`
    -- directly, for a note with exactly one external edge, or after
    :class:`ExternalPickerScreen` resolves which of several. Shows
    ``snapshots.body`` (the extracted text -- ``NOT NULL``, ``schema.sql``;
    even a tombstone snapshot carries a stable placeholder body,
    :func:`lode.externals.tombstone_body`) in a read-only ``TextArea`` by
    default. ``Binding('t', 'toggle_raw', ...)`` switches to
    ``snapshots.raw_payload`` instead -- the same nullable raw-HTML column
    ``lode dump-html`` (lode-olmi.7) prints to stdout -- and back again on a
    second press. Unlike that CLI command, a missing ``raw_payload`` here
    isn't an error: it notifies ``'no raw HTML captured for this source'``
    and stays on the body, since the body is still perfectly viewable and the
    toggle simply has nothing to switch to (never a blank toggle).

    ``Esc`` dismisses -- the same one-level-at-a-time contract every other
    modal in this module uses. Bare printable ``t`` is safe here (unlike
    :class:`EditScreen`'s own binding for reaching this screen,
    ``docs/keybindings.md``): this screen's body ``TextArea`` is
    ``read_only=True``, so it never intercepts a printable keypress before a
    Screen-level binding sees it (the same reason ``NoteViewScreen``'s
    read-only body could bind bare ``h``, back when that screen existed).
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("t", "toggle_raw", "Toggle raw HTML"),
    ]

    def __init__(self, snapshot_id: str) -> None:
        super().__init__()
        self.snapshot_id = snapshot_id
        self._snapshot: SnapshotRow | None = None
        self._showing_raw = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            TextArea("", read_only=True, id=SNAPSHOT_VIEWER_BODY_ID),
            id=SNAPSHOT_VIEWER_DIALOG_ID,
        )

    def on_mount(self) -> None:
        snapshot = read_snapshot(self.app.db_path, self.snapshot_id)
        if snapshot is None:
            # Only ever pushed for a snapshot_id an already-assembled
            # ExternalView carried, so a missing row here would be a real
            # bug, not a normal race worth a soft fallback -- the same stance
            # EnrichmentModalScreen.on_mount takes for a missing note.
            raise LookupError(f"no snapshot {self.snapshot_id!r} to view")
        self._snapshot = snapshot
        self._show_body()

    def _show_body(self) -> None:
        self._showing_raw = False
        assert self._snapshot is not None
        self.query_one(
            f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea
        ).text = self._snapshot.body

    def action_toggle_raw(self) -> None:
        """``t``: switch to the raw HTML, or back to the body from there."""
        assert self._snapshot is not None
        if self._showing_raw:
            self._show_body()
            return
        if not self._snapshot.raw_payload:
            self.notify("no raw HTML captured for this source", severity="warning")
            return
        self._showing_raw = True
        self.query_one(
            f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea
        ).text = self._snapshot.raw_payload

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class EnrichmentModalScreen(ModalScreen[None]):
    """A glance-and-dismiss popup over one note's full enrichment (lode-ay5.2).

    Pushed from :meth:`BrowseScreen.action_inspect_selected` via ``i`` on the
    highlighted row, keyed to that row's ``note_id`` the same way ``e``
    resolves one for :class:`EditScreen`. Renders
    :func:`lode.enrichment_view.enrichment_view` verbatim -- summary, tags,
    entities, inferred edges (reason+confidence+stale), embed status, and the
    three-valued ``enrichment_state`` -- with **no** DB access or display
    policy of its own; this screen only shapes the already-decided fields into
    widgets. The module-level ``_item_text``/``_items_line``/``_edges_text``
    helpers above do the one bit of real work this modal owns: styling
    ``stale`` dim instead of string-sniffing a suffix (lode-0qc; see
    ``docs/storage.md``'s "Enrichment view-model" section).

    Content lives in a :class:`~textual.containers.VerticalScroll` (not a
    fixed :class:`~textual.containers.Vertical`, unlike
    :class:`~lode.tui.screens.capture.DiscardConfirmScreen`'s small fixed
    dialog) so a note with many tags/entities/edges scrolls within the popup
    rather than overflowing or truncating. ``Esc`` pops back to
    :class:`BrowseScreen` -- the same one-level-at-a-time contract every other
    screen in this module already uses. Like ``DiscardConfirmScreen``, this is
    a bare ``ModalScreen`` pushed directly (not a :data:`~lode.tui.app.
    LodeApp.SCREENS` entry): it dims the table underneath for free via
    ``ModalScreen``'s own ``DEFAULT_CSS``, and ``lode.tcss`` adds only sizing
    and centering for :data:`INSPECTOR_DIALOG_ID`.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def __init__(self, note_id: str) -> None:
        super().__init__()
        self.note_id = note_id

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static("", id=INSPECTOR_STATE_ID),
            Static("", id=INSPECTOR_SUMMARY_ID),
            Static("", id=INSPECTOR_TAGS_ID),
            Static("", id=INSPECTOR_ENTITIES_ID),
            Static("", id=INSPECTOR_EDGES_ID),
            Static("", id=INSPECTOR_EMBED_ID),
            id=INSPECTOR_DIALOG_ID,
        )

    def on_mount(self) -> None:
        view = enrichment_view(self.app.db_path, self.note_id)
        if view is None:
            # BrowseScreen only ever pushes this for a row already in the
            # (live-only) table, so a missing note here would be a real bug,
            # not a normal race worth a soft fallback -- the same stance
            # EditScreen.on_mount takes for a missing head.
            raise LookupError(f"no live note {self.note_id!r} to inspect")

        self.query_one(f"#{INSPECTOR_STATE_ID}", Static).update(
            f"Enrichment: {view.enrichment_state}"
        )
        self.query_one(f"#{INSPECTOR_SUMMARY_ID}", Static).update(
            Text("Summary: ") + _summary_text(view.summary)
        )
        self.query_one(f"#{INSPECTOR_TAGS_ID}", Static).update(
            Text("Tags: ") + _items_line(view.tags)
        )
        self.query_one(f"#{INSPECTOR_ENTITIES_ID}", Static).update(
            Text("Entities: ") + _items_line(view.entities)
        )
        self.query_one(f"#{INSPECTOR_EDGES_ID}", Static).update(
            Text("Edges:\n") + _edges_text(view.edges)
        )
        self.query_one(f"#{INSPECTOR_EMBED_ID}", Static).update(
            f"Embedded: {'yes' if view.embedded else 'no'} "
            f"({view.passage_count} passages)"
        )

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class DeleteConfirmScreen(ModalScreen[bool]):
    """A small Yes/No confirm before a browse-row soft-delete (lode-d32.1).

    Mirrors :class:`~lode.tui.screens.capture.DiscardConfirmScreen`'s popup
    *styling* (bordered, centered dialog over the dimmed screen beneath,
    lode-1i8.4) but not its Save/Discard/Cancel choices -- there is nothing to
    save here, just "yes, delete" or "no, don't." Dismisses with a ``bool``:
    ``True`` on confirm, ``False`` on decline or Escape.
    """

    BINDINGS = [
        Binding("y", "choose(True)", "Yes, delete"),
        Binding("n", "choose(False)", "No, cancel"),
        Binding("escape", "choose(False)", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                "Delete this note? (Y)es / (N)o",
                id=DELETE_CONFIRM_MESSAGE_ID,
            ),
            id="delete-confirm-dialog",
        )

    def action_choose(self, confirmed: bool) -> None:
        self.dismiss(confirmed)


class BrowseScreen(Screen[None]):
    """Id | Date | Version | Summary, newest-first, over every live note."""

    # Descriptions kept short (lode-l38d.3): the stock Footer renders all 7 of
    # these plus the 4 App-level bindings (LodeApp.BINDINGS) on one line, and at
    # full length that clipped at 80 columns (128 columns' worth of content).
    # Every binding stays visible and only the description text was shortened --
    # hiding entries via show=False was ruled out on lode-l38d.3, because the
    # footer is the only surface these keys are discoverable on.
    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("i", "inspect_selected", "Insp"),
        Binding("v", "view_content", "View"),
        Binding("d", "delete_selected", "Del"),
        Binding("x", "toggle_summary", "Exp"),
        Binding("slash", "search_forward", "Find"),
        Binding("question_mark", "search_backward", "Up"),
    ]

    def __init__(self) -> None:
        super().__init__()
        #: Which way the last-opened search box scans (lode-olmi.4): +1 for
        #: ``/`` (downward), -1 for ``?`` (upward). Set in :meth:`_open_search`,
        #: read by :meth:`_seek_match` on every keystroke.
        self._search_direction = 1
        #: Whether the search box is currently open -- read by
        #: :meth:`action_dismiss_screen` to decide what Escape means right now
        #: (close the search box vs. pop back to capture).
        self._search_open = False
        #: The ``note_id`` of the one row currently showing its full,
        #: untruncated summary (lode-juz8.4), or ``None`` when every row is
        #: 1-line-capped. Set/cleared by :meth:`action_toggle_summary`; reset
        #: to ``None`` by :meth:`on_screen_resume`/:meth:`on_resize` before
        #: their own reload, so expansion does not survive tabbing away or a
        #: resize (see the module docstring's lode-juz8.4 section).
        self._expanded_note_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id=TABLE_ID, cursor_type="row")
        yield Input(id=SEARCH_INPUT_ID, placeholder="Search summaries...")
        # Still the stock Footer, just asked for less padding (lode-l38d.3).
        # compact=True trims Textual's built-in FooterKey padding from 3 columns
        # of overhead per entry to 1 -- across 11 entries that alone is 22
        # columns, which is most of what makes the bar fit. Textual also
        # auto-adds a 12th "^p palette" entry regardless of BINDINGS;
        # show_command_palette=False hides only that icon (ctrl+p still opens
        # the palette) and buys the last few columns, which is what lets all 11
        # real bindings stay visible without cryptic single-letter labels.
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        # Columns are (re)built in _reload_rows, not here: the Summary column's
        # width depends on the current terminal width, which _reload_rows reads
        # back off the laid-out table. on_mount only needs to take focus.
        self.query_one(f"#{TABLE_ID}", DataTable).focus()
        # Closed by default (lode-olmi.4) -- display=False claims no vertical
        # space, so the "one-line input box at the bottom" only appears once
        # '/' or '?' is pressed.
        self.query_one(f"#{SEARCH_INPUT_ID}", Input).display = False

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
        to that budget by :func:`_clip_summary_to_row_height` before it ever
        reaches the table -- overflow is truncated, not wrapped further.
        Summaries are prompted lede-first (lode-juz8.5) so the truncated line
        still carries the note's point.

        **One row can opt out (lode-juz8.4).** When :attr:`_expanded_note_id`
        names a row still present in *rows*, that one row is rendered with
        :func:`_wrap_summary_full` instead -- the full, untruncated summary,
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
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        selected_note_id: str | None = None
        if table.row_count > 0:
            selected_note_id = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key.value
        rows = list_notes(self.app.db_path)
        table.clear(columns=True)

        # Id/Date/Version keep their natural widths -- the same max(header,
        # widest cell) a DataTable auto-column would pick. Date is the short
        # adaptive form (lode-1gr.8), not the full ISO-8601 timestamp --
        # shorter, so it frees more of that natural width for Summary below;
        # Id is the shared 8-char note-id abbreviation (lode-1gr.2), not the
        # full id -- EditScreen shows that instead, where there's no width
        # budget to protect.
        id_cells = [short_note_id(row.note_id) for row in rows]
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

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter/select on a row opens that note's editor directly (lode-olmi.2)."""
        note_id = event.row_key.value
        if note_id is not None:
            self.app.push_screen(EditScreen(note_id))

    def action_inspect_selected(self) -> None:
        """``i``: open the highlighted row's enrichment inspector modal."""
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        if table.row_count == 0:
            return
        note_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if note_id is not None:
            self.app.push_screen(EnrichmentModalScreen(note_id))

    def action_view_content(self) -> None:
        """``v``: view the highlighted row's retrieved external content, if any (lode-0sjj)."""
        table = self.query_one(f"#{TABLE_ID}", DataTable)
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
        table = self.query_one(f"#{TABLE_ID}", DataTable)
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

    def action_delete_selected(self) -> None:
        """``d``: soft-delete the highlighted row's note, after confirming (lode-d32.1)."""
        table = self.query_one(f"#{TABLE_ID}", DataTable)
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

    def action_search_forward(self) -> None:
        """``/``: open the progressive search box, scanning downward (lode-olmi.4)."""
        self._open_search(direction=1)

    def action_search_backward(self) -> None:
        """``?``: open the progressive search box, scanning upward (lode-olmi.4)."""
        self._open_search(direction=-1)

    def _open_search(self, *, direction: int) -> None:
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        if table.row_count == 0:
            return
        self._search_direction = direction
        self._search_open = True
        search_input = self.query_one(f"#{SEARCH_INPUT_ID}", Input)
        search_input.value = ""
        search_input.display = True
        search_input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Every keystroke re-scans from the cursor's current row (lode-olmi.4)."""
        if event.input.id != SEARCH_INPUT_ID:
            return
        self._seek_match(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter: confirm and close the box, keeping wherever the search landed."""
        if event.input.id != SEARCH_INPUT_ID:
            return
        self._close_search()

    def _seek_match(self, query: str) -> None:
        """Move the cursor to the closest row (in ``_search_direction``) whose
        Summary contains ``query``, case-insensitive, wrapping if needed.

        An empty query is a no-op (acceptance criteria) -- returns immediately
        rather than "matching" every row. Scanning starts at ``offset=0`` (the
        cursor's own current row), so a query that already matches where the
        cursor sits leaves it in place instead of jumping to the *next*
        occurrence.
        """
        if not query:
            return
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        row_count = table.row_count
        if row_count == 0:
            return
        needle = query.lower()
        start = table.cursor_row
        direction = self._search_direction
        for offset in range(row_count):
            candidate = (start + offset * direction) % row_count
            summary = str(table.get_row_at(candidate)[3])
            if needle in summary.lower():
                table.move_cursor(row=candidate)
                return

    def _close_search(self) -> None:
        search_input = self.query_one(f"#{SEARCH_INPUT_ID}", Input)
        search_input.display = False
        search_input.value = ""
        self._search_open = False
        self.query_one(f"#{TABLE_ID}", DataTable).focus()

    def action_dismiss_screen(self) -> None:
        """Escape: close an open search box first, else pop back to capture.

        The same key means two different things depending on whether the
        search box is open (lode-olmi.4) -- closing it keeps the current
        selection rather than popping the whole screen out from under it.
        """
        if self._search_open:
            self._close_search()
            return
        self.app.pop_screen()


class EditScreen(Screen[None]):
    """An existing note's live head, loaded editable (lode-0wj.6).

    Saving reparents the buffer onto the loaded head via the CAS path
    (:func:`lode.tui.edit.save_edit`) -- a new *version* on this note's
    chain, never a new note. Escape returns to the browse list, reusing
    capture's Save/Discard/Cancel confirm
    (:class:`~lode.tui.screens.capture.DiscardConfirmScreen`, lode-0wj.1) --
    but **this is the first screen where the dirty check can't be "is the
    buffer non-empty"**: a freshly loaded existing version is non-empty by
    construction, so that check would wrongly confirm on every Escape even
    with zero edits. :meth:`action_cancel` instead compares the live buffer
    against the body loaded at :meth:`on_mount` -- "changed since it was
    opened," the same standard the CAS layer itself uses for the head.

    **App-level Ctrl+Q (lode-b14).** :meth:`confirm_quit` gives
    ``LodeApp.action_quit`` (lode-0wj.8's generic "ask the current screen"
    hook) the same unchanged-vs-edited dirty check, but its own
    Save/Discard/Cancel resolution ends in ``self.app.exit()`` /
    ``self.app.exit(note_id)`` -- never ``pop_screen`` -- matching Ctrl+Q's
    global "quit the whole app" contract rather than Escape's "back to
    browse" one. The two can't share a method (see :meth:`action_cancel`'s
    docstring); mirrors :meth:`~lode.tui.screens.capture.CaptureScreen.confirm_quit`'s
    contract exactly.

    **Passive related-notes panel (lode-aoc).** Composes the same
    :class:`~lode.tui.related_notes_panel.RelatedNotesPanel` widget
    :class:`~lode.tui.screens.capture.CaptureScreen` uses, for parity --
    "you wrote about this before" while editing, not just while capturing a
    brand-new note. Constructed with ``exclude_note_id=self.note_id`` so the
    note being edited never matches its own (near-identical) draft. Needs no
    reset call of its own anywhere this screen exits: unlike capture's Ctrl+N
    (which keeps the screen alive for a fresh note), every exit here either
    pops or tears down this screen, and Textual cancels a screen's workers on
    unmount -- the same guarantee the panel's own module docstring relies on.

    **Row-select opens here directly; full id; version history (lode-olmi.2).**
    Before this, row-select pushed a separate read-only note view first, and
    this screen was only reached via a distinct ``e`` keypress -- both now
    retired, since selecting a row *is* "I want to edit this note." Two things
    that screen used to own move in along with it: the header's ``sub_title``
    now shows the full 36-char ``note_id`` (:meth:`on_mount`, same as the
    retired screen did -- selectable/copyable, unlike the Browse table's
    8-char abbreviation, which has a width budget to protect), and ``Ctrl+H``
    pushes :class:`VersionHistoryScreen` for this note (:meth:`action_show_history`).
    ``Ctrl+H``, not bare ``h``: this screen's body ``TextArea`` is editable
    (unlike the retired note view's), and Textual's ``TextArea`` consumes
    every ``is_printable`` keypress -- including a bare ``h`` -- before a
    Screen-level, non-priority ``Binding`` ever sees it (confirmed empirically
    -- a bare ``h`` binding here would insert the literal letter into the note
    body instead of opening history). ``Ctrl+S``/``Ctrl+N`` already use this
    same non-printable-key escape hatch on this and capture's screen, for the
    identical reason.

    **Enrichment inspector, Ctrl+G not bare ``i`` (lode-g5es).**
    :meth:`action_inspect_selected` pushes the same
    :class:`EnrichmentModalScreen` :meth:`BrowseScreen.action_inspect_selected`
    does, keyed to ``self.note_id`` -- "was anything retrieved for this note,"
    reachable while editing, not just from the browse row. It is bound to
    ``Ctrl+G``, not ``BrowseScreen``'s bare ``i``: this screen's body
    ``TextArea`` is editable, so a bare ``i`` would type a literal letter
    instead of opening the modal, the identical trap ``Ctrl+H`` above exists
    to dodge. Two more letters that look tempting fail for their own
    reasons: ``Ctrl+I`` is *not* a safe substitute for bare ``i`` -- terminals
    encode Ctrl+I as the Tab control character, and Textual's ``KEY_ALIASES``
    reflects that (``"tab": ["ctrl+i"]``), so a ``ctrl+i`` binding is
    indistinguishable from ``tab`` -- a non-printable navigation key -- and
    would be silently unreachable too (confirmed empirically: pressing it with
    the body focused neither opens the inspector nor types anything)
    -- and ``Ctrl+P`` (a natural "peek" pick, matching this modal's own
    glance-and-dismiss contract) collides with Textual's own App-level
    command-palette binding, which is registered with ``priority=True`` and
    so wins over *any* Screen-level binding on the same key, confirmed
    empirically (pressing it opened ``CommandPalette``, never the inspector).
    ``Ctrl+G`` ("glance") is free of all three traps. This is project
    practice, not a one-off: every action this screen binds beyond Escape
    uses a ``Ctrl+``-prefixed (or otherwise non-printable) key for exactly
    this reason -- see ``docs/keybindings.md``.

    **Content viewer, Ctrl+R not bare ``v`` (lode-0sjj).**
    :meth:`action_view_content` resolves this note's external edges the same
    way :meth:`BrowseScreen.action_view_content` does (via the shared
    :func:`_view_note_external_content`) and pushes
    :class:`SnapshotViewerScreen` (zero/one) or :class:`ExternalPickerScreen`
    (many). ``BrowseScreen``'s binding for the identical feature is bare
    ``v`` -- safe there because its focused widget is a ``DataTable``, not an
    editable ``TextArea``. Here the body is editable, so the literal ``v``
    key lode-olmi.8's design named would just type a letter, exactly the
    ``Ctrl+H``/``Ctrl+G`` trap above; a human resolved this 2026-07-15 as a
    Ctrl-prefixed key, consistent with every other action on this screen.
    ``Ctrl+R`` ("retrieved") was checked against all three traps
    ``docs/keybindings.md`` catalogs before landing on it: it is not one of
    ``TextArea``'s own builtin bindings (``ctrl+a/e/w/d/x/c/v/u/k/z/y``, see
    that doc's table), Textual's ``KEY_ALIASES`` does not alias it to a
    non-printable key (unlike ``ctrl+i`` -> ``tab``, ``ctrl+m`` -> ``enter``),
    and it is not one of ``App``'s own ``priority=True`` reservations (unlike
    ``ctrl+p``, the command palette) -- confirmed empirically, not just by
    inspection, the same standard ``Ctrl+G``'s own candidates were held to.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Back"),
        Binding("ctrl+f", "focus_related", "Related"),
        Binding("ctrl+h", "show_history", "History"),
        Binding("ctrl+g", "inspect_selected", "Inspect"),
        Binding("ctrl+r", "view_content", "View content"),
    ]

    def __init__(self, note_id: str) -> None:
        super().__init__()
        self.note_id = note_id
        #: The head this screen loaded and will CAS against on save; also the
        #: unchanged-vs-edited baseline for the dirty check. Set in
        #: :meth:`on_mount` from the note's live head -- ``BrowseScreen``
        #: only ever pushes this screen for a row already in the (live-only)
        #: table, so a missing/tombstoned note here would be a real bug, not
        #: a normal race worth a soft fallback.
        self._loaded_head = ""
        self._loaded_body = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            TextArea(id=EDIT_BODY_ID),
            RelatedNotesPanel(exclude_note_id=self.note_id, id=EDIT_RELATED_ID),
        )
        yield Footer()

    def on_mount(self) -> None:
        # Full 36-char id (lode-1gr.2/lode-olmi.2) -- selectable/copyable in
        # the header, unlike Browse's 8-char abbreviated Id column, which has
        # a width budget to protect.
        self.sub_title = self.note_id
        head = load_head(self.app.db_path, self.note_id)
        if head is None:
            raise LookupError(f"no live note {self.note_id!r} to edit")
        self._loaded_head, self._loaded_body = head
        text_area = self.query_one(f"#{EDIT_BODY_ID}", TextArea)
        # Setting .text posts a TextArea.Changed message (load_text), so this
        # also primes the related-notes panel with the just-loaded body via
        # on_text_area_changed below -- editing an existing note surfaces
        # related notes for its starting content, not only once the user
        # types further.
        text_area.text = self._loaded_body
        text_area.focus()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Forward the body's text to the related-notes panel (lode-aoc).

        Guarded to this screen's own body id, mirroring
        :meth:`~lode.tui.screens.capture.CaptureScreen.on_text_area_changed`.
        """
        if event.text_area.id != EDIT_BODY_ID:
            return
        self.query_one(RelatedNotesPanel).update_draft(event.text_area.text)

    def action_focus_related(self) -> None:
        """Ctrl+F: move focus onto the related-notes panel (lode-olmi.9).

        Its own Up/Down/Enter bindings only fire while it holds focus (see
        :mod:`lode.tui.related_notes_panel`'s module docstring) — the body
        ``TextArea`` consumes those keys itself while typing, so this is the
        only way to reach them.
        """
        self.query_one(RelatedNotesPanel).focus()

    def action_show_history(self) -> None:
        """Ctrl+H: push this note's version-history list (lode-0wj.7/lode-olmi.2).

        Not bare ``h`` -- the body ``TextArea`` is editable here and consumes
        every printable keypress before a Screen-level binding can fire (see
        this class's docstring).
        """
        self.app.push_screen(VersionHistoryScreen(self.note_id))

    def action_inspect_selected(self) -> None:
        """Ctrl+G: open this note's enrichment inspector modal (lode-g5es).

        Mirrors :meth:`BrowseScreen.action_inspect_selected` -- same modal,
        same glance-and-dismiss contract, keyed to ``self.note_id`` directly
        (this screen always has exactly one note loaded, unlike Browse's
        table, which needs the highlighted row). See this class's docstring
        for why ``Ctrl+G`` rather than bare ``i``, ``Ctrl+I``, or ``Ctrl+P``.
        """
        self.app.push_screen(EnrichmentModalScreen(self.note_id))

    def action_view_content(self) -> None:
        """Ctrl+R: view this note's retrieved external content, if any (lode-0sjj).

        Not bare ``v`` -- this screen's body ``TextArea`` is editable and
        consumes every printable keypress before a Screen-level binding ever
        fires, the identical trap ``Ctrl+H``/``Ctrl+G`` above exist to dodge
        (this class's docstring; ``docs/keybindings.md``). ``Ctrl+R``
        ("retrieved") is free of the same three traps checked there: it isn't
        a builtin ``TextArea`` binding (``ctrl+a/e/w/d/x/c/v/u/k/z/y`` are, see
        that doc), Textual's ``KEY_ALIASES`` doesn't remap it to a
        non-printable key the way ``ctrl+i``/``ctrl+m`` are, and ``App``
        doesn't reserve it with ``priority=True`` the way ``ctrl+p`` is for
        the command palette.
        """
        _view_note_external_content(self, self.note_id)

    def action_save(self) -> None:
        """Ctrl+S: append a new version onto this note's chain, or explain why not."""
        body = self.query_one(f"#{EDIT_BODY_ID}", TextArea).text
        result = self._attempt_save(body)
        if result is None:
            return
        if isinstance(result, EditConflict):
            self.app.push_screen(
                ReconcileScreen(result, on_resolved=self._on_reconcile_resolved)
            )
            return
        self.app.pop_screen()

    def _attempt_save(self, body: str) -> SaveResult | EditConflict | None:
        """Try the CAS save; ``None`` means refused-as-empty (already notified).

        Shared by :meth:`action_save` (Ctrl+S) and :meth:`_on_quit_confirm`
        (Ctrl+Q's confirm-then-save) -- both need the identical save attempt,
        they only differ on what happens *after* a clean save or a conflict
        (back to browse vs. quit the app).
        """
        app = self.app
        try:
            return save_edit(
                app.db_path,
                self.note_id,
                body,
                parent=self._loaded_head,
                settings=app.settings,
            )
        except EmptyEditError:
            self.notify("Refusing to save an empty note.", severity="warning")
            return None

    def _on_reconcile_resolved(self, result: SaveResult | None) -> None:
        """The pushed ``ReconcileScreen`` resolved (re-applied or discarded).

        Unlike capture's root-screen use of ``ReconcileScreen`` (which ends
        the whole app on resolve), this screen is itself pushed on top of
        ``BrowseScreen`` -- "resolved" here means popping back to the list:
        first this reconcile screen, then this edit screen underneath it.
        """
        del result  # Same next step either way: back to the browse list.
        self.app.pop_screen()  # ReconcileScreen
        self.app.pop_screen()  # this EditScreen -> BrowseScreen

    def action_cancel(self) -> None:
        """Escape: return to the list immediately if unchanged, else confirm first.

        Deliberately **not** named ``confirm_quit`` -- that name is
        ``LodeApp.action_quit``'s app-level Ctrl+Q hook (lode-0wj.8), whose
        contract is "confirm, then *quit the whole app*." This screen's
        Escape means "confirm, then go back to the browse list" instead, a
        different final action -- reusing this method for Ctrl+Q would make
        it silently just navigate back rather than quit, so Ctrl+Q instead
        gets its own :meth:`confirm_quit` (lode-b14) that ends in
        ``self.app.exit()`` rather than ``pop_screen``.
        """
        body = self.query_one(f"#{EDIT_BODY_ID}", TextArea).text
        if body == self._loaded_body:
            self.app.pop_screen()
            return
        self.app.push_screen(DiscardConfirmScreen(), self._on_discard_confirm)

    def _on_discard_confirm(self, choice: str) -> None:
        """Act on the confirm dialog's answer: save, discard, or resume editing."""
        if choice == "save":
            self.action_save()
        elif choice == "discard":
            self.app.pop_screen()
        # "cancel" (or the dialog dismissing with no answer): stay right here,
        # buffer untouched.

    def confirm_quit(self) -> None:
        """Exit the whole app immediately if unchanged, else confirm first.

        ``LodeApp.action_quit``'s app-level Ctrl+Q hook (lode-0wj.8) calls
        this generically, the same way it calls
        :meth:`~lode.tui.screens.capture.CaptureScreen.confirm_quit` -- and
        mirrors that method's contract exactly. The dirty check is the same
        "changed since :meth:`on_mount` loaded it" comparison
        :meth:`action_cancel` uses (not "is the buffer empty" -- a freshly
        loaded existing version is never empty). Unlike
        :meth:`action_cancel`/:meth:`_on_discard_confirm` (Escape's "back to
        browse" contract, ``pop_screen``), every branch here ends in
        ``self.app.exit()`` / ``self.app.exit(note_id)``, matching Ctrl+Q's
        global "quit the whole app" contract.
        """
        body = self.query_one(f"#{EDIT_BODY_ID}", TextArea).text
        if body == self._loaded_body:
            self.app.exit()
            return
        self.app.push_screen(DiscardConfirmScreen(), self._on_quit_confirm)

    def _on_quit_confirm(self, choice: str) -> None:
        """Act on Ctrl+Q's confirm dialog answer: save-then-quit, quit, or resume.

        A conflict on save pushes :class:`~lode.tui.screens.reconcile.ReconcileScreen`
        with no ``on_resolved`` override -- its default already ends in
        ``self.app.exit()`` / ``self.app.exit(note_id)`` (the same default
        :meth:`~lode.tui.screens.capture.CaptureScreen` relies on), which is
        exactly this method's own "quit the app" contract.
        """
        if choice == "save":
            body = self.query_one(f"#{EDIT_BODY_ID}", TextArea).text
            result = self._attempt_save(body)
            if result is None:
                return
            if isinstance(result, EditConflict):
                self.app.push_screen(ReconcileScreen(result))
                return
            self.app.exit(self.note_id)
        elif choice == "discard":
            self.app.exit()
        # "cancel" (or the dialog dismissing with no answer): stay right here,
        # buffer untouched.
