"""The ask screen (lode-mkc.2) -- cited Q&A with provenance, in the TUI.

``docs/retrieval.md`` "Faithfulness: verify citations, don't just require
them": the answer this screen shows is already gated -- every surviving claim
carries a verbatim-verified citation, no_egress matches surface as withheld
rather than silently dropped, and an unsupported question abstains rather
than hallucinating. This screen owns none of that; it only asks a question,
runs the pipeline off the UI thread, and displays what
:func:`lode.tui.services.ask.run_ask` / :func:`lode.tui.services.ask.render_ask_result` return.

**Navigate from a cited result to the note it came from (lode-35nu.4).** Once
an answer renders, Up/Down step a "focused citation" cursor through the
distinct citation targets in the order they render
(:func:`lode.tui.services.ask.citation_targets`) -- shown in
:data:`CITATION_STATUS_ID`'s status line, since the answer itself still
renders as plain text (:data:`RESULTS_ID`) with no per-citation widget to
focus. ``Ctrl+J`` (the last formally-safe letter per ``docs/keybindings.md``)
opens the focused citation's *exact cited version* -- not just the note's
current head -- via :class:`~lode.tui.screens.version_view.VersionViewScreen`,
or the cited snapshot via
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` for an
external. Escape from either of those pops back to this screen, which was
never destroyed, so the answer is exactly as it was -- no re-query.

**"Ask about THIS note" (lode-35nu.11.3).** The constructor's optional
``note_id`` pins that note as primary Q&A context
(:func:`lode.tui.services.ask.run_ask`'s own ``pinned_note_id``), rather than
letting it compete for retrieval rank like every other note. There is no
separate screen for this -- ``docs/conventions.md``'s one-Screen-per-module
fiat governs a *class*, and this is the same class, the same pipeline, the
same gate, and the same grouped/cited rendering, parameterized; a second
near-identical screen module would just be this one's logic forked in two.
The zero-arg form (the App-level ``ctrl+l`` binding, via the ``SCREENS["ask"]``
name-string push in :mod:`lode.tui.app`) is exactly the prior corpus-wide
behaviour, unaffected. :class:`~lode.tui.screens.edit.EditScreen` and
:class:`~lode.tui.screens.version_view.VersionViewScreen` each add their own
SCREEN-level ``Binding("ctrl+l", ...)`` that pushes ``AskScreen(note_id=...)``
-- Textual resolves a keypress screen-first (``docs/keybindings.md``,
"Screen-level shadows App-level on the same key"), so the same key, and the
same "Ask" footer label, simply opens the note-scoped flow instead while one
of those screens is active, with **no new letter spent** against the
already-exhausted ``ctrl+<letter>`` pool that doc's own ledger tracks.
"""

from __future__ import annotations

from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Header, Input

from lode.tui.screens.save_as_note_confirm import SaveAsNoteConfirmScreen
from lode.tui.screens.snapshot_viewer import SnapshotViewerScreen
from lode.tui.screens.version_view import VersionViewScreen
from lode.tui.services.ask import (
    AskResult,
    citation_targets,
    render_ask_result,
    run_ask,
    save_ask_answer_as_note,
)
from lode.tui.services.reconcile import Conflict
from lode.tui.widgets.lode_footer import LodeFooter
from lode.tui.widgets.lode_static import LodeStatic

#: The question input's widget id -- read back in tests.
QUESTION_ID = "ask-question"
#: The results pane's widget id -- read back in tests.
RESULTS_ID = "ask-results"
#: The focused-citation status line's widget id (lode-35nu.4) -- read back in tests.
CITATION_STATUS_ID = "ask-citation-status"

_PLACEHOLDER = "Ask a question about your notes, then press Enter."

#: Braille spinner frames, animated at :data:`_SPINNER_INTERVAL_S` while a
#: question is in flight -- distinguishes "still working" from "hung"
#: (lode-35nu.5), which a static "Thinking..." string could not.
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL_S = 0.1


class AskScreen(Screen[None]):
    """One question input, one results pane. Enter asks; Escape pops back.

    Pushed on top of another screen via the App-level ``ctrl+l`` binding
    (Textual's builtin ``push_screen('ask')`` action string,
    :mod:`lode.tui.app`'s ``LodeApp.BINDINGS`` -- lode-11io, lode-pijc).
    Escape pops back to whatever screen was showing, via the builtin
    APP-NAMESPACED ``app.pop_screen`` action string (bare ``pop_screen``
    silently fails on a Screen -- see ``docs/keybindings.md``).
    """

    BINDINGS: ClassVar = [
        Binding("escape", "app.pop_screen", "Back"),
        # Up/Down step the focused-citation cursor -- hidden from the footer,
        # like a DataTable's own arrow-key row navigation elsewhere in this
        # TUI. These are SCREEN-level, so they only fire when the focused
        # widget doesn't consume the key first. Textual's ``Input`` (the
        # question field, focused on mount) has no up/down binding, so they
        # bubble here. The results pane deliberately still wins: it is a
        # focusable ``VerticalScroll`` whose own up/down bindings scroll the
        # answer, so tabbing to it keeps scrolling intact and simply parks
        # the citation cursor -- verified empirically, not assumed. Ctrl+J
        # is bound to nothing on either, so it reaches this screen from both.
        Binding("up", "focus_prev_citation", "Prev citation", show=False),
        Binding("down", "focus_next_citation", "Next citation", show=False),
        # ctrl+j: the one formally-safe letter docs/keybindings.md's letter
        # ledger had left unclaimed -- confirmed free against Input.BINDINGS,
        # textual.keys.KEY_ALIASES, and every other screen's own BINDINGS.
        Binding("ctrl+j", "open_citation", "Open citation"),
        # ctrl+s: "Save as note" (lode-35nu.11.4). Free here despite being
        # spent on EditScreen/CaptureScreen -- this screen bears no TextArea,
        # only an Input, whose builtins exclude it. The full clearance (all
        # three traps, and why reusing the "Ctrl+S = Save" mnemonic is
        # deliberate) is recorded once in docs/keybindings.md's ledger.
        Binding("ctrl+s", "save_as_note", "Save as note"),
    ]

    def __init__(self, note_id: str | None = None) -> None:
        super().__init__()
        # The pin (lode-35nu.11.3) -- see this module's docstring. ``None`` is
        # the unchanged corpus-wide behaviour.
        self._note_id = note_id
        # The late-write guard (lode-35nu.5's noted hazard).
        # ``@work(exclusive=True)`` cancels a stale worker's *task* but cannot
        # preempt a blocking call already in flight inside it, so a superseded
        # ask can still come back with an answer to an old question. Every
        # write to the results pane therefore carries the generation live when
        # its question was submitted, and is dropped once a newer question has
        # bumped the counter.
        #
        # ``on_input_submitted`` is the counter's ONLY writer -- main thread,
        # never the worker's. That keeps the ``+= 1`` single-threaded (it is a
        # non-atomic read-modify-write) and orders generations by *submission*
        # rather than by the thread pool's arbitrary worker-start order; the
        # inverted order would hand the older question the higher generation
        # and let its answer win, which is the very bug being guarded.
        self._ask_generation = 0
        self._spinner_timer: Timer | None = None
        self._spinner_frame = 0
        self._stage = ""
        # Citation navigation (lode-35nu.4). ``_result`` is the last rendered
        # answer and the SINGLE source of truth for what is navigable: the
        # target list is re-derived from it on demand by ``_targets`` rather
        # than cached alongside it, so the two can never drift out of sync.
        # ``_citation_idx`` is which target Up/Down/Ctrl+J currently act on.
        self._result: AskResult | None = None
        self._citation_idx = 0
        # The last rendered answer text (lode-35nu.11.4) -- exactly what
        # ``RESULTS_ID`` shows. Kept alongside ``_result`` rather than
        # re-rendered on demand so the confirm-preview and the note body
        # that gets saved are byte-for-byte the same text the user just
        # looked at, not a fresh render that could in principle drift.
        self._rendered_text = _PLACEHOLDER

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Input(id=QUESTION_ID, placeholder=_PLACEHOLDER),
            VerticalScroll(
                # LodeStatic defaults markup=False: render_ask_result emits
                # literal bracket groups ("[version <id>, as of <ts>]",
                # "[withheld] ...") around verbatim user text, and a
                # markup=True Static -- the stock default -- would silently
                # eat them.
                LodeStatic(_PLACEHOLDER, id=RESULTS_ID),
                id="ask-results-pane",
            ),
            # Empty until an answer with at least one navigable citation
            # renders -- see :meth:`_update_citation_status`.
            LodeStatic("", id=CITATION_STATUS_ID),
        )
        yield LodeFooter()

    def on_mount(self) -> None:
        if self._note_id is not None:
            # Full 36-char id, same as EditScreen/VersionViewScreen's own
            # sub_title -- selectable/copyable, no width budget to protect
            # here the way Browse's abbreviated Id column has.
            self.sub_title = f"about this note ({self._note_id})"
        self.query_one(f"#{QUESTION_ID}", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question:
            return
        # Bumped here, on the main thread, never inside the worker -- see
        # ``_ask_generation``'s comment in ``__init__`` for why. Starting the
        # spinner here too means the indicator appears on submit, even if the
        # worker's thread is slow to start.
        self._ask_generation += 1
        self._start_spinner()
        # A new question invalidates the previous answer's citations --
        # clear the focused-citation cursor now rather than leaving Ctrl+J
        # briefly reachable against a stale target while the spinner runs.
        self._result = None
        self._citation_idx = 0
        self._update_citation_status()
        self._ask(question, self._ask_generation)

    @work(thread=True, exclusive=True)
    def _ask(self, question: str, generation: int) -> None:
        """Run the ask pipeline off the UI thread -- it does DB + network I/O.

        Deferred import -- :class:`~lode.auth.AuthError` lives in a module
        that pulls in the Anthropic SDK, which nothing in this screen's own
        module scope may load (mirrors ``lode.cli.ask``'s own deferred import
        of the same name, for the same reason).

        ``generation`` is the counter's value at the moment *this* question
        was submitted, passed in by :meth:`on_input_submitted`; every write it
        makes to the results pane carries it and is dropped if superseded.
        See ``_ask_generation``'s comment in :meth:`__init__` for the hazard
        that guard exists for.
        """
        from lode.auth import AuthError

        app = self.app

        def _on_stage(stage: str) -> None:
            self.app.call_from_thread(self._set_stage, generation, stage)

        try:
            result = run_ask(
                app.db_path,
                question,
                settings=app.settings,
                on_stage=_on_stage,
                pinned_note_id=self._note_id,
            )
        except AuthError as err:
            self.app.call_from_thread(self._finish, generation, _PLACEHOLDER, None)
            self.app.call_from_thread(self.notify, str(err), severity="error")
            return
        rendered = render_ask_result(
            result, context_chars=app.settings.ask_context_chars
        )
        self.app.call_from_thread(self._finish, generation, rendered, result)

    def _start_spinner(self) -> None:
        """Begin the animated in-flight indicator. Main thread only.

        Needs no generation check of its own: its sole caller runs on the main
        thread immediately after bumping the counter, so it is always current.
        """
        self._spinner_frame = 0
        self._stage = ""
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
        self._spinner_timer = self.set_interval(_SPINNER_INTERVAL_S, self._tick_spinner)
        self._render_spinner()

    def _set_stage(self, generation: int, stage: str) -> None:
        """Update the in-flight stage text for ``generation``. Main thread only."""
        if generation != self._ask_generation:
            return
        self._stage = stage
        self._render_spinner()

    def _tick_spinner(self) -> None:
        """Advance the spinner animation by one frame. Main thread only (timer callback)."""
        self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
        self._render_spinner()

    def _render_spinner(self) -> None:
        frame = _SPINNER_FRAMES[self._spinner_frame]
        results = self.query_one(f"#{RESULTS_ID}", LodeStatic)
        results.update(f"{frame} {self._stage}" if self._stage else frame)

    def _finish(self, generation: int, text: str, result: AskResult | None) -> None:
        """Stop the spinner and show the final result for ``generation``. Main thread only.

        Dropped (spinner left running for whichever worker *is* current) if
        a newer question has since superseded this one -- the late-write
        guard. ``result`` is ``None`` on the ``AuthError`` path (nothing to
        navigate) and on an abstained answer's own citation set is simply
        empty -- either way :meth:`_update_citation_status` clears the status
        line rather than special-casing them here.
        """
        if generation != self._ask_generation:
            return
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        results = self.query_one(f"#{RESULTS_ID}", LodeStatic)
        results.update(text)
        self._result = result
        self._rendered_text = text
        self._citation_idx = 0
        self._update_citation_status()

    def _targets(self) -> list[str]:
        """The current answer's navigable citation targets, in rendered order.

        Re-derived from :attr:`_result` on demand rather than cached beside
        it, so there is no second field to keep in sync (and no way for the
        two to drift). Cheap: a pure in-memory walk of one already-resolved
        answer's handful of citations, no I/O.
        """
        return citation_targets(self._result) if self._result is not None else []

    def _update_citation_status(self) -> None:
        """Refresh :data:`CITATION_STATUS_ID` for the current focused-citation cursor.

        Empty when there is nothing navigable -- no answer yet, an
        abstention, or an answer whose citations all failed to resolve an
        identity (see :func:`~lode.tui.services.ask.citation_targets`).
        """
        status = self.query_one(f"#{CITATION_STATUS_ID}", LodeStatic)
        targets = self._targets()
        if not targets or self._result is None:
            status.update("")
            return
        identity = self._result.identities[targets[self._citation_idx]]
        status.update(
            f"Citation {self._citation_idx + 1}/{len(targets)}: "
            f"{identity.title}  (Ctrl+J to open)"
        )

    def _step_citation(self, delta: int) -> None:
        """Move the focused-citation cursor by ``delta``, wrapping at both ends."""
        targets = self._targets()
        if not targets:
            return
        self._citation_idx = (self._citation_idx + delta) % len(targets)
        self._update_citation_status()

    def action_focus_prev_citation(self) -> None:
        """Up: move the focused-citation cursor to the previous citation, wrapping."""
        self._step_citation(-1)

    def action_focus_next_citation(self) -> None:
        """Down: move the focused-citation cursor to the next citation, wrapping."""
        self._step_citation(1)

    def action_open_citation(self) -> None:
        """Ctrl+J: open the focused citation's exact cited version/snapshot.

        Pushes :class:`~lode.tui.screens.version_view.VersionViewScreen` keyed to the
        cited ``version_id`` (not the note's current head) for a note citation, or
        :class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` keyed to the
        cited ``snapshot_id`` for an external. Escape from either pops straight back to
        this screen, which was never destroyed -- the answer is exactly as it was, not
        re-queried. A no-op (with a notification) when there is nothing navigable, e.g.
        no question has been asked yet or the answer abstained.
        """
        targets = self._targets()
        if not targets or self._result is None:
            self.notify("no citation to open", severity="warning")
            return
        target_id = targets[self._citation_idx]
        identity = self._result.identities[target_id]
        if identity.note_id is not None:
            self.app.push_screen(VersionViewScreen(identity.note_id, target_id))
        else:
            self.app.push_screen(SnapshotViewerScreen(target_id))

    def action_save_as_note(self) -> None:
        """Ctrl+S: preview-and-confirm saving the current answer as a new note (lode-35nu.11.4).

        Only reachable for a per-note ask (:attr:`_note_id` set -- see the
        module docstring's "Ask about THIS note" section): the new note's
        note->note edge needs a source note to link back to, and corpus-wide
        Ask has none. A no-op (with a notification) when there is no
        confirmable answer yet -- no question asked, still in flight, an
        abstention (nothing was actually answered), or a corpus-wide ask.
        Pushes :class:`~lode.tui.screens.save_as_note_confirm.SaveAsNoteConfirmScreen`
        with the exact text currently on screen; the LLM never writes on its
        own, this only ever fires from the user's own explicit Yes.
        """
        if self._note_id is None:
            self.notify(
                "save as note needs a source note -- ask about a note first",
                severity="warning",
            )
            return
        if self._result is None or self._result.answer.abstained:
            self.notify("no answer to save yet", severity="warning")
            return
        self.app.push_screen(
            SaveAsNoteConfirmScreen(self._rendered_text), self._on_save_as_note_confirm
        )

    def _on_save_as_note_confirm(self, confirmed: bool | None) -> None:
        """Act on the confirm modal's answer: save-through-capture, or leave untouched.

        Rejecting (``False``/``None``, e.g. Escape) writes nothing at all --
        no new note, no edge, no version. A CAS reject on the fresh note id
        (:class:`~lode.tui.services.reconcile.Conflict`, practically
        unreachable -- a fresh ``uuid4`` has nothing to collide with) is
        notified rather than routed to the reconcile screen: there is no
        user-typed buffer to preserve here, just the same already-on-screen
        answer, safe to let the user retry.

        The ``_note_id`` re-check is a type narrow, not a second guard --
        :meth:`action_save_as_note` already refused a corpus-wide ask, and
        the field is fixed at construction. ``_result`` is deliberately NOT
        re-checked: a modal blocks input, so no new question can have landed
        while this callback was pending.
        """
        if not confirmed or self._note_id is None:
            return
        result = save_ask_answer_as_note(
            self.app.db_path,
            source_note_id=self._note_id,
            body=self._rendered_text,
            settings=self.app.settings,
        )
        if isinstance(result, Conflict):
            self.notify("could not save -- please try again", severity="error")
            return
        self.notify("saved as a new note")
