"""Keybinding help overlay (lode-2bt3.2) -- the missing discovery surface.

A footerless :class:`~textual.screen.ModalScreen` that lists every reachable
keybinding -- the *active* screen's own ``BINDINGS`` merged with
:class:`~lode.tui.app.LodeApp`'s App-level ``BINDINGS``, both altitudes
visually distinguished the same way :mod:`textual.widgets._key_panel`'s own
``KeyPanel``/``BindingsTable`` already group and style them by namespace
(``BINDING_GROUP_TITLE``). Per the ticket's own review findings (bd notes,
criticisms 1/2/7): **compose Textual's shipped ``BindingsTable`` verbatim, do
not hand-roll the merge/shadow/enable-state logic it already gets right** --
this module writes only the open key
(:meth:`~lode.tui.app.LodeApp.action_show_help`), the pre-push snapshot
mechanism below, and styling.

**Content is derived, never a parallel list** -- ``BindingsTable`` reads
``self.screen.active_bindings`` at render time, so a binding this overlay
doesn't yet know about is a bug in the *snapshot*, never a missed
transcription. ``tests/test_tui_help_screen.py``'s anti-drift test pins that
snapshot: it asserts the overlay's binding set against a representative
screen's real ``BINDINGS`` plus the app's, so a regression in the capture
below -- the one way this overlay *can* go lossy -- fails the suite.

**ModalScreen truncates the binding chain (bd notes, criticism 4/11).**
``Screen._modal_binding_chain`` stops at the last modal on the stack, so a
stock ``BindingsTable`` mounted *inside* this screen would see only this
screen's own (near-empty) bindings via ``self.screen.active_bindings`` --
not the screen underneath it is meant to describe.
:meth:`~lode.tui.app.LodeApp.action_show_help` reads
``self.screen.active_bindings`` **before** pushing this modal (while
``self.screen`` is still the real screen underneath) and passes that
snapshot to :class:`HelpScreen`'s constructor; :attr:`HelpScreen.active_bindings`
below simply overrides ``Screen``'s own property to return it, so the stock,
untouched ``BindingsTable`` widget reads it back via the completely normal
``self.screen.active_bindings`` path -- no widget-level hand-rolling or
monkeypatching needed, only the one property this screen already owns.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import ActiveBinding, Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets._key_panel import BindingsTable

#: The overlay's scrollable dialog container id. Centering and the frame come
#: from ``lode.tcss``'s shared screen-type selector and ``.confirm-dialog``
#: class; this id's own rule carries only the larger 80%/80% size deviation
#: it shares with the other big popups (lode-f0qf).
HELP_DIALOG_ID = "help-dialog"


class HelpScreen(ModalScreen[None]):
    """The keybinding help overlay itself.

    Footerless (docs/tui.md's modal rule, ``lode-ev5j.3``) -- it dismisses on
    Escape/``?`` and carries no other standing action. Reachable from every
    screen via the App-level ``Ctrl+_``/``?`` bindings
    (:class:`~lode.tui.app.LodeApp`); see ``docs/keybindings.md`` for why
    ``Ctrl+_`` (not the unavailable ``Ctrl+?``) is the reachable-everywhere
    key.
    """

    # escape/'?' both dismiss, APP-NAMESPACED per docs/keybindings.md (the
    # bare "pop_screen" form silently no-ops on a Screen). Hidden -- this
    # modal is deliberately footerless.
    BINDINGS: ClassVar = [
        Binding("escape", "app.pop_screen", "Close", show=False),
        Binding("?", "app.pop_screen", "Close", show=False),
    ]

    def __init__(self, active_bindings: dict[str, ActiveBinding]) -> None:
        super().__init__()
        self._snapshot = active_bindings

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:  # type: ignore[override]
        """The screen-underneath's bindings, captured before this modal was
        pushed -- see the module docstring for why this override exists.
        """
        return self._snapshot

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            BindingsTable(shrink=True, expand=False),
            id=HELP_DIALOG_ID,
            classes="confirm-dialog",
        )
