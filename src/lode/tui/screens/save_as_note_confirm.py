"""Confirm-and-save preview for an ask answer, before it becomes a note (lode-35nu.11.4).

Split out of :mod:`lode.tui.screens.ask` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed from
:meth:`~lode.tui.screens.ask.AskScreen.action_save_as_note` via ``Ctrl+S``
once an answer has rendered.

**Preview-and-confirm of the NEW note's content, not a diff against an
existing one** (the ticket's own framing): accepting an ask answer never
mutates the note the question was asked about -- it creates a brand-new note
through the standard capture path, so there is nothing to diff. This screen
just shows the exact text that would become that note's body and asks
Yes/No, mirroring :class:`~lode.tui.screens.delete_confirm.DeleteConfirmScreen`'s
shape (a small binary confirm) rather than
:class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`'s three-way
choice -- there is no third option here. Built on the shared
:class:`~lode.tui.screens.yes_no_confirm.YesNoConfirmScreen` skeleton
(lode-1ip2), extended with a scrollable preview pane via
:meth:`~lode.tui.screens.yes_no_confirm.YesNoConfirmScreen._dialog_children`.
"""

from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widget import Widget

from lode.tui.screens.yes_no_confirm import YesNoConfirmScreen
from lode.tui.widgets.lode_static import LodeStatic

#: The preview pane's widget id -- read back in tests.
SAVE_AS_NOTE_PREVIEW_ID = "save-as-note-preview"
#: The prompt line's widget id -- read back in tests.
SAVE_AS_NOTE_PROMPT_ID = "save-as-note-prompt"


class SaveAsNoteConfirmScreen(YesNoConfirmScreen):
    """A Yes/No confirm previewing the note an accepted ask answer would create.

    Dismisses with a ``bool``: ``True`` on confirm (save), ``False`` on
    decline or Escape -- rejecting writes nothing at all, no new note, no
    edge, no version anywhere (the ticket's own acceptance wording).
    """

    def __init__(self, preview_text: str) -> None:
        super().__init__(
            "Save this answer as a new note, linked to the source note? (Y)es / (N)o",
            dialog_id="save-as-note-confirm-dialog",
            message_id=SAVE_AS_NOTE_PROMPT_ID,
        )
        self._preview_text = preview_text

    def _dialog_children(self) -> list[Widget]:
        return [
            *super()._dialog_children(),
            VerticalScroll(
                # markup=False -- the preview is the answer's own rendered
                # text, which uses literal bracket citation markers (see
                # AskScreen's RESULTS_ID widget for the same reasoning).
                LodeStatic(self._preview_text, id=SAVE_AS_NOTE_PREVIEW_ID),
                id="save-as-note-preview-pane",
            ),
        ]
