"""The CAS-conflict reconciliation screen (lode-mkc.4) — shown on a rejected save.

``docs/storage.md`` ("What the user sees when CAS rejects a save"): manual
reconciliation, never auto-merge, never clobber. This screen shows a diff of
the caller's buffer against the new head and offers **re-apply** (re-parent
the buffer onto the new head as the next version) or **discard**; both the
draft persistence and the CAS retry are delegated to
:mod:`lode.tui.reconcile` — this screen owns only the diff/keys UI, same
division of labor as :class:`~lode.tui.screens.capture.CaptureScreen` /
:mod:`lode.tui.capture`.

A caller pushes an instance directly — ``self.app.push_screen(ReconcileScreen(conflict))``
— since the screen needs the conflict's data; it is still registered by name
in :data:`~lode.tui.app.LodeApp.SCREENS` for discoverability, following the
app-shell's registration convention.

**A second caller, a different lifecycle (lode-0wj.6).**
:class:`~lode.tui.screens.capture.CaptureScreen` is the app's *root* screen,
so a resolved conflict there ends the whole session (``self.app.exit(...)``)
— that is what every existing test above pins. The edit flow
(:class:`~lode.tui.screens.edit.EditScreen`) instead pushes this screen from
*inside* an already-multi-screen session (capture → browse → edit), where
"resolved" has to mean "pop back to the browse list," not "exit the app."
Rather than hardcoding either convention, the optional ``on_resolved``
constructor argument lets a caller supply its own "what happens next";
leaving it unset (every existing caller) preserves the exit behaviour
byte-for-byte.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Header, Static, TextArea

from lode.tui.lode_footer import LodeFooter
from lode.tui.reconcile import Conflict, discard, reapply
from lode.versions import SaveResult

#: The diff view's widget id — read back in tests.
DIFF_ID = "reconcile-diff"
#: The status line's widget id — read back in tests.
MESSAGE_ID = "reconcile-message"


def _diff_text(conflict: Conflict) -> str:
    """A unified diff: the new head on the left, the rejected buffer on the right."""
    head_lines = (conflict.actual_head_body or "").splitlines(keepends=True)
    buffer_lines = conflict.rejected_buffer.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(head_lines, buffer_lines, fromfile="head", tofile="buffer")
    )
    return "".join(diff) if diff else "(buffer and head are identical)"


class ReconcileScreen(Screen[None]):
    """A buffer-vs-head diff with re-apply/discard bindings for a CAS reject."""

    BINDINGS = [
        Binding("r", "reapply", "Re-apply"),
        Binding("d", "discard", "Discard"),
    ]

    def __init__(
        self,
        conflict: Conflict,
        *,
        on_resolved: Callable[[SaveResult | None], None] | None = None,
    ) -> None:
        super().__init__()
        self.conflict = conflict
        #: Called with the successful ``SaveResult`` on re-apply, or ``None``
        #: on discard, instead of the default "exit the app" — see the
        #: module docstring's lode-0wj.6 note. ``None`` (every existing
        #: caller) preserves the original behaviour exactly.
        self._on_resolved = on_resolved

    def _resolved(self, result: SaveResult | None) -> None:
        """Route a successful resolution to the caller, or exit (the default)."""
        if self._on_resolved is not None:
            self._on_resolved(result)
        elif result is not None:
            self.app.exit(result.note_id)
        else:
            self.app.exit()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static(
                "This note changed since you opened it. Re-apply (r) onto the "
                "new head, or discard (d) your edit.",
                id=MESSAGE_ID,
            ),
            TextArea(_diff_text(self.conflict), read_only=True, id=DIFF_ID),
        )
        yield LodeFooter()

    def action_reapply(self) -> None:
        """Re-parent the buffer onto the new head and save it, or exit on success."""
        result = reapply(self.app.db_path, self.conflict, settings=self.app.settings)
        if isinstance(result, Conflict):
            # The head moved again while this screen was up. No auto-merge —
            # show the newer diff and let the user resolve against it.
            self.conflict = result
            self.query_one(f"#{DIFF_ID}", TextArea).text = _diff_text(result)
            self.notify(
                "Changed again since re-apply; resolve against the latest head.",
                severity="warning",
            )
            return
        self._resolved(result)

    def action_discard(self) -> None:
        """Drop the rejected edit, remove its preserved draft, and exit."""
        discard(self.conflict)
        self._resolved(None)
