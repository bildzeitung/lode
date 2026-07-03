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
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Header, Static, TextArea

from lode.tui.capture import CaptureConflict, EmptyCaptureError, save_capture
from lode.tui.screens.reconcile import ReconcileScreen

if TYPE_CHECKING:
    # Type-only; the runtime import lives inside _search_related so this
    # screen's own import stays free of the vector stack (pyarrow) and the
    # embedder (fastembed) until a passive-surfacing pass actually runs.
    from lode.tui.related import RelatedNote

#: The text area's widget id — read back in tests and by this screen alike.
BODY_ID = "capture-body"

#: The passive "related past notes" panel's widget id (lode-mkc.3) — read back
#: in tests.
RELATED_ID = "related-notes"


class CaptureScreen(Screen[None]):
    """One text area plus a passive related-notes panel.

    Ctrl+S saves and exits; Escape discards and exits. The related-notes panel
    is read-only and non-interactive — it never takes focus or input, so it
    changes nothing about capture's "get in, dump text, get out" contract.
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
        """Discard the buffer and exit without saving."""
        self.app.exit()

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
        self._related_timer = self.set_timer(delay_s, self._start_related_search)

    def _start_related_search(self) -> None:
        """Timer callback: read the current buffer and kick off the search worker."""
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
        """
        from lode.tui.related import find_related_notes

        app = self.app
        related = await asyncio.to_thread(
            find_related_notes, app.db_path, body, settings=app.settings
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
