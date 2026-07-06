"""The browse screen (lode-0wj.5) -- list live notes, pick one to view or edit.

``docs/design.md``'s post-E11 feedback: a way to see what you've captured
without leaving the terminal. Reached from :class:`~lode.tui.screens.capture.
CaptureScreen` via the app-level ``F3`` binding (:mod:`lode.tui.app`, the same
"reachable from anywhere" convention ``F2``'s config screen already uses).
This screen owns no read logic of its own -- it only renders the rows
:func:`lode.tui.browse.list_notes` returns into a ``DataTable`` (Date |
Version | Summary, newest-first, live notes only) and reacts to a row select
or an edit key press.

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

**Edit an existing note (lode-0wj.6).** ``e`` on a highlighted row -- "edit an
existing note *from the browse screen*," per the ticket title -- pushes
:class:`EditScreen` directly, without detouring through the read-only
``NoteViewScreen`` first. Saving there appends a new version onto that note's
chain via the CAS head path (:mod:`lode.tui.edit`), never a new note; the
table is stale the moment that happens (its Version/Summary columns no
longer match the just-written head), so :meth:`BrowseScreen.on_screen_resume`
-- Textual's hook for "this screen is visible again" -- reloads the table
every time browse becomes the top screen again (a fresh ``F3``, or popping
back from ``NoteViewScreen``/``EditScreen``), not only on first mount.

**View prior versions (lode-0wj.7).** ``h`` on :class:`NoteViewScreen` pushes
:class:`VersionHistoryScreen` -- a Date | Version | Op table over the note's
whole chain (:func:`lode.tui.browse.list_versions`), newest (the head) first.
Selecting a row pushes :class:`VersionViewScreen`, a read-only view of that
exact version's body (:func:`lode.tui.browse.version_body`) -- the same
"read-only ``TextArea``" pattern ``NoteViewScreen`` itself already uses, just
keyed to a specific ``version_id`` instead of always the live head. Escape
pops one level at a time here too: version body -> history list -> note view,
falling out of the same Textual screen-stack pop every other Escape in this
module already relies on.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, TextArea

from lode.tui.browse import list_notes, list_versions, note_body, version_body
from lode.tui.edit import EditConflict, EmptyEditError, load_head, save_edit
from lode.tui.screens.capture import DiscardConfirmScreen
from lode.tui.screens.reconcile import ReconcileScreen
from lode.versions import SaveResult

#: The notes table's widget id -- read back in tests.
TABLE_ID = "browse-table"
#: The read-only note body's widget id -- read back in tests.
NOTE_BODY_ID = "note-view-body"
#: The editable note body's widget id -- read back in tests.
EDIT_BODY_ID = "note-edit-body"
#: The version-history table's widget id -- read back in tests.
HISTORY_TABLE_ID = "version-history-table"
#: The read-only prior-version body's widget id -- read back in tests.
VERSION_BODY_ID = "version-view-body"


class NoteViewScreen(Screen[None]):
    """A read-only view of one note's live head body.

    **Version history (lode-0wj.7).** ``h`` pushes :class:`VersionHistoryScreen`
    for this same ``note_id`` -- "expose its version history" from a note
    already opened in browse, per the ticket title. Escape here still pops
    straight back to :class:`BrowseScreen`, unaffected: the history screen is
    reached and left via its own push/pop, not a change to this screen's own
    Escape contract.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("h", "show_history", "History"),
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

    def action_show_history(self) -> None:
        """``h``: push this note's version-history list (lode-0wj.7)."""
        self.app.push_screen(VersionHistoryScreen(self.note_id))


class VersionHistoryScreen(Screen[None]):
    """A note's version chain, newest (the head) first (lode-0wj.7).

    Pushed from :class:`NoteViewScreen` via ``h``. Each row is one version
    (Date | Version | Op, mirroring :class:`BrowseScreen`'s own column style);
    selecting one pushes :class:`VersionViewScreen`, a read-only view of that
    exact version's body -- deliberately every row, including the current head,
    rather than filtering it out: picking the head row just shows the same
    body :class:`NoteViewScreen` already displayed, which is harmless and
    avoids an off-by-one special case for no real benefit.

    Escape pops back to :class:`NoteViewScreen`, the same "one level at a time"
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
            table.add_row(row.created, f"v{row.seq}", row.op, key=row.version_id)
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


class BrowseScreen(Screen[None]):
    """Date | Version | Summary, newest-first, over every live note."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("e", "edit_selected", "Edit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id=TABLE_ID, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        table.add_columns("Date", "Version", "Summary")
        table.focus()

    def on_screen_resume(self) -> None:
        """(Re)load the rows every time this screen becomes visible.

        Textual fires this on the *initial* push too (after ``on_mount``),
        so this is the one place that populates the table -- ``on_mount``
        never needs its own copy. That single load path is what makes
        lode-0wj.6's edit flow correct: a ``NoteViewScreen``/``EditScreen``
        pushed on top and popped back leaves the previously-loaded rows
        stale (an edit's Version/Summary columns no longer match the
        just-written head), and this hook reloads them every time browse
        becomes the top screen again, not only on first mount.
        """
        self._reload_rows()

    def _reload_rows(self) -> None:
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        table.clear()
        for row in list_notes(self.app.db_path):
            table.add_row(row.created, f"v{row.version}", row.summary, key=row.note_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        note_id = event.row_key.value
        if note_id is not None:
            self.app.push_screen(NoteViewScreen(note_id))

    def action_edit_selected(self) -> None:
        """``e``: open the highlighted row's note directly into an edit buffer."""
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        if table.row_count == 0:
            return
        note_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if note_id is not None:
            self.app.push_screen(EditScreen(note_id))

    def action_dismiss_screen(self) -> None:
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
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Back"),
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
        yield TextArea(id=EDIT_BODY_ID)
        yield Footer()

    def on_mount(self) -> None:
        head = load_head(self.app.db_path, self.note_id)
        if head is None:
            raise LookupError(f"no live note {self.note_id!r} to edit")
        self._loaded_head, self._loaded_body = head
        text_area = self.query_one(f"#{EDIT_BODY_ID}", TextArea)
        text_area.text = self._loaded_body
        text_area.focus()

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
