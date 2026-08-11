"""A small Yes/No confirm before a browse-row soft-delete (lode-d32.1, extracted lode-s5kp.1).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed from
:meth:`~lode.tui.screens.browse.BrowseScreen.action_delete_selected` via
``d`` on a highlighted row.
"""

from __future__ import annotations

from lode.tui.screens.yes_no_confirm import YesNoConfirmScreen

#: The delete-confirm dialog's message widget id -- read back in tests.
DELETE_CONFIRM_MESSAGE_ID = "delete-confirm-message"


class DeleteConfirmScreen(YesNoConfirmScreen):
    """A small Yes/No confirm before a browse-row soft-delete (lode-d32.1).

    Mirrors :class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`'s
    popup *styling* (bordered, centered dialog over the dimmed screen
    beneath, lode-1i8.4) but not its Save/Discard/Cancel choices -- there is
    nothing to save here, just "yes, delete" or "no, don't." Built on the
    shared :class:`~lode.tui.screens.yes_no_confirm.YesNoConfirmScreen`
    skeleton (lode-1ip2).
    """

    def __init__(self) -> None:
        super().__init__(
            "Delete this note? (Y)es / (N)o",
            dialog_id="delete-confirm-dialog",
            message_id=DELETE_CONFIRM_MESSAGE_ID,
        )
