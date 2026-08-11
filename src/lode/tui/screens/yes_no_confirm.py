"""Shared Yes/No confirm ModalScreen skeleton (lode-1ip2).

:class:`DeleteConfirmScreen`, :class:`SaveAsNoteConfirmScreen`, and
:class:`NoEgressClearConfirmScreen` were three near-identical
``ModalScreen[bool]`` subclasses -- the same ``y``/``n``/``escape``
bindings onto ``action_choose`` -> ``dismiss(bool)``, and a
``Vertical(..., id=<dialog-id>)`` compose -- differing only in prompt
text, widget id, and (previously) footer binding labels. This module holds
the one shared skeleton; each named screen is now a thin subclass that
supplies its own prompt message and dialog id, and -- for
``SaveAsNoteConfirmScreen``, which also previews the note body -- extra
compose content via :meth:`YesNoConfirmScreen._dialog_children`.

Split into its own module per the one-Screen-per-module fiat
(``docs/conventions.md``); the named subclasses each keep their own module
too, since each is still its own importable screen with its own test call
sites and message-id constants -- only the duplicated BINDINGS/compose/
action_choose bodies moved here.

The ``y``/``n`` footer labels are generic ("Yes"/"No") rather than each
subclass's former verb-specific text ("Yes, delete", "Yes, clear", "Yes,
save") -- Textual gathers ``BINDINGS`` from the class ``__dict__`` walking
the MRO, not from instance state, so a per-instance label isn't
straightforward, and the dialog's own prompt text already spells out the
action ("(Y)es / (N)o" following "Delete this note?" etc.), so the footer
verb was redundant.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget

from lode.tui.widgets.lode_static import LodeStatic


class YesNoConfirmScreen(ModalScreen[bool]):
    """Shared base for a small Yes/No confirm dialog.

    Dismisses with a ``bool``: ``True`` on confirm, ``False`` on decline or
    Escape. Subclasses pass the prompt ``message``, the dialog's widget
    ``dialog_id``, and the ``message_id`` its prompt widget is queried back
    by in tests; a subclass whose dialog needs more than the bare prompt
    (e.g. a preview pane) overrides :meth:`_dialog_children`.
    """

    BINDINGS: ClassVar = [
        Binding("y", "choose(True)", "Yes"),
        Binding("n", "choose(False)", "No"),
        Binding("escape", "choose(False)", "Cancel", show=False),
    ]

    def __init__(self, message: str, *, dialog_id: str, message_id: str) -> None:
        super().__init__()
        self._message = message
        self._dialog_id = dialog_id
        self._message_id = message_id

    def _dialog_children(self) -> list[Widget]:
        """The widgets inside the dialog's outer ``Vertical``.

        Override to add content beyond the bare prompt (e.g. a preview
        pane) -- call ``super()._dialog_children()`` first to keep the
        prompt itself.
        """
        return [LodeStatic(self._message, id=self._message_id)]

    def compose(self) -> ComposeResult:
        yield Vertical(
            *self._dialog_children(),
            id=self._dialog_id,
            classes="confirm-dialog",
        )

    def action_choose(self, confirmed: bool) -> None:
        self.dismiss(confirmed)
