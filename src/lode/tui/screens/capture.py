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
:meth:`_ensure_embedder` now constructs one :class:`~lode.embedding.Embedder`
and reuses it for this screen's lifetime, so only the *first* pass in a
session pays the ONNX model's cold-load cost; every pass after it reuses the
already-loaded model and pays only the single-digit-ms inference cost
lode-0wj.2's spike measured. (Deliberately not warmed any earlier than that
first real pass -- see :meth:`_ensure_embedder`'s docstring for why eagerly
warming at mount time backfired.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Footer, Header, Static, TextArea

from lode.tui.capture import CaptureConflict, EmptyCaptureError, save_capture
from lode.tui.latency_probe import probe_event_loop_lag
from lode.tui.screens.reconcile import ReconcileScreen

if TYPE_CHECKING:
    # Type-only; the runtime imports live inside _ensure_embedder /
    # _search_related so this screen's own import stays free of the vector
    # stack (pyarrow) and the embedder (fastembed) until a passive-surfacing
    # pass actually runs.
    from lode.embedding import Embedder
    from lode.tui.related import RelatedNote

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

    Ctrl+S saves and exits. Escape discards and exits immediately if the
    buffer is empty/whitespace-only; otherwise it pops a Save/Discard/Cancel
    confirm (lode-0wj.1) rather than discarding silently. The app-level
    Ctrl+Q binding (:mod:`lode.tui.app`) applies the same guard via
    :meth:`confirm_quit` (lode-0wj.8). The related-notes panel is read-only
    and non-interactive — it never takes focus or input, so it changes
    nothing about capture's "get in, dump text, get out" contract.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save & quit"),
        Binding("escape", "cancel", "Discard & quit"),
    ]

    def __init__(
        self,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        #: The pending debounce timer for a passive surfacing pass, restarted
        #: on every keystroke; ``None`` when no pass is scheduled.
        self._related_timer: Timer | None = None
        #: The most recently rendered related-notes result, kept as plain
        #: screen state (not just Static markup) so it is a stable, direct
        #: assertion surface for tests rather than parsed back out of the
        #: rendered widget.
        self._related: list[RelatedNote] = []
        #: Incrementing id for each related-notes pass (lode-0wj.2 instrumentation) --
        #: lets the DEBUG log correlate a pass's start/finish/cancellation lines.
        self._related_pass_seq = 0
        #: The shared query embedder for the related-notes pass, constructed
        #: once and reused for this screen's lifetime (lode-0wj.4) rather than
        #: a fresh instance per pass -- see :meth:`_ensure_embedder`.
        self._embedder: Embedder | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            TextArea(id=BODY_ID, placeholder="What did you learn today?"),
            # markup=False: snippets are verbatim user note text and
            # commonly contain bracket sequences (list[0], [link](url),
            # log [ERROR], footnote [1]) that Textual would otherwise parse
            # as console markup and raise MarkupError on (lode-mkc.3).
            Static("", id=RELATED_ID, markup=False),
        )
        yield Footer()

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

    def _ensure_embedder(self) -> Embedder:
        """Return the shared query embedder, constructing the wrapper on first use.

        Cheap and synchronous: :class:`~lode.embedding.FastEmbedEmbedder`'s
        ``__init__`` only stashes the model name -- the actual (expensive) ONNX
        model load stays lazy inside it (guarded by a lock there against
        concurrent callers) until the first :meth:`_search_related` pass
        actually embeds something (lode-0wj.4). Deliberately **not** warmed
        eagerly at mount: every ``CaptureScreen`` instantiation (including in
        plain unit tests that never let the debounce fire) would otherwise pay
        a real ONNX model load unconditionally, which is exactly the kind of
        surprise cost :class:`~lode.embedding.FastEmbedEmbedder`'s lazy-by-design
        docstring exists to avoid -- and, in one test, made a real background
        load run long enough in wall-clock time for an unrelated debounce timer
        to fire during a should-be-instant discard. The fix scoped here is
        reuse across passes, not moving *when* the unavoidable first load
        happens.
        """
        if self._embedder is None:
            from lode.embedding import FastEmbedEmbedder

            self._embedder = FastEmbedEmbedder(self.app.settings)
        return self._embedder

    def action_save(self) -> None:
        """Save the buffer instantly (no AI call) and exit, or explain why not."""
        body = self.query_one(f"#{BODY_ID}", TextArea).text
        app = self.app
        try:
            result = save_capture(app.db_path, body, settings=app.settings)
        except EmptyCaptureError:
            self.notify("Refusing to save an empty note.", severity="warning")
            return
        if isinstance(result, CaptureConflict):
            self.app.push_screen(ReconcileScreen(result))
            return
        self.app.exit(result.note_id)

    def action_cancel(self) -> None:
        """Escape: exit immediately if the buffer is empty, else confirm first."""
        self.confirm_quit()

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
        """Debounce a passive connection-surfacing pass (lode-mkc.3).

        Every keystroke restarts the idle timer
        (``Settings.related_notes_debounce_ms``) rather than searching inline,
        so a burst of typing triggers at most one pass per idle pause — half
        of the acceptance criterion's "passive, non-blocking" (the other half,
        keeping the pass itself off the UI thread, is
        :meth:`_search_related`'s job). Guarded to the capture body's own id
        so a future widget's ``Changed`` message (bubbling through the same
        handler name) can never mis-trigger this.
        """
        if event.text_area.id != BODY_ID:
            return
        if self._related_timer is not None:
            self._related_timer.stop()
        delay_s = self.app.settings.related_notes_debounce_ms / 1000
        log.debug(
            "keystroke: related-notes debounce (re)started, delay=%.0fms",
            delay_s * 1000,
        )
        self._related_timer = self.set_timer(delay_s, self._start_related_search)

    def _start_related_search(self) -> None:
        """Timer callback: read the current buffer and kick off the search worker."""
        log.debug("related-notes debounce fired: starting a pass")
        body = self.query_one(f"#{BODY_ID}", TextArea).text
        self._search_related(body)

    @work(exclusive=True, group="related-notes")
    async def _search_related(self, body: str) -> None:
        """Run the retrieval/graph pipeline off the UI thread, then render it.

        ``find_related_notes`` (:mod:`lode.tui.related`) does real DB + local-
        model work (FTS5, LanceDB, the ONNX embedder); ``asyncio.to_thread``
        keeps it off the event loop so typing and Ctrl+S/Escape are never
        blocked on it. ``exclusive=True`` (same worker group each call)
        cancels any still-running prior pass before starting this one, so a
        fast typist never sees results arrive out of order.

        **lode-0wj.2 instrumentation:** logs this pass's sequence number, wall-clock
        duration and result count at DEBUG (how often it fires and how long
        ``find_related_notes`` -- FTS5 + the ONNX embedder + LanceDB -- actually
        takes), and whether it got cancelled by a newer pass before finishing
        (``exclusive=True`` makes true overlap structurally impossible; this just
        makes that supersession visible instead of silent).

        **lode-0wj.4:** passes :meth:`_ensure_embedder`'s shared embedder rather
        than leaving ``find_related_notes`` build its own -- see the module
        docstring for why a fresh instance per pass was the real felt-lag source.
        """
        from lode.tui.related import find_related_notes

        self._related_pass_seq += 1
        seq = self._related_pass_seq
        log.debug("related-notes pass #%d: starting (draft_len=%d)", seq, len(body))
        app = self.app
        embedder = self._ensure_embedder()
        start = time.monotonic()
        try:
            related = await asyncio.to_thread(
                find_related_notes,
                app.db_path,
                body,
                settings=app.settings,
                embedder=embedder,
            )
        except asyncio.CancelledError:
            elapsed_ms = (time.monotonic() - start) * 1000
            log.debug(
                "related-notes pass #%d: cancelled after %.1fms (superseded)",
                seq,
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.monotonic() - start) * 1000
        log.debug(
            "related-notes pass #%d: finished in %.1fms, %d related note(s)",
            seq,
            elapsed_ms,
            len(related),
        )
        self._render_related(related)

    def _render_related(self, related: list[RelatedNote]) -> None:
        """Render (or clear) the passive related-notes panel.

        ``self._related`` is kept as plain screen state alongside the
        rendered ``Static`` markup, so a test (or a future caller) has a
        stable, direct assertion surface rather than needing to parse
        results back out of rendered widget content.
        """
        self._related = related
        panel = self.query_one(f"#{RELATED_ID}", Static)
        if not related:
            panel.update("")
            return
        lines = [f"· {note.age} — {note.snippet}" for note in related]
        panel.update("Related notes:\n" + "\n".join(lines))
