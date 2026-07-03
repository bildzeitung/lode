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
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, TextArea

from lode.tui.capture import CaptureConflict, EmptyCaptureError, save_capture
from lode.tui.screens.reconcile import ReconcileScreen

#: The text area's widget id — read back in tests and by this screen alike.
BODY_ID = "capture-body"


class CaptureScreen(Screen[None]):
    """One text area. Ctrl+S saves and exits; Escape discards and exits."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save & quit"),
        Binding("escape", "cancel", "Discard & quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            TextArea(id=BODY_ID, placeholder="What did you learn today?"),
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
