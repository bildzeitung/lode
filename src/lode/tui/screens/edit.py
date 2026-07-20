"""An existing note's live head, loaded editable (lode-0wj.6, extracted lode-s5kp.1).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed from :meth:`~lode.tui.screens.browse.
BrowseScreen.on_data_table_row_selected` -- Enter, or a mouse click, on a row
(lode-olmi.2): editing an existing note *is* the point of opening it, so
there is no separate read-only detour first. Escape returns to
:class:`~lode.tui.screens.browse.BrowseScreen`.

Saving reparents the buffer onto the loaded head via the CAS path
(:func:`lode.tui.services.edit.save_edit`) -- a new *version* on this note's chain,
never a new note. Escape returns to the browse list, reusing capture's
Save/Discard/Cancel confirm
(:class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`, lode-0wj.1) --
but **this is the first screen where the dirty check can't be "is the buffer
non-empty"**: a freshly loaded existing version is non-empty by construction,
so that check would wrongly confirm on every Escape even with zero edits.
:meth:`EditScreen.action_cancel` instead compares the live buffer against the
body loaded at :meth:`EditScreen.on_mount` -- "changed since it was opened,"
the same standard the CAS layer itself uses for the head.

**App-level Ctrl+Q (lode-b14).** :meth:`EditScreen.confirm_quit` gives
``LodeApp.action_quit`` (lode-0wj.8's generic "ask the current screen" hook)
the same unchanged-vs-edited dirty check, but its own Save/Discard/Cancel
resolution ends in ``self.app.exit()`` / ``self.app.exit(note_id)`` -- never
``pop_screen`` -- matching Ctrl+Q's global "quit the whole app" contract
rather than Escape's "back to browse" one. The two can't share a method (see
:meth:`EditScreen.action_cancel`'s docstring); mirrors
:meth:`~lode.tui.screens.capture.CaptureScreen.confirm_quit`'s contract
exactly.

**Passive related-notes panel (lode-aoc).** Composes the same
:class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel` widget
:class:`~lode.tui.screens.capture.CaptureScreen` uses, for parity -- "you
wrote about this before" while editing, not just while capturing a
brand-new note. Constructed with ``exclude_note_id=self.note_id`` so the
note being edited never matches its own (near-identical) draft. Needs no
reset call of its own anywhere this screen exits: unlike capture's Ctrl+N
(which keeps the screen alive for a fresh note), every exit here either pops
or tears down this screen, and Textual cancels a screen's workers on
unmount -- the same guarantee the panel's own module docstring relies on.

**Row-select opens here directly; full id; version history (lode-olmi.2).**
Before this, row-select pushed a separate read-only note view first, and
this screen was only reached via a distinct ``e`` keypress -- both now
retired, since selecting a row *is* "I want to edit this note." Two things
that screen used to own move in along with it: the header's ``sub_title``
now shows the full 36-char ``note_id`` (:meth:`EditScreen.on_mount`, same as
the retired screen did -- selectable/copyable, unlike the Browse table's
8-char abbreviation, which has a width budget to protect), and ``Ctrl+H``
pushes :class:`~lode.tui.screens.version_history.VersionHistoryScreen` for
this note (:meth:`EditScreen.action_show_history`). ``Ctrl+H``, not bare
``h``: this screen's body ``TextArea`` is editable (unlike the retired note
view's), and Textual's ``TextArea`` consumes every ``is_printable``
keypress -- including a bare ``h`` -- before a Screen-level, non-priority
``Binding`` ever sees it (confirmed empirically -- a bare ``h`` binding here
would insert the literal letter into the note body instead of opening
history). ``Ctrl+S``/``Ctrl+N`` already use this same non-printable-key
escape hatch on this and capture's screen, for the identical reason.

**Enrichment inspector, Ctrl+G not bare ``i`` (lode-g5es).**
:meth:`EditScreen.action_inspect_selected` pushes the same
:class:`~lode.tui.screens.enrichment_modal.EnrichmentModalScreen`
:meth:`~lode.tui.screens.browse.BrowseScreen.action_inspect_selected` does,
keyed to ``self.note_id`` -- "was anything retrieved for this note,"
reachable while editing, not just from the browse row. It is bound to
``Ctrl+G``, not ``BrowseScreen``'s bare ``i``: this screen's body
``TextArea`` is editable, so a bare ``i`` would type a literal letter
instead of opening the modal, the identical trap ``Ctrl+H`` above exists to
dodge. Two more letters that look tempting fail for their own reasons:
``Ctrl+I`` is *not* a safe substitute for bare ``i`` -- terminals encode
Ctrl+I as the Tab control character, and Textual's ``KEY_ALIASES`` reflects
that (``"tab": ["ctrl+i"]``), so a ``ctrl+i`` binding is indistinguishable
from ``tab`` -- a non-printable navigation key -- and would be silently
unreachable too (confirmed empirically: pressing it with the body focused
neither opens the inspector nor types anything) -- and ``Ctrl+P`` (a
natural "peek" pick, matching this modal's own glance-and-dismiss contract)
collides with Textual's own App-level command-palette binding, which is
registered with ``priority=True`` and so wins over *any* Screen-level
binding on the same key, confirmed empirically (pressing it opened
``CommandPalette``, never the inspector). ``Ctrl+G`` ("glance") is free of
all three traps. This is project practice, not a one-off: every action this
screen binds beyond Escape uses a ``Ctrl+``-prefixed (or otherwise
non-printable) key for exactly this reason -- see ``docs/keybindings.md``.

**Content viewer, Ctrl+R not bare ``v`` (lode-0sjj).**
:meth:`EditScreen.action_view_content` resolves this note's external edges
the same way :meth:`~lode.tui.screens.browse.BrowseScreen.
action_view_content` does (via the shared
:func:`~lode.tui.screens._content_view._view_note_external_content`) and pushes
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` (zero/one) or
:class:`~lode.tui.screens.external_picker.ExternalPickerScreen` (many).
``BrowseScreen``'s binding for the identical feature is bare ``v`` -- safe
there because its focused widget is a ``DataTable``, not an editable
``TextArea``. Here the body is editable, so the literal ``v`` key
lode-olmi.8's design named would just type a letter, exactly the
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

**Import cycle, dissolved (lode-s5kp.1, lode-2zj0).**
:class:`~lode.tui.screens.browse.BrowseScreen` imports :class:`EditScreen`
from here to push it on row-select. Originally,
``_view_note_external_content`` lived in :mod:`lode.tui.screens.browse`
itself, so this module had to reach back into ``browse`` for it -- a
top-level import the other way would have formed a cycle, worked around
with a method-local import inside :meth:`EditScreen.action_view_content`.
That function (and its helper ``_resolve_externals``) has since moved to
its own leaf module, :mod:`lode.tui.screens._content_view`, which neither
``browse`` nor ``edit`` reach back from -- both import it at module level
now, and the cycle no longer exists.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Header, TextArea

from lode.tui.services.edit import EditConflict, EmptyEditError, load_head, save_edit
from lode.tui.screens._markdown_area import _markdown_text_area
from lode.tui.widgets.lode_footer import LodeFooter
from lode.tui.widgets.related_notes_panel import RelatedNotesPanel
from lode.tui.screens._content_view import _view_note_external_content
from lode.tui.screens.discard_confirm import DiscardConfirmScreen
from lode.tui.screens.enrichment_modal import EnrichmentModalScreen
from lode.tui.screens.reconcile import ReconcileScreen
from lode.tui.screens.version_history import VersionHistoryScreen
from lode.versions import SaveResult

#: The editable note body's widget id -- read back in tests.
EDIT_BODY_ID = "note-edit-body"
#: The edit screen's passive related-notes panel widget id (lode-aoc) -- read
#: back in tests.
EDIT_RELATED_ID = "edit-related-notes"


class EditScreen(Screen[None]):
    """An existing note's live head, loaded editable (lode-0wj.6)."""

    # "View content" -> "View" (lode-uczx): this screen is the tightest
    # footer of the ten (131 columns' worth of content at full length, the
    # only one that clipped even under the new 100-column bound). Every
    # other label here stays full -- this one shortening, plus the
    # App-level "Cfg" (:mod:`lode.tui.app`), is what buys this screen's fit
    # and the slack lode-11io's not-yet-landed Ask binding needs.
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Back"),
        Binding("ctrl+f", "focus_related", "Related"),
        Binding("ctrl+h", "show_history", "History"),
        Binding("ctrl+g", "inspect_selected", "Inspect"),
        Binding("ctrl+r", "view_content", "View"),
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
            _markdown_text_area(id=EDIT_BODY_ID),
            RelatedNotesPanel(exclude_note_id=self.note_id, id=EDIT_RELATED_ID),
        )
        yield LodeFooter()

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
        :mod:`lode.tui.widgets.related_notes_panel`'s module docstring) — the body
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

        Mirrors :meth:`~lode.tui.screens.browse.BrowseScreen.
        action_inspect_selected` -- same modal, same glance-and-dismiss
        contract, keyed to ``self.note_id`` directly (this screen always has
        exactly one note loaded, unlike Browse's table, which needs the
        highlighted row). See this class's docstring for why ``Ctrl+G``
        rather than bare ``i``, ``Ctrl+I``, or ``Ctrl+P``.
        """
        self.app.push_screen(EnrichmentModalScreen(self.note_id))

    def action_view_content(self) -> None:
        """Ctrl+R: view this note's retrieved external content, if any (lode-0sjj).

        Not bare ``v`` -- this screen's body ``TextArea`` is editable and
        consumes every printable keypress before a Screen-level binding ever
        fires, the identical trap ``Ctrl+H``/``Ctrl+G`` above exist to dodge
        (this class's docstring; ``docs/keybindings.md``). ``Ctrl+R``
        ("retrieved") is free of the same three traps checked there.
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
