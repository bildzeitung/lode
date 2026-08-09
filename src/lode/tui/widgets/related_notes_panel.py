"""A self-contained passive related-notes panel (lode-aoc).

``docs/design.md`` §2 supporting feature 2 ("you wrote about this 3 weeks
ago") originally landed wired directly into
:class:`~lode.tui.screens.capture.CaptureScreen` (lode-mkc.3). This module
extracts that machinery — the debounce timer, the ``@work(exclusive=True)``
search pass, the lazy shared query embedder, cancel-in-flight logic, and the
panel's own rendering — into a self-contained widget any screen can compose,
so :class:`~lode.tui.screens.edit.EditScreen` gets the same passive
surfacing capture already has without a copy-pasted ~100 lines.

**Composition, not inheritance (decided 2026-07-09, with user).** A common
``NoteEditorScreen`` superclass was explicitly rejected: capture's and edit's
surrounding lifecycle plumbing (dirty check, the discard-confirm dance,
reconcile resolution) is deliberately divergent, and a base class would need
several template-method hooks landing exactly on those divergences. This
widget instead owns its own timer/worker/embedder/rendering end to end;
a screen composes it and forwards its own ``TextArea.Changed`` text via
:meth:`RelatedNotesPanel.update_draft`, and calls :meth:`RelatedNotesPanel.reset`
wherever it used to cancel/clear the in-flight pass. Nothing about a screen's
save/dirty/reconcile flow needs to know this widget exists.

**Self-exclusion.** :class:`~lode.tui.screens.edit.EditScreen` constructs
this widget with ``exclude_note_id`` set to the note being edited — its own
freshly-typed draft would otherwise trivially match itself, since editing
does not change a note's id. :class:`~lode.tui.screens.capture.CaptureScreen`
passes ``None`` (a brand-new note has no id yet to exclude). The exclusion
itself is enforced by :func:`lode.tui.services.related.find_related_notes`'s own
``exclude_note_id`` parameter, not filtered here — this widget stays a thin
wiring layer over that pure function, same as capture's original wiring was.

**Behaviour contracts preserved from the extraction (lode-0wj.2, lode-0wj.4,
lode-mkc.3):** the query embedder is constructed lazily (no eager ONNX load
at mount — see :meth:`_ensure_embedder`) and reused for this widget's
lifetime; a scheduled *and* an in-flight pass are both cancelled on
:meth:`reset` or on an emptied draft, so a slow pass for an already-abandoned
draft cannot land afterwards and paint stale results; an empty/whitespace-only
draft clears the panel immediately without scheduling a pointless pass;
``markup=False`` on the underlying ``Static`` (a note's snippet is verbatim
user text and commonly contains bracket sequences Textual would otherwise
parse as console markup, lode-mkc.3).

**Interactive stepping + a highlighted-context modal (lode-olmi.9).** The panel is no
longer purely passive: it is now focusable (:attr:`can_focus`) and binds Up/Down to move
a selection cursor through :attr:`_related` and Enter to open
:class:`~lode.tui.screens.related_note_modal.RelatedNoteModalScreen` for the selected
note. These bindings only fire once the panel itself holds focus — Textual's stock
``TextArea`` already consumes bare Up/Down/Enter/Tab for cursor movement, newline
insertion, and indentation while *it* holds focus, so identical bindings on the panel
would be unreachable while the note body is being typed into. Each composing screen
(:class:`~lode.tui.screens.capture.CaptureScreen`,
:class:`~lode.tui.screens.edit.EditScreen`) therefore adds its own screen-level binding
that calls this panel's inherited ``focus()`` to move focus onto it deliberately;
nothing here changes what happens while the body still holds focus (the passive
debounce/render path above is untouched). Highlighted context is the exact matched
passage span — ``RelatedNote. char_range`` (:mod:`lode.tui.services.related`) offsets
into the exact ``version_id`` the retrieval pipeline matched, looked up via
:func:`lode.notes_read.version_body` (not the note's possibly-since-edited live head) —
not the whole note passively dumped; see
:class:`~lode.tui.screens.related_note_modal.RelatedNoteModalScreen` (its own module,
per the one-Screen/Widget-per-module fiat, lode-s5kp.3).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.timer import Timer
from textual.widgets import Static

from lode.tui.screens.related_note_modal import RelatedNoteModalScreen

if TYPE_CHECKING:
    # Type-only; the runtime import lives inside _ensure_embedder so this
    # module's own import stays free of the embedder (fastembed) until a
    # passive-surfacing pass actually runs.
    from lode.embedding import Embedder
    from lode.tui.services.related import RelatedNote

log = logging.getLogger(__name__)

#: Rows :meth:`RelatedNotesPanel.on_mount` reserves beyond the note lines
#: themselves: the ``"Related notes:"`` header rendered by
#: :meth:`RelatedNotesPanel._render_related`.
_HEADER_ROWS = 1

#: Rows reserved for ``lode.tcss``'s ``RelatedNotesPanel:focus`` border, which
#: Textual counts *inside* a fixed height (its default ``box-sizing`` is
#: ``border-box``). **Coupled to that CSS rule**: change the ``:focus``
#: border there — or add one to the unfocused state — and this must change with
#: it, or the panel starts clipping its own last notes (lode-35nu.10).
_FOCUS_BORDER_ROWS = 2


class RelatedNotesPanel(Static):
    """A "related past notes" panel — passive surfacing, interactive stepping.

    Owns its own debounce timer, worker, lazy shared embedder, and rendering;
    a screen only ever calls :meth:`update_draft` (forwarding its
    ``TextArea.Changed`` text) and :meth:`reset` (wherever it used to
    cancel/clear a pass of its own) for the passive half of its contract.
    Composing a screen changes nothing about that half. The interactive half
    (lode-olmi.9) is opt-in from the panel's own focus: see the module
    docstring's "Interactive stepping" section.
    """

    can_focus = True

    BINDINGS: ClassVar = [
        Binding("up", "select_previous", "Prev related"),
        Binding("down", "select_next", "Next related"),
        Binding("enter", "open_selected", "Open related"),
    ]

    def __init__(
        self,
        *,
        exclude_note_id: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__("", id=id, markup=False)
        #: The note this pass must never surface as its own related result
        #: (lode-aoc) -- ``None`` when there is no such note (a brand-new
        #: capture has no id yet).
        self.exclude_note_id = exclude_note_id
        #: The pending debounce timer for a passive surfacing pass, restarted
        #: on every :meth:`update_draft` call; ``None`` when no pass is
        #: scheduled.
        self._related_timer: Timer | None = None
        #: The draft text captured at the most recent :meth:`update_draft`
        #: call -- read back when the debounce timer fires, mirroring "search
        #: whatever the current draft is" without this widget needing a
        #: reference to the screen's ``TextArea``.
        self._pending_draft: str = ""
        #: The most recently rendered related-notes result, kept as plain
        #: widget state (not just ``Static`` markup) so it is a stable, direct
        #: assertion surface for tests rather than parsed back out of the
        #: rendered widget.
        self._related: list[RelatedNote] = []
        #: Which entry in :attr:`_related` Up/Down/Enter act on (lode-olmi.9).
        #: Clamped back into range every time :attr:`_related` is replaced
        #: (:meth:`_render_related`) so a shrinking result set can never leave
        #: it pointing past the end.
        self._selected_index = 0
        #: Incrementing id for each related-notes pass (lode-0wj.2
        #: instrumentation) -- lets the DEBUG log correlate a pass's
        #: start/finish/cancellation lines.
        self._related_pass_seq = 0
        #: The shared query embedder for the related-notes pass, constructed
        #: once and reused for this widget's lifetime (lode-0wj.4) rather than
        #: a fresh instance per pass -- see :meth:`_ensure_embedder`.
        self._embedder: Embedder | None = None

    def on_mount(self) -> None:
        """Reserve this panel's full growth height up front (lode-35nu.10).

        This widget composes into a ``Vertical`` alongside a ``1fr`` body
        ``TextArea`` (``EditScreen``/``CaptureScreen``). ``Static``'s own
        ``DEFAULT_CSS`` height is ``auto`` (``docs/tui.md``), and Textual
        sizes ``auto`` siblings *before* handing the remainder to any ``1fr``
        sibling — so every time a passive pass renders more related notes,
        this panel's auto height grows *after* the ``TextArea`` has already
        been laid out, stealing rows from it on the next layout pass and
        displacing whatever line the edit cursor was resting on. The
        related-notes list is asynchronous and non-user-initiated; it must
        never move the ground under an active edit.

        Fixing this panel's height to its maximum possible size here, at
        mount, before any pass has ever rendered a result, makes its content
        growth invisible to layout: the panel's box never grows past what was
        already reserved, so the ``TextArea``'s own space (and the cursor's
        line within it) never moves, regardless of how many related notes a
        later pass finds.

        Every reserved row is load-bearing: the ``related_notes_limit`` note
        lines a pass can ever render, plus :data:`_HEADER_ROWS`, plus
        :data:`_FOCUS_BORDER_ROWS`. The border rows are the subtle half —
        ``lode.tcss``'s ``RelatedNotesPanel:focus`` rule draws a ``round``
        border once Ctrl+F moves focus here (lode-olmi.9), and Textual's
        default ``box-sizing`` is ``border-box``, so a fixed height counts that
        border *inside* the box. Reserving the note lines and the header alone
        would keep the box stable (the bug this fixes) but silently clip the
        last two related notes the moment the panel is focused — precisely when
        the user is stepping through them with Up/Down, so the selection cursor
        could land on a row that is not on screen. Reserving the border rows
        costs two blank rows while unfocused.

        This arithmetic counts **one row per note**, which is true only because
        ``lode.tcss`` also pins this panel to ``text-wrap: nowrap`` with
        ``text-overflow: ellipsis``. Without that rule a full-length snippet
        wraps to *two* rows and the reservation becomes optimistic rather than
        exact, silently pushing the tail of the list off screen. The two are
        one mechanism — the ``nowrap`` constraint is what makes this
        reservation exact, and together they keep every note reachable in both
        the focused and the unfocused state; dropping either reintroduces a
        bug. Measurements and rejected alternatives: ``docs/tui.md``.
        """
        self.styles.height = (
            self.app.settings.related_notes_limit + _HEADER_ROWS + _FOCUS_BORDER_ROWS
        )

    def _ensure_embedder(self) -> Embedder:
        """Return the shared query embedder, constructing the wrapper on first use.

        Cheap and synchronous: :class:`~lode.embedding.FastEmbedEmbedder`'s
        ``__init__`` only stashes the model name -- the actual (expensive) ONNX
        model load stays lazy inside it (guarded by a lock there against
        concurrent callers) until the first :meth:`_search_related` pass
        actually embeds something (lode-0wj.4). Deliberately **not** warmed
        eagerly at mount: every screen composing this widget (including in
        plain unit tests that never let the debounce fire) would otherwise pay
        a real ONNX model load unconditionally, which is exactly the kind of
        surprise cost :class:`~lode.embedding.FastEmbedEmbedder`'s lazy-by-design
        docstring exists to avoid.
        """
        if self._embedder is None:
            from lode.embedding import FastEmbedEmbedder

            self._embedder = FastEmbedEmbedder(self.app.settings)
        return self._embedder

    def update_draft(self, text: str) -> None:
        """Debounce a passive connection-surfacing pass for ``text``.

        Every call restarts the idle timer (``Settings.related_notes_debounce_ms``)
        rather than searching inline, so a burst of typing triggers at most one
        pass per idle pause -- half of the acceptance criterion's "passive,
        non-blocking" (the other half, keeping the pass itself off the UI
        thread, is :meth:`_search_related`'s job). The caller (a screen's
        ``on_text_area_changed``) is responsible for guarding this to its own
        text area's id -- this widget forwards whatever text it is given.

        An empty/whitespace-only ``text`` (a select-all-delete, or a screen's
        own reset path) has nothing to search on: ``find_related_notes`` would
        only short-circuit on ``related_notes_min_chars``, so debouncing a pass
        just to reach that short-circuit buys a worker and a thread hop and no
        results. The panel is cleared straight away instead of waiting out a
        debounce that would end up clearing it anyway.

        Skipping the pass means ``@work(exclusive=True)`` never runs, so it can
        no longer supersede an in-flight pass from the previous, non-empty
        draft -- that pass has to be cancelled here explicitly
        (:meth:`_cancel_related_pass`) or it lands after this clear and paints
        the deleted draft's related notes into the emptied panel.
        """
        if not text.strip():
            self._cancel_related_pass()
            self._render_related([])
            return
        if self._related_timer is not None:
            self._related_timer.stop()
            self._related_timer = None
        self._pending_draft = text
        delay_s = self.app.settings.related_notes_debounce_ms / 1000
        log.debug(
            "keystroke: related-notes debounce (re)started, delay=%.0fms",
            delay_s * 1000,
        )
        self._related_timer = self.set_timer(delay_s, self._start_related_search)

    def reset(self) -> None:
        """Drop any scheduled/in-flight pass and clear the panel immediately.

        For a screen's own "start fresh" moment (capture's Ctrl+S "Save & New"
        reset -- lode-d32.4's Ctrl+N, folded onto Ctrl+S by lode-bsmc) -- the
        reset drops any scheduled *and* any in-flight pass first
        (:meth:`_cancel_related_pass`), so a slow pass started for the
        just-abandoned draft cannot land afterwards and paint its results into
        the freshly-cleared panel.
        """
        self._cancel_related_pass()
        self._render_related([])

    def on_unmount(self) -> None:
        """Drop any scheduled/in-flight pass when this widget goes away (lode-ivu).

        :meth:`reset` is called explicitly only from
        :meth:`~lode.tui.screens.capture.CaptureScreen.action_save`'s "Save & New" --
        every *other* way a composing screen goes away (the quit/discard confirm's
        save-and-exit, Escape/Ctrl+Q discard,
        :class:`~lode.tui.screens.edit.EditScreen`'s Ctrl+S save-and-pop, a future
        navigation) left the debounce timer (and a since-started worker) running
        unattended. Textual dispatches ``Unmount`` to every mounted widget as a screen
        is popped or the app exits, so this single hook -- not a cancel call duplicated
        into each exit path of every screen that composes this widget (capture *and*
        :class:`~lode.tui.screens.edit.EditScreen`) -- catches all of them uniformly.
        Purely a timer-lifecycle efficiency cleanup: a post- teardown firing was already
        harmless (:func:`lode.tui.services.related.find_related_notes`'s
        ``db_path.exists()`` guard, lode-e1s) and its result was always discarded here
        (nothing left mounted to render into) -- this just stops the wasted embed + FTS5
        + LanceDB pass from running at all -- but only for the part that *can* be
        stopped: a pass already inside :meth:`_search_related`'s ``asyncio.to_thread``
        call runs to completion regardless, and cancelling merely discards its result.
        See :meth:`_cancel_related_pass` and docs/tui.md's "RelatedNotesPanel's
        background pass" section for the tolerate-the-straggler decision that accepts
        that residual.
        """
        self._cancel_related_pass()

    def _cancel_related_pass(self) -> None:
        """Drop the scheduled *and* the in-flight related-notes pass, if any.

        Both halves are needed, and only together: stopping
        ``self._related_timer`` prevents a pass that has not started yet, while
        ``cancel_group`` stops one already running from reaching
        :meth:`_render_related` with results for text no longer current.
        ``cancel_group`` is the same mechanism ``@work(exclusive=True)`` uses to
        supersede a prior pass, and it is prompt enough: every caller here runs
        on the event loop and does not await between this call and the clear
        that follows, so a cancelled worker cannot resume and repaint in the
        gap -- it wakes with ``CancelledError`` at its ``await`` inside
        :meth:`_search_related` instead of returning results.

        "Prompt enough" is a claim about the coroutine side only. The
        ``asyncio.to_thread`` work it awaits is uncancellable: a pass already
        running in its thread keeps executing to completion on its own
        schedule, so this discards that pass's result rather than stopping
        its work. See docs/tui.md's "RelatedNotesPanel's background pass"
        section for the tolerate-the-straggler decision that accepts this
        residual.
        """
        if self._related_timer is not None:
            self._related_timer.stop()
            self._related_timer = None
        self.workers.cancel_group(self, "related-notes")

    def _start_related_search(self) -> None:
        """Timer callback: kick off the search worker for the pending draft."""
        log.debug("related-notes debounce fired: starting a pass")
        self._search_related(self._pending_draft)

    @work(exclusive=True, group="related-notes")
    async def _search_related(self, body: str) -> None:
        """Run the retrieval/graph pipeline off the UI thread, then render it.

        ``find_related_notes`` (:mod:`lode.tui.services.related`) does real DB + local-
        model work (FTS5, LanceDB, the ONNX embedder); ``asyncio.to_thread``
        keeps it off the event loop so typing and each screen's own save/cancel
        bindings are never blocked on it. ``exclusive=True`` (same worker group
        each call) cancels any still-running prior pass before starting this
        one, so a fast typist never sees results arrive out of order.

        **lode-0wj.2 instrumentation:** logs this pass's sequence number, wall-clock
        duration and result count at DEBUG, and whether it got cancelled by a
        newer pass before finishing (``exclusive=True`` makes true overlap
        structurally impossible; this just makes that supersession visible
        instead of silent).

        **lode-aoc:** passes ``self.exclude_note_id`` through so a screen
        editing an existing note never sees that note match its own draft.
        """
        from lode.tui.services.related import find_related_notes

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
                exclude_note_id=self.exclude_note_id,
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
        """Render (or clear) the panel, marking the current selection.

        ``self._related`` is kept as plain widget state alongside the
        rendered content, so a test (or a future caller) has a stable, direct
        assertion surface rather than needing to parse results back out of
        rendered widget content.

        ``self._selected_index`` (lode-olmi.9) is clamped into the new list's
        range here — the one place a fresh (possibly shorter, possibly empty)
        result set lands — so a superseded pass can never leave it pointing
        past the end of the list it now selects into.
        """
        self._related = related
        if not related:
            self._selected_index = 0
            self.update("")
            return
        self._selected_index = min(self._selected_index, len(related) - 1)
        text = Text("Related notes:\n")
        for i, note in enumerate(related):
            selected = i == self._selected_index
            marker = "▸ " if selected else "· "
            text.append(
                f"{marker}{note.age} — {note.snippet}",
                style="reverse" if selected else "",
            )
            if i < len(related) - 1:
                text.append("\n")
        self.update(text)

    def _step_selection(self, delta: int) -> None:
        """Move the selection cursor by ``delta``, wrapping at both ends.

        No-op on an empty result set; the shared body of the Up/Down actions
        so the guard, the wrap, and the re-render can't drift between them
        (lode-olmi.9).
        """
        if not self._related:
            return
        self._selected_index = (self._selected_index + delta) % len(self._related)
        self._render_related(self._related)

    def action_select_previous(self) -> None:
        """Up (while focused): move the selection cursor back one (lode-olmi.9)."""
        self._step_selection(-1)

    def action_select_next(self) -> None:
        """Down (while focused): move the selection cursor forward one (lode-olmi.9)."""
        self._step_selection(1)

    def action_open_selected(self) -> None:
        """Enter (while focused): open the selected note's highlighted context.

        No-op on an empty result set — there is nothing to open (lode-olmi.9).
        """
        if not self._related:
            return
        self.app.push_screen(
            RelatedNoteModalScreen(self._related[self._selected_index])
        )
