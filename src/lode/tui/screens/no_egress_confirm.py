"""A small Yes/No confirm before browse un-withholds a note (lode-a50f).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed from
:meth:`~lode.tui.screens.browse.BrowseScreen.action_toggle_no_egress`, only
for the CLEARING direction -- setting ``no_egress`` is the safe direction and
stays a bare, unconfirmed toggle, same as before lode-a50f. Clearing it is the
dangerous one: it makes an explicitly withheld note cloud-eligible again on
its next enrichment/Q&A send, so it gets the same confirm-before-acting
treatment :class:`~lode.tui.screens.delete_confirm.DeleteConfirmScreen`
already gives a soft-delete.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen

from lode.tui.widgets.lode_static import LodeStatic

#: The no-egress-clear-confirm dialog's message widget id -- read back in tests.
NO_EGRESS_CLEAR_CONFIRM_MESSAGE_ID = "no-egress-clear-confirm-message"


class NoEgressClearConfirmScreen(ModalScreen[bool]):
    """A small Yes/No confirm before clearing a note's no_egress flag (lode-a50f).

    Mirrors :class:`~lode.tui.screens.delete_confirm.DeleteConfirmScreen`'s
    popup styling and Yes/No shape. Dismisses with a ``bool``: ``True`` on
    confirm, ``False`` on decline or Escape.
    """

    BINDINGS: ClassVar = [
        Binding("y", "choose(True)", "Yes, clear"),
        Binding("n", "choose(False)", "No, cancel"),
        Binding("escape", "choose(False)", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            LodeStatic(
                "Clear no-egress on this note? It will become cloud-eligible"
                " again. (Y)es / (N)o",
                id=NO_EGRESS_CLEAR_CONFIRM_MESSAGE_ID,
            ),
            id="no-egress-clear-confirm-dialog",
        )

    def action_choose(self, confirmed: bool) -> None:
        self.dismiss(confirmed)
