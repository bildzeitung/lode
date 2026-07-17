"""The instant capture screen (lode-mkc.1) — get in, dump text, get out.

``docs/design.md`` §1/§2: "No AI in the capture path" and explicitly not
autocomplete / "improve my note" / chat-to-add — this screen is one text area
and two keys. Saving delegates entirely to
:func:`lode.tui.capture.save_capture`, which drives the same
``Repository.save`` + synchronous-FTS5-only cache seam ``lode add`` uses but
never runs the CLI's opportunistic immediate-enrich call, so no AI call can
land in this screen's save path. This screen owns no persistence logic of its
own — it only reads the text area, calls :func:`~lode.tui.capture.save_capture`,
and reacts to the result. A CAS reject (see :class:`~lode.tui.capture.CaptureConflict`)
is handed straight to :class:`~lode.tui.screens.reconcile.ReconcileScreen`
(lode-mkc.4) rather than handled here — this screen's job ends at "the save
was rejected," the reconcile screen's job is the diff and the resolution.

**Passive connection surfacing (lode-mkc.3, ``docs/design.md`` §2 "Surfacing
connections").** While the user types, an idle-debounced background pass
(:func:`lode.tui.related.find_related_notes`) surfaces related past notes into
a small panel below the text area — "you wrote about this 3 weeks ago". This
stays out of the save path entirely (it never touches ``save_capture``) and
runs off the UI thread via a Textual worker, so a slow or in-flight pass never
blocks typing or Ctrl+S/Escape.

**Confirm-on-unsaved guard for Escape (lode-0wj.1) and app-level Ctrl+Q
(lode-0wj.8).** Escape used to discard silently regardless of buffer state —
an easy vi-muscle-memory footgun. Now Escape on a non-empty/non-whitespace
buffer pops :class:`DiscardConfirmScreen` (Save/Discard/Cancel) instead of
exiting straight away; an empty/whitespace buffer still exits immediately, so
the fast "get in, dump, get out" path for a genuinely empty capture is
untouched. Ctrl+S is unaffected either way. Ctrl+Q is a *global*
``App``-priority binding (:mod:`lode.tui.app`) that this screen can't bind
directly, so it reaches the same guard through :meth:`CaptureScreen.confirm_quit`
— the dirty-check-then-confirm logic :meth:`action_cancel` uses for Escape,
pulled out into its own method so ``LodeApp.action_quit`` can call it
generically without knowing anything about capture's buffer. A screen with
nothing unsaved simply doesn't define ``confirm_quit``, so the app quits it
immediately.

**Lag-diagnosis instrumentation (lode-0wj.2, SPIKE).** Feedback that typing
feels laggy prompted lightweight, toggleable logging around this screen's
input path and the passive related-notes pass: when the debounce timer
(re)starts and fires, each pass's sequence number / duration / result count
(and whether it was cancelled by a newer pass -- ``exclusive=True`` below
makes overlap structurally impossible, it just supersedes), and, only while
DEBUG logging is on, an event-loop-lag heartbeat
(:func:`lode.tui.latency_probe.probe_event_loop_lag`) that is this spike's
keystroke->render latency proxy. All of it is gated behind
``log.isEnabledFor(logging.DEBUG)`` (``LODE_LOG_LEVEL=DEBUG``,
:mod:`lode.logconfig`) -- zero log calls and no extra worker at the default
``INFO`` level, so this changes no behaviour. See
``tests/test_capture_lag_diagnosis.py`` for the offline reproduction against
the lode-5y8.4 seed corpus and the measured verdict.

**Latency fix: reuse one embedder instead of one per pass (lode-0wj.4).** The
lag-diagnosis spike (lode-0wj.2) found the related-notes pass itself
non-blocking -- but only tested it with a *pre-warmed* embedder. This screen's
real wiring never passed one, so :func:`~lode.tui.related.find_related_notes`
built a *fresh* :class:`~lode.embedding.FastEmbedEmbedder` every debounce fire
-- and unlike ONNX *inference* (which releases the GIL, per the spike), the
ONNX model's *construction* does not: measured against the lode-5y8.4 seed
corpus with the real embedder (the same diagnostic harness the spike used,
but driving the real screen through Textual's pilot rather than an isolated
function call), this cost the event loop ~1.5s of stall on *every* pause in
typing, for the whole session -- exactly the felt "typing blocks" complaint.
The shared embedder now lives in :class:`~lode.tui.related_notes_panel.
RelatedNotesPanel` (below) and is reused for the panel's lifetime, so only the
*first* pass in a session pays the ONNX model's cold-load cost; every pass
after it reuses the already-loaded model and pays only the single-digit-ms
inference cost lode-0wj.2's spike measured.

**Extracted into a shared widget (lode-aoc).** The debounce timer, the
``@work(exclusive=True)`` search pass, the lazy shared embedder, cancel-in-
flight logic, and the panel's own rendering all now live in
:class:`~lode.tui.related_notes_panel.RelatedNotesPanel`, not this screen --
see that module's docstring for why (composition over a shared base class,
decided 2026-07-09) and for the full behaviour-contract list this extraction
preserved. This screen now only composes the widget, forwards its text area's
``Changed`` text to :meth:`~lode.tui.related_notes_panel.RelatedNotesPanel.
update_draft`, and calls :meth:`~lode.tui.related_notes_panel.
RelatedNotesPanel.reset` wherever it used to cancel/clear a pass of its own
(:meth:`CaptureScreen.action_save_and_new`). :class:`~lode.tui.screens.browse.
EditScreen` composes the same widget for parity (the ticket this extraction
was done for).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Static, TextArea

from lode.tui.capture import CaptureConflict, EmptyCaptureError, save_capture
from lode.tui.latency_probe import probe_event_loop_lag
from lode.tui.related_notes_panel import RelatedNotesPanel
from lode.tui.screens.reconcile import ReconcileScreen

if TYPE_CHECKING:
    from lode.versions import SaveResult

log = logging.getLogger(__name__)

#: The text area's widget id — read back in tests and by this screen alike.
BODY_ID = "capture-body"

#: The passive "related past notes" panel's widget id (lode-mkc.3) — read back
#: in tests.
RELATED_ID = "related-notes"

#: The confirm dialog's message widget id (lode-0wj.1) — read back in tests.
CONFIRM_MESSAGE_ID = "capture-confirm-message"


class DiscardConfirmScreen(ModalScreen[str]):
    """Save / Discard / Cancel confirm, popped on Escape over a dirty buffer.

    Dismisses with one of ``"save"``, ``"discard"``, ``"cancel"`` — the caller
    (:meth:`CaptureScreen.confirm_quit`, reached from both Escape and the
    app-level Ctrl+Q) decides what each means; this screen owns only the
    prompt and the three keys.

    **Popup styling (lode-1i8.4).** Pushed via ``push_screen`` (not
    ``switch_screen``), so :class:`CaptureScreen` stays mounted underneath on
    the app's screen stack rather than being replaced — this dialog is an
    overlay, not a navigation. :class:`~textual.screen.ModalScreen`'s own
    ``DEFAULT_CSS`` already dims that screen underneath
    (``background: $background 60%``); ``lode.tcss`` (:mod:`lode.tui`, loaded
    via ``LodeApp.CSS_PATH``) adds only what was missing — centering and
    sizing the ``#capture-confirm-dialog`` box itself — so the prompt reads
    as a bounded, bordered popup over the still-visible editor instead of a
    blank full screen.
    """

    BINDINGS = [
        Binding("s", "choose('save')", "Save & quit"),
        Binding("d", "choose('discard')", "Discard & quit"),
        Binding("c", "choose('cancel')", "Cancel"),
        Binding("escape", "choose('cancel')", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                "Unsaved note. (S)ave, (D)iscard, or (C)ancel?",
                id=CONFIRM_MESSAGE_ID,
            ),
            id="capture-confirm-dialog",
        )

    def action_choose(self, choice: str) -> None:
        self.dismiss(choice)


class CaptureScreen(Screen[None]):
    """One text area plus a passive related-notes panel.

    Ctrl+S saves and exits. Ctrl+N saves the same way but resets the buffer
    for a fresh note instead of exiting, so a second (or third...) note never
    requires leaving the TUI (lode-d32.4, :meth:`action_save_and_new`).
    Escape discards and exits immediately if the buffer is empty/whitespace-
    only; otherwise it pops a Save/Discard/Cancel confirm (lode-0wj.1) rather
    than discarding silently. The app-level Ctrl+Q binding
    (:mod:`lode.tui.app`) applies the same guard via :meth:`confirm_quit`
    (lode-0wj.8). The related-notes panel stays passive by default while the
    body holds focus, but is itself interactive (lode-olmi.9) — Ctrl+F moves
    focus onto it to step through results and open one's highlighted context;
    see :mod:`lode.tui.related_notes_panel`'s module docstring.
    """

    # Descriptions kept short (lode-3rvw) -- same lever and same rationale as
    # BrowseScreen's lode-l38d.3 fix, which spells both out (see
    # lode.tui.screens.browse): show=False stays ruled out, only the
    # description text is shortened. At full length these 4 plus the 4
    # App-level bindings (LodeApp.BINDINGS) really consumed 100 columns.
    #
    # ctrl+n deliberately keeps its full "Save & new" (lode-3rvw review): the
    # shortened "New" sits next to "Save" and reads as "start a new note"
    # WITHOUT saving -- the opposite of what it does, which is the same
    # discoverability cost show=False was ruled out for. Only ONE of the two
    # long labels fits: "Save & new" alone lands at 77/80, but restoring
    # "Discard & quit" alongside it clips again at 84/80. Escape keeps the
    # short form because "Discard" still names its destructive half honestly.
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+n", "save_and_new", "Save & new"),
        Binding("escape", "cancel", "Discard"),
        Binding("ctrl+f", "focus_related", "Related"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            TextArea(id=BODY_ID, placeholder="What did you learn today?"),
            # exclude_note_id=None: a brand-new capture has no note id yet to
            # exclude -- see lode.tui.related_notes_panel's module docstring.
            RelatedNotesPanel(id=RELATED_ID),
        )
        # Still the stock Footer, just asked for less padding (lode-3rvw) --
        # the same two levers as BrowseScreen's lode-l38d.3 fix, which
        # documents their mechanics (see lode.tui.screens.browse). Measured at
        # 80x24: 77/80 with every binding visible.
        #
        # BOTH levers are load-bearing -- do not "simplify" either away: with
        # these labels, dropping compact=True needs 93 columns and dropping
        # show_command_palette=False needs 89. Measure with sum(FooterKey
        # widths) + (N-1) gutters, never the right edge or
        # show_horizontal_scrollbar alone -- both under-report a small overflow
        # (tests/test_tui_app.py documents that trap).
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one(f"#{BODY_ID}", TextArea).focus()
        # lode-0wj.2: the event-loop-lag heartbeat only ever runs while DEBUG
        # logging is on -- gating the *start* (not just the log calls inside it)
        # means the default INFO level spawns no extra worker at all.
        if log.isEnabledFor(logging.DEBUG):
            self._probe_loop_lag()

    @work(group="latency-probe")
    async def _probe_loop_lag(self) -> None:
        """DEBUG-only: run the event-loop-lag heartbeat for this screen's lifetime.

        Textual cancels a screen's workers on unmount, which is this loop's only
        exit (:func:`~lode.tui.latency_probe.probe_event_loop_lag` never returns).
        """
        await probe_event_loop_lag()

    def _save_buffer(self) -> SaveResult | None:
        """Save the buffer instantly (no AI call); ``None`` if nothing was saved.

        The single save path Ctrl+S (:meth:`action_save`) and Ctrl+N
        (:meth:`action_save_and_new`) share, so lode-d32.4's "identical no-AI
        save path as Ctrl+S" is structural rather than a promise two copies of
        the same eight lines have to keep. Returning ``None`` means the caller
        must *not* treat the save as done: either the buffer was empty (refused
        with the same notify ``lode add`` gives) or the compare-and-swap was
        rejected, in which case the buffer is already preserved as a draft and
        :class:`~lode.tui.screens.reconcile.ReconcileScreen` now owns the diff.

        A capture-path CAS reject is practically unreachable —
        :func:`~lode.tui.capture.save_capture` mints a fresh ``uuid4`` per call,
        so there is nothing for the compare-and-swap to collide with. It is
        still narrowed here rather than assumed away, because ``save_capture``
        is *typed* to return it: one branch in one place is cheaper than either
        caller mistaking a :class:`~lode.tui.capture.CaptureConflict` for a
        successful save and reporting it as one (Ctrl+N would clear the buffer
        and announce "Saved."). This is the whole of the CAS handling — there
        is deliberately no reconcile-then-continue flow.
        """
        body = self.query_one(f"#{BODY_ID}", TextArea).text
        app = self.app
        try:
            result = save_capture(app.db_path, body, settings=app.settings)
        except EmptyCaptureError:
            self.notify("Refusing to save an empty note.", severity="warning")
            return None
        if isinstance(result, CaptureConflict):
            self.app.push_screen(ReconcileScreen(result))
            return None
        return result

    def action_save(self) -> None:
        """Ctrl+S: save the buffer instantly (no AI call) and exit, or explain why not."""
        result = self._save_buffer()
        if result is None:
            return
        self.app.exit(result.note_id)

    def action_save_and_new(self) -> None:
        """Ctrl+N: save exactly like Ctrl+S, then reset for a fresh note (lode-d32.4).

        A clean save does not exit the app: it clears the text area and the
        related-notes panel and leaves focus in the editor, so the next note
        can start immediately without relaunching the TUI. Because nothing
        exits and nothing else changes on screen, an emptied buffer would
        otherwise be indistinguishable from a discard — hence the notify.

        The reset drops any scheduled *and* any in-flight related-notes pass
        first (:meth:`~lode.tui.related_notes_panel.RelatedNotesPanel.reset`),
        so a slow pass started for the just-saved note cannot land afterwards
        and paint its results into the freshly-cleared panel. A refused
        (empty) or CAS-rejected save resets nothing: ``_save_buffer`` returned
        ``None`` and the buffer stands.
        """
        if self._save_buffer() is None:
            return

        self.query_one(RelatedNotesPanel).reset()
        text_area = self.query_one(f"#{BODY_ID}", TextArea)
        text_area.clear()
        text_area.focus()
        self.notify("Saved. New note.")

    def action_cancel(self) -> None:
        """Escape: exit immediately if the buffer is empty, else confirm first."""
        self.confirm_quit()

    def action_focus_related(self) -> None:
        """Ctrl+F: move focus onto the related-notes panel (lode-olmi.9).

        Its own Up/Down/Enter bindings only fire while it holds focus (see
        :mod:`lode.tui.related_notes_panel`'s module docstring) — the body
        ``TextArea`` consumes those keys itself while typing, so this is the
        only way to reach them.
        """
        self.query_one(RelatedNotesPanel).focus()

    def confirm_quit(self) -> None:
        """Exit immediately if the buffer is empty, else confirm first.

        Shared by Escape (:meth:`action_cancel`) and the app-level Ctrl+Q
        (``LodeApp.action_quit``, lode-0wj.8) — both want the identical
        "nothing to lose, just go" / "ask first" split, so this is the one
        place that owns it. An empty/whitespace-only buffer has nothing to
        lose, so it keeps the old "discard and exit" behaviour unprompted —
        the fast path a genuine empty capture (opened by mistake, or just
        backed out of) still wants. A non-empty buffer instead pops
        :class:`DiscardConfirmScreen`; its answer is handled by
        :meth:`_on_discard_confirm`.
        """
        body = self.query_one(f"#{BODY_ID}", TextArea).text
        if not body.strip():
            self.app.exit()
            return
        self.app.push_screen(DiscardConfirmScreen(), self._on_discard_confirm)

    def _on_discard_confirm(self, choice: str) -> None:
        """Act on the confirm dialog's answer: save, discard, or resume editing."""
        if choice == "save":
            self.action_save()
        elif choice == "discard":
            self.app.exit()
        # "cancel" (or the dialog dismissing with no answer): stay right here,
        # buffer untouched.

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Forward the body's text to the related-notes panel (lode-mkc.3, lode-aoc).

        Guarded to the capture body's own id so a future widget's ``Changed``
        message (bubbling through the same handler name) can never
        mis-trigger this. All of the debounce/search/cancel-in-flight
        machinery this used to own directly now lives in
        :class:`~lode.tui.related_notes_panel.RelatedNotesPanel` — see that
        module's docstring.
        """
        if event.text_area.id != BODY_ID:
            return
        self.query_one(RelatedNotesPanel).update_draft(event.text_area.text)
