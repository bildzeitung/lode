"""The Save/Discard/Cancel confirm dialog (lode-0wj.1), split out of
:mod:`lode.tui.screens.capture` (lode-s5kp.2, the one-Screen-per-module fiat,
``docs/conventions.md``). Pure extraction -- no behaviour change: see
:class:`~lode.tui.screens.capture.CaptureScreen.confirm_quit` for the
Escape/Ctrl+Q guard that pushes this screen, and
:meth:`~lode.tui.screens.capture.CaptureScreen._on_discard_confirm` for how its
answer is handled.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen

from lode.tui.widgets.lode_static import LodeStatic

#: The confirm dialog's message widget id (lode-0wj.1) -- read back in tests.
CONFIRM_MESSAGE_ID = "capture-confirm-message"


class DiscardConfirmScreen(ModalScreen[str]):
    """Save / Discard / Cancel confirm, popped on Escape over a dirty buffer.

    Dismisses with one of ``"save"``, ``"discard"``, ``"cancel"`` — the caller
    (:meth:`~lode.tui.screens.capture.CaptureScreen.confirm_quit`, reached
    from both Escape and the app-level Ctrl+Q) decides what each means; this
    screen owns only the prompt and the three keys.

    **Popup styling (lode-1i8.4).** Pushed via ``push_screen`` (not
    ``switch_screen``), so :class:`~lode.tui.screens.capture.CaptureScreen`
    stays mounted underneath on the app's screen stack rather than being
    replaced — this dialog is an overlay, not a navigation.
    :class:`~textual.screen.ModalScreen`'s own ``DEFAULT_CSS`` already dims
    that screen underneath (``background: $background 60%``); ``lode.tcss``
    (:mod:`lode.tui`, loaded via ``LodeApp.CSS_PATH``) adds only what was
    missing — centering and sizing the ``#capture-confirm-dialog`` box itself
    — so the prompt reads as a bounded, bordered popup over the still-visible
    editor instead of a blank full screen.
    """

    BINDINGS: ClassVar = [
        Binding("s", "choose('save')", "Save & quit"),
        Binding("d", "choose('discard')", "Discard & quit"),
        Binding("c", "choose('cancel')", "Cancel"),
        Binding("escape", "choose('cancel')", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            LodeStatic(
                "Unsaved note. (S)ave, (D)iscard, or (C)ancel?",
                id=CONFIRM_MESSAGE_ID,
            ),
            id="capture-confirm-dialog",
            classes="confirm-dialog",
        )

    def action_choose(self, choice: str) -> None:
        self.dismiss(choice)
