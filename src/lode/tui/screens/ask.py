"""The ask screen (lode-mkc.2) -- cited Q&A with provenance, in the TUI.

``docs/retrieval.md`` "Faithfulness: verify citations, don't just require
them": the answer this screen shows is already gated -- every surviving claim
carries a verbatim-verified citation, no_egress matches surface as withheld
rather than silently dropped, and an unsupported question abstains rather
than hallucinating. This screen owns none of that; it only asks a question,
runs the pipeline off the UI thread, and displays what
:func:`lode.tui.services.ask.run_ask` / :func:`lode.tui.services.ask.render_ask_result` return.
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

from lode.tui.services.ask import render_ask_result, run_ask
from lode.tui.widgets.lode_footer import LodeFooter
from lode.tui.widgets.lode_static import LodeStatic

#: The question input's widget id -- read back in tests.
QUESTION_ID = "ask-question"
#: The results pane's widget id -- read back in tests.
RESULTS_ID = "ask-results"

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
    ]

    def __init__(self) -> None:
        super().__init__()
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Input(id=QUESTION_ID, placeholder=_PLACEHOLDER),
            VerticalScroll(
                # LodeStatic defaults markup=False (lode-3dz2, was a per-site
                # markup=False kwarg here -- lode-ix4i): render_ask_result
                # emits literal bracket groups ("[version <id>, as of <ts>]",
                # "[withheld] ...") around verbatim user text, and a
                # markup=True Static -- the stock default -- would silently
                # eat them.
                LodeStatic(_PLACEHOLDER, id=RESULTS_ID),
                id="ask-results-pane",
            ),
        )
        yield LodeFooter()

    def on_mount(self) -> None:
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
                app.db_path, question, settings=app.settings, on_stage=_on_stage
            )
        except AuthError as err:
            self.app.call_from_thread(self._finish, generation, _PLACEHOLDER)
            self.app.call_from_thread(self.notify, str(err), severity="error")
            return
        self.app.call_from_thread(self._finish, generation, render_ask_result(result))

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

    def _finish(self, generation: int, text: str) -> None:
        """Stop the spinner and show the final result for ``generation``. Main thread only.

        Dropped (spinner left running for whichever worker *is* current) if
        a newer question has since superseded this one -- the late-write
        guard.
        """
        if generation != self._ask_generation:
            return
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        results = self.query_one(f"#{RESULTS_ID}", LodeStatic)
        results.update(text)
