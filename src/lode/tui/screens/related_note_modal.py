"""The related-note glance-and-dismiss modal (lode-olmi.9, extracted lode-s5kp.3).

Pushed by :meth:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel.action_open_selected`
for the currently selected :class:`~lode.tui.services.related.RelatedNote` — Enter, while
the panel holds focus, opens this screen over the highlighted matched passage.
Lives in its own module here under :mod:`lode.tui.screens`, per the
one-Screen/Widget-per-module fiat (``docs/conventions.md``); previously
co-located inside :mod:`lode.tui.widgets.related_notes_panel` itself, alongside
:class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel`. See
:class:`RelatedNoteModalScreen`'s own docstring for why the split needed no
import-cycle workaround.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen

from lode.notes_read import version_body
from lode.tui.widgets.lode_static import LodeStatic

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from lode.tui.services.related import RelatedNote

#: Dialog id for :class:`RelatedNoteModalScreen`'s scrollable body (lode.tcss).
RELATED_MODAL_DIALOG_ID = "related-note-modal-dialog"
#: The ``Static`` inside that dialog holding the (possibly highlighted) body.
RELATED_MODAL_BODY_ID = "related-note-modal-body"


class RelatedNoteModalScreen(ModalScreen[None]):
    """A glance-and-dismiss popup showing one related note's matched context.

    Pushed by :meth:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel.
    action_open_selected` (lode-olmi.9) for the currently selected
    :class:`~lode.tui.services.related.RelatedNote`. Lives in its own module here
    under :mod:`lode.tui.screens` (lode-s5kp.3), per the
    one-Screen/Widget-per-module fiat (``docs/conventions.md``) — previously
    co-located inside :mod:`lode.tui.widgets.related_notes_panel` alongside
    :class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel` itself. The
    import stays one-directional even split across modules: the panel
    imports this screen to push it, and this screen never imports the panel
    back (it only takes a plain :class:`~lode.tui.services.related.RelatedNote` value),
    so there was never a cycle to avoid by co-locating the two — same
    "self-contained, any screen can compose it" stance the panel's own module
    docstring takes for the panel itself.

    **Highlighted context = the matched passage span, not the whole note**
    (the design question the ticket posed, decided in ``--design``): loads
    the note's *exact* ``target_version`` body via :func:`lode.notes_read.
    version_body` — not a current-live-head lookup — because ``char_range``
    is only guaranteed valid against the precise version it was computed
    from; the note's live head can have moved on since. The ``[start:end)``
    slice is styled ``reverse`` in the surrounding body
    (:meth:`_highlighted_body`) via a plain Rich
    :class:`~rich.text.Text` object (not ``markup=True`` on the ``Static`` —
    verbatim note text commonly contains bracket sequences Textual would
    otherwise parse as console markup, the same ``markup=False`` reasoning
    the panel itself already relies on, lode-mkc.3).
    """

    # escape/Back uses the APP-NAMESPACED "app.pop_screen" -- the bare
    # "pop_screen" silently fails on a Screen. See docs/keybindings.md.
    BINDINGS: ClassVar = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, note: RelatedNote) -> None:
        super().__init__()
        self._note = note

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            LodeStatic("", id=RELATED_MODAL_BODY_ID),
            id=RELATED_MODAL_DIALOG_ID,
        )

    def on_mount(self) -> None:
        body = version_body(self.app.db_path, self._note.note_id, self._note.version_id)
        self.query_one(f"#{RELATED_MODAL_BODY_ID}", LodeStatic).update(
            self._highlighted_body(body or "")
        )

    def _highlighted_body(self, body: str) -> Text:
        """Style ``self._note``'s matched ``char_range`` slice of ``body``.

        Falls back to the plain, unhighlighted body whenever ``char_range``
        can't be trusted against *this* ``body`` — malformed
        (``"start:end"`` fails to parse), or out of bounds (``body`` came back
        empty/``None``, e.g. the version row is gone) — rather than raising
        out of a glance-and-dismiss popup or highlighting the wrong span.
        """
        text = Text(body)
        start_str, _, end_str = self._note.char_range.partition(":")
        try:
            start, end = int(start_str), int(end_str)
        except ValueError:
            return text
        if not (0 <= start < end <= len(body)):
            return text
        text.stylize("reverse", start, end)
        return text
