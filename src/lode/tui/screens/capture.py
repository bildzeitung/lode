"""The instant capture screen (lode-mkc.1) — get in, dump text, get out.

``docs/design.md`` §1/§2: "No AI in the capture path" and explicitly not
autocomplete / "improve my note" / chat-to-add — this screen is one text area
and two keys. Saving delegates entirely to
:func:`lode.tui.services.capture.save_capture`, which drives the same
``Repository.save`` + synchronous-FTS5-only cache seam ``lode add`` uses but
never runs the CLI's opportunistic immediate-enrich call, so no AI call can
land in this screen's save path. This screen owns no persistence logic of its
own — it only reads the text area, calls :func:`~lode.tui.services.capture.save_capture`,
and reacts to the result. A CAS reject (see :class:`~lode.tui.services.capture.CaptureConflict`)
is handed straight to :class:`~lode.tui.screens.reconcile.ReconcileScreen`
(lode-mkc.4) rather than handled here — this screen's job ends at "the save
was rejected," the reconcile screen's job is the diff and the resolution.

**Passive connection surfacing (lode-mkc.3, ``docs/design.md`` §2 "Surfacing
connections").** While the user types, an idle-debounced background pass
(:func:`lode.tui.services.related.find_related_notes`) surfaces related past notes into
a small panel below the text area — "you wrote about this 3 weeks ago". This
stays out of the save path entirely (it never touches ``save_capture``) and
runs off the UI thread via a Textual worker, so a slow or in-flight pass never
blocks typing or Ctrl+S/Escape.

**Confirm-on-unsaved guard for Escape (lode-0wj.1) and app-level Ctrl+Q
(lode-0wj.8).** Escape used to discard silently regardless of buffer state —
an easy vi-muscle-memory footgun. Now Escape on a non-empty/non-whitespace
buffer pops :class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`
(Save/Discard/Cancel) instead of exiting straight away; an empty/whitespace
buffer still exits immediately, so
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
real wiring never passed one, so :func:`~lode.tui.services.related.find_related_notes`
built a *fresh* :class:`~lode.embedding.FastEmbedEmbedder` every debounce fire
-- and unlike ONNX *inference* (which releases the GIL, per the spike), the
ONNX model's *construction* does not: measured against the lode-5y8.4 seed
corpus with the real embedder (the same diagnostic harness the spike used,
but driving the real screen through Textual's pilot rather than an isolated
function call), this cost the event loop ~1.5s of stall on *every* pause in
typing, for the whole session -- exactly the felt "typing blocks" complaint.
The shared embedder now lives in :class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel` (below) and is reused for the panel's lifetime, so only the
*first* pass in a session pays the ONNX model's cold-load cost; every pass
after it reuses the already-loaded model and pays only the single-digit-ms
inference cost lode-0wj.2's spike measured.

**Extracted into a shared widget (lode-aoc).** The debounce timer, the
``@work(exclusive=True)`` search pass, the lazy shared embedder, cancel-in-
flight logic, and the panel's own rendering all now live in
:class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel`, not this screen --
see that module's docstring for why (composition over a shared base class,
decided 2026-07-09) and for the full behaviour-contract list this extraction
preserved. This screen now only composes the widget, forwards its text area's
``Changed`` text to :meth:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel.update_draft`, and calls :meth:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel.reset` wherever it used to cancel/clear a pass of its own
(:meth:`CaptureScreen.action_save`). :class:`~lode.tui.screens.edit.EditScreen` composes the same widget for parity (the ticket this extraction
was done for).

**One stack-aware Ctrl+S; Ctrl+N retired (lode-bsmc).** This screen used to
bind its own "Save & quit" to Ctrl+S and a separate "Save & new" to Ctrl+N
(lode-d32.4). Both saved through the identical no-AI path
(:meth:`_save_buffer`); the only difference was what happened after: exit, or
reset-and-stay. Consolidated onto one stack-aware Ctrl+S -- this screen is
always the bottom of the stack (a brand-new note, nothing pushed on top), so
its Ctrl+S is unconditionally "Save & New" now (:meth:`action_save`, the
renamed former ``action_save_and_new``); :class:`~lode.tui.screens.edit.EditScreen`'s Ctrl+S (a screen *is* on the stack there) was already "Save &
pop" and is unchanged. "Save & quit" survives only inside the quit/discard
confirm dialog's own "Save" answer, decoupled onto its own exit path
(:meth:`_save_and_exit`) rather than reusing :meth:`action_save`, precisely
because that method no longer exits. ``ctrl+n`` is freed --
``docs/keybindings.md``'s letter-space accounting.

**...and reclaimed for open-link (lode-5ill)** -- a different action on the same
letter, so the retirement above is history, not a free key. Rationale lives on
:meth:`action_open_link`; ``docs/editing.md`` carries the design record.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Header, TextArea

from lode.tui.latency_probe import probe_event_loop_lag
from lode.tui.screens._link_open import open_link_under_cursor
from lode.tui.screens._markdown_area import _markdown_text_area
from lode.tui.screens.discard_confirm import DiscardConfirmScreen
from lode.tui.screens.reconcile import ReconcileScreen
from lode.tui.services.capture import CaptureConflict, EmptyCaptureError, save_capture
from lode.tui.widgets.lode_footer import LodeFooter
from lode.tui.widgets.related_notes_panel import RelatedNotesPanel

if TYPE_CHECKING:
    from lode.versions import SaveResult

log = logging.getLogger(__name__)

#: The text area's widget id — read back in tests and by this screen alike.
BODY_ID = "capture-body"

#: The passive "related past notes" panel's widget id (lode-mkc.3) — read back
#: in tests.
RELATED_ID = "related-notes"


class CaptureScreen(Screen[None]):
    """One text area plus a passive related-notes panel.

    Ctrl+S saves via the no-AI path, then resets the buffer for a fresh note
    instead of exiting, so a second (or third...) note never requires leaving
    the TUI (lode-d32.4's "Save & new", folded onto Ctrl+S by lode-bsmc --
    this screen is always the bottom of the stack, so its Ctrl+S is
    unconditionally "Save & New"; see :meth:`action_save`). There is no more
    "Save & quit" here -- that survives only inside the quit/discard confirm
    dialog's own "Save" answer (:meth:`_save_and_exit`). Escape discards and
    exits immediately if the buffer is empty/whitespace-only; otherwise it
    pops a Save/Discard/Cancel confirm (lode-0wj.1) rather than discarding
    silently. The app-level Ctrl+Q binding (:mod:`lode.tui.app`) applies the
    same guard via :meth:`confirm_quit` (lode-0wj.8). The related-notes panel
    stays passive by default while the body holds focus, but is itself
    interactive (lode-olmi.9) — Ctrl+F moves focus onto it to step through
    results and open one's highlighted context; see
    :mod:`lode.tui.widgets.related_notes_panel`'s module docstring. Ctrl+N
    opens the URL under the cursor (lode-5ill), matching the same binding on
    :class:`~lode.tui.screens.edit.EditScreen` and the two read-only version
    viewers; see :meth:`action_open_link`.
    """

    # Descriptions kept short (lode-3rvw), unchanged by the LodeFooter
    # extraction (lode-uczx): show=False stays ruled out, only description
    # text is ever shortened -- see :mod:`lode.tui.widgets.lode_footer` for why a
    # shared footer widget exists instead of per-screen compact/palette
    # flags.
    #
    # ctrl+s deliberately keeps its full "Save & new" (lode-3rvw review,
    # carried over from the retired ctrl+n binding by lode-bsmc): the
    # shortened "Save" alone no longer names what happens next -- the buffer
    # stays and resets, it doesn't exit -- so spelling out "& new" is the
    # discoverability cost show=False was ruled out for. Only ONE of "Save &
    # new" and "Discard & quit" can be spelled out in full without the other
    # -- Escape keeps the short "Discard" because it still names its
    # destructive half honestly, and this screen's labels are otherwise
    # unchanged by lode-uczx's 100-column bound (docs/tui.md): they already
    # fit comfortably within it. Adding "Link" (lode-5ill) still fits: the
    # footer's real consumed width test (test_tui_app.py) measured it well
    # under the 100-column bound with all 9 entries (4 screen + 5 app) shown.
    BINDINGS: ClassVar = [
        Binding("ctrl+s", "save", "Save & new"),
        Binding("escape", "cancel", "Discard"),
        Binding("ctrl+f", "focus_related", "Related"),
        Binding("ctrl+n", "open_link", "Link"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            _markdown_text_area(id=BODY_ID, placeholder="What did you learn today?"),
            # exclude_note_id=None: a brand-new capture has no note id yet to
            # exclude -- see lode.tui.widgets.related_notes_panel's module docstring.
            RelatedNotesPanel(id=RELATED_ID),
        )
        yield LodeFooter()

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

        The single save path :meth:`action_save` (Ctrl+S, "Save & New") and
        :meth:`_save_and_exit` (the quit/discard confirm's "Save" answer,
        "Save & quit") share, so lode-d32.4's "identical no-AI save path" is
        structural rather than a promise two copies of the same eight lines
        have to keep (lode-bsmc kept that structural guarantee when the two
        callers' identities changed). Returning ``None`` means the caller
        must *not* treat the save as done: either the buffer was empty (refused
        with the same notify ``lode add`` gives) or the compare-and-swap was
        rejected, in which case the buffer is already preserved as a draft and
        :class:`~lode.tui.screens.reconcile.ReconcileScreen` now owns the diff.

        A capture-path CAS reject is practically unreachable —
        :func:`~lode.tui.services.capture.save_capture` mints a fresh ``uuid4`` per call,
        so there is nothing for the compare-and-swap to collide with. It is
        still narrowed here rather than assumed away, because ``save_capture``
        is *typed* to return it: one branch in one place is cheaper than either
        caller mistaking a :class:`~lode.tui.services.capture.CaptureConflict` for a
        successful save and reporting it as one (:meth:`action_save` would
        clear the buffer and announce "Saved."). This is the whole of the CAS
        handling — there
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
        """Ctrl+S: save instantly (no AI call), then reset for a fresh note.

        This screen is always the bottom of the stack -- a brand-new capture,
        nothing pushed on top -- so its stack-aware Ctrl+S contract (lode-bsmc)
        is unconditionally "Save & New": it never exits the app. (Contrast
        :class:`~lode.tui.screens.edit.EditScreen`, where a screen already *is*
        on the stack and Ctrl+S pops back to it instead.) Formerly Ctrl+N's
        ``action_save_and_new`` (lode-d32.4); renamed onto ``action_save`` when
        the two screens' Ctrl+S bindings were consolidated onto one save path,
        freeing Ctrl+N. "Save & quit" is no longer reachable from this binding
        at all -- it survives only inside the quit/discard confirm dialog's own
        "Save" answer, which calls :meth:`_save_and_exit` instead, decoupled
        from this method precisely because this method no longer exits.

        A clean save does not exit the app: it clears the text area and the
        related-notes panel and leaves focus in the editor, so the next note
        can start immediately without relaunching the TUI. Because nothing
        exits and nothing else changes on screen, an emptied buffer would
        otherwise be indistinguishable from a discard — hence the notify.

        The reset drops any scheduled *and* any in-flight related-notes pass
        first (:meth:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel.reset`),
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

    def _save_and_exit(self) -> None:
        """Save instantly (no AI call) and exit -- the quit-confirm "Save" path.

        Decoupled from :meth:`action_save` (lode-bsmc): once Ctrl+S became
        "Save & New" (stays in the app), reusing it here would have made the
        quit/discard confirm dialog's "Save & quit" answer silently save-and-
        *stay* instead, contradicting its own label and the user's evident
        intent (they asked to leave via Escape or Ctrl+Q). This is the only
        remaining save-and-exit path in the screen; :meth:`_on_discard_confirm`
        is its sole caller.
        """
        result = self._save_buffer()
        if result is None:
            return
        self.app.exit(result.note_id)

    def action_cancel(self) -> None:
        """Escape: exit immediately if the buffer is empty, else confirm first."""
        self.confirm_quit()

    def action_focus_related(self) -> None:
        """Ctrl+F: move focus onto the related-notes panel (lode-olmi.9).

        Its own Up/Down/Enter bindings only fire while it holds focus (see
        :mod:`lode.tui.widgets.related_notes_panel`'s module docstring) — the body
        ``TextArea`` consumes those keys itself while typing, so this is the
        only way to reach them.
        """
        self.query_one(RelatedNotesPanel).focus()

    def action_open_link(self) -> None:
        """Ctrl+N: open the URL under the cursor, or explain there isn't one (lode-5ill).

        Not bare ``o``/``l`` -- this screen's body ``TextArea`` is editable and
        consumes every printable keypress before a Screen-level binding ever
        fires (``docs/keybindings.md``'s hard rule). ``Ctrl+N`` matches the
        identical binding on :class:`~lode.tui.screens.edit.EditScreen`,
        :class:`~lode.tui.screens.version_view.VersionViewScreen`, and
        :class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` --
        one key, every screen that can show a link.
        :func:`~lode.tui.screens._link_open.open_link_under_cursor` does the
        actual extraction + browser-safety work, shared with all three.
        """
        text_area = self.query_one(f"#{BODY_ID}", TextArea)
        open_link_under_cursor(self, text_area)

    def confirm_quit(self) -> None:
        """Exit immediately if the buffer is empty, else confirm first.

        Shared by Escape (:meth:`action_cancel`) and the app-level Ctrl+Q
        (``LodeApp.action_quit``, lode-0wj.8) — both want the identical
        "nothing to lose, just go" / "ask first" split, so this is the one
        place that owns it. An empty/whitespace-only buffer has nothing to
        lose, so it keeps the old "discard and exit" behaviour unprompted —
        the fast path a genuine empty capture (opened by mistake, or just
        backed out of) still wants. A non-empty buffer instead pops
        :class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`; its
        answer is handled by :meth:`_on_discard_confirm`.
        """
        body = self.query_one(f"#{BODY_ID}", TextArea).text
        if not body.strip():
            self.app.exit()
            return
        self.app.push_screen(DiscardConfirmScreen(), self._on_discard_confirm)

    def _on_discard_confirm(self, choice: str) -> None:
        """Act on the confirm dialog's answer: save, discard, or resume editing."""
        if choice == "save":
            self._save_and_exit()
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
        :class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel` — see that
        module's docstring.
        """
        if event.text_area.id != BODY_ID:
            return
        self.query_one(RelatedNotesPanel).update_draft(event.text_area.text)
