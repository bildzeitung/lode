"""The ask screen (lode-mkc.2) -- cited Q&A with provenance, in the TUI.

``docs/retrieval.md`` "Faithfulness: verify citations, don't just require
them": the answer this screen shows is already gated -- every surviving claim
carries a verbatim-verified citation, no_egress matches surface as withheld
rather than silently dropped, and an unsupported question abstains rather
than hallucinating. This screen owns none of that; it only asks a question,
runs the pipeline off the UI thread, and displays what
:func:`lode.tui.ask.run_ask` / :func:`lode.tui.ask.render_ask_result` return.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Header, Input, Static

from lode.tui.ask import render_ask_result, run_ask
from lode.tui.lode_footer import LodeFooter

#: The question input's widget id -- read back in tests.
QUESTION_ID = "ask-question"
#: The results pane's widget id -- read back in tests.
RESULTS_ID = "ask-results"

_PLACEHOLDER = "Ask a question about your notes, then press Enter."
_THINKING = "Thinking..."


class AskScreen(Screen[None]):
    """One question input, one results pane. Enter asks; Escape pops back.

    Pushed on top of another screen via the App-level ``ctrl+l`` binding
    (Textual's builtin ``push_screen('ask')`` action string,
    :mod:`lode.tui.app`'s ``LodeApp.BINDINGS`` -- lode-11io, lode-pijc) --
    Escape pops back to whatever screen was showing, via Textual's builtin
    APP-NAMESPACED ``app.pop_screen`` action string (lode-pijc): the
    unqualified ``pop_screen`` resolves against this Screen's own action
    namespace, which has no such method, and silently fails (verified
    against Textual 8.2.8).
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Input(id=QUESTION_ID, placeholder=_PLACEHOLDER),
            VerticalScroll(Static(_PLACEHOLDER, id=RESULTS_ID), id="ask-results-pane"),
        )
        yield LodeFooter()

    def on_mount(self) -> None:
        self.query_one(f"#{QUESTION_ID}", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question:
            return
        self._ask(question)

    @work(thread=True, exclusive=True)
    def _ask(self, question: str) -> None:
        """Run the ask pipeline off the UI thread -- it does DB + network I/O.

        Deferred import -- :class:`~lode.auth.AuthError` lives in a module
        that pulls in the Anthropic SDK, which nothing in this screen's own
        module scope may load (mirrors ``lode.cli.ask``'s own deferred import
        of the same name, for the same reason).
        """
        from lode.auth import AuthError

        results = self.query_one(f"#{RESULTS_ID}", Static)
        self.app.call_from_thread(results.update, _THINKING)
        app = self.app
        try:
            result = run_ask(app.db_path, question, settings=app.settings)
        except AuthError as err:
            self.app.call_from_thread(results.update, _PLACEHOLDER)
            self.app.call_from_thread(self.notify, str(err), severity="error")
            return
        self.app.call_from_thread(results.update, render_ask_result(result))
