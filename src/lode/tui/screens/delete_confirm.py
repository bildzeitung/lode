"""A small Yes/No confirm before a browse-row soft-delete (lode-d32.1, extracted lode-s5kp.1).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed from
:meth:`~lode.tui.screens.browse.BrowseScreen.action_delete_selected` via
``d`` on a highlighted row.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen

from lode.tui.widgets.lode_static import LodeStatic

#: The delete-confirm dialog's message widget id -- read back in tests.
DELETE_CONFIRM_MESSAGE_ID = "delete-confirm-message"


class DeleteConfirmScreen(ModalScreen[bool]):
    """A small Yes/No confirm before a browse-row soft-delete (lode-d32.1).

    Mirrors :class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`'s
    popup *styling* (bordered, centered dialog over the dimmed screen
    beneath, lode-1i8.4) but not its Save/Discard/Cancel choices -- there is
    nothing to save here, just "yes, delete" or "no, don't." Dismisses with a
    ``bool``: ``True`` on confirm, ``False`` on decline or Escape.
    """

    BINDINGS = [
        Binding("y", "choose(True)", "Yes, delete"),
        Binding("n", "choose(False)", "No, cancel"),
        Binding("escape", "choose(False)", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            LodeStatic(
                "Delete this note? (Y)es / (N)o",
                id=DELETE_CONFIRM_MESSAGE_ID,
            ),
            id="delete-confirm-dialog",
        )

    def action_choose(self, confirmed: bool) -> None:
        self.dismiss(confirmed)
