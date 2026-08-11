"""Shared Yes/No confirm ModalScreen skeleton (lode-1ip2).

:class:`DeleteConfirmScreen`, :class:`SaveAsNoteConfirmScreen`, and
:class:`NoEgressClearConfirmScreen` were three near-identical
``ModalScreen[bool]`` subclasses -- the same ``y``/``n``/``escape``
bindings onto ``action_choose`` -> ``dismiss(bool)``, and a
``Vertical(..., id=<dialog-id>)`` compose -- differing only in prompt
text, widget id, and (previously) footer binding labels. This module holds
the one shared skeleton; each named screen is now a thin subclass that
declares its own prompt message and widget ids as class attributes, and --
for ``SaveAsNoteConfirmScreen``, which also previews the note body -- extra
compose content via :meth:`YesNoConfirmScreen._extra_children`.

Split into its own module per the one-Screen-per-module fiat
(``docs/conventions.md``); the named subclasses each keep their own module
too, since each is still its own importable screen with its own test call
sites and message-id constants -- only the duplicated BINDINGS/compose/
action_choose bodies moved here.

The ``y``/``n`` binding descriptions are generic ("Yes"/"No") rather than
each subclass's former verb-specific text ("Yes, delete", "Yes, clear",
"Yes, save"). **Not a Textual limitation** -- verified against the installed
Textual (8.2.8): ``DOMNode.__init_subclass__`` merges ``BINDINGS`` down the
MRO, so a subclass declaring its own two-entry ``BINDINGS`` overrides just
the ``y``/``n`` descriptions while still inheriting ``escape`` and the
shared ``compose``/``action_choose``. It would cost ~4 lines per subclass
and it would work.

It is omitted because the descriptions have **no render surface** on these
three screens. They are footerless (docs/tui.md's modal rule,
``lode-ev5j.3``) -- none composes a ``LodeFooter`` -- and
:attr:`~textual.screen.Screen._modal_binding_chain` truncates the binding
chain at the modal, so :class:`~lode.tui.app.LodeApp`'s non-priority
``?``/``ctrl+underscore`` help bindings are not active over them either;
the help overlay that would otherwise show a binding description cannot be
opened from here at all. The prompt text itself carries the verb ("Delete
this note? (Y)es / (N)o"). Re-adding per-subclass ``BINDINGS`` would also
re-expand ``docs/keybindings.md``'s Screen-level table by six rows, since
``tests/test_keybindings_doc.py`` keys off each class's *own* ``BINDINGS``.
Give a subclass its own ``BINDINGS`` the day it grows a footer.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    Escape.

    A subclass supplies its prompt and its two widget ids **declaratively**,
    as the three class attributes below -- they are per-*class* constants
    (no call site passes them), so they sit alongside ``BINDINGS`` as
    class-level data rather than being forwarded through a per-instance
    ``__init__``. A subclass whose dialog needs more than the bare prompt
    (e.g. a preview pane) returns the extra widgets from
    :meth:`_extra_children`.
    """

    #: The prompt text. Spells out the action, since the y/n binding
    #: descriptions have no render surface here (see the module docstring).
    MESSAGE: ClassVar[str]
    #: The outer dialog ``Vertical``'s widget id -- the tcss size hook.
    DIALOG_ID: ClassVar[str]
    #: The prompt widget's id -- queried back in tests.
    MESSAGE_ID: ClassVar[str]

    BINDINGS: ClassVar = [
        Binding("y", "choose(True)", "Yes"),
        Binding("n", "choose(False)", "No"),
        Binding("escape", "choose(False)", "Cancel", show=False),
    ]

    def _extra_children(self) -> Iterable[Widget]:
        """Widgets placed *after* the prompt inside the dialog.

        Defaults to none. Override to add content (e.g. a preview pane);
        the prompt itself is composed by :meth:`compose` and is not the
        override's to re-emit, so a subclass cannot drop it by forgetting a
        ``super()`` call.
        """
        return ()

    def compose(self) -> ComposeResult:
        yield Vertical(
            LodeStatic(self.MESSAGE, id=self.MESSAGE_ID),
            *self._extra_children(),
            id=self.DIALOG_ID,
            classes="confirm-dialog",
        )

    def action_choose(self, confirmed: bool) -> None:
        self.dismiss(confirmed)
