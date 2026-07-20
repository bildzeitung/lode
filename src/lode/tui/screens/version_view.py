"""A read-only view of one specific version's body (lode-0wj.7, extracted lode-s5kp.1).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed by :class:`~lode.tui.screens.version_history.
VersionHistoryScreen` on row-select, keyed to that exact ``version_id`` --
deliberately every row, including the current head, rather than filtering it
out: picking the head row just shows the same body
:class:`~lode.tui.screens.edit.EditScreen` already has loaded, which is
harmless and avoids an off-by-one special case for no real benefit. Escape
pops back to that history list -- one level at a time, the same contract every
screen in this browse-family cluster uses.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, TextArea

from lode.notes_read import version_body
from lode.tui.screens._markdown_area import _markdown_text_area
from lode.tui.widgets.lode_footer import LodeFooter
from lode.tui.screens._link_open import open_link_under_cursor

#: The read-only prior-version body's widget id -- read back in tests.
VERSION_BODY_ID = "version-view-body"


class VersionViewScreen(Screen[None]):
    """A read-only view of one specific (possibly non-head) version's body.

    Pushed from :class:`~lode.tui.screens.version_history.
    VersionHistoryScreen` on row-select. Escape pops back to that history
    list -- one level at a time, same as everywhere else in this module.
    """

    # escape/Back uses the APP-NAMESPACED "app.pop_screen" -- the bare
    # "pop_screen" silently fails on a Screen. See docs/keybindings.md.
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+n", "open_link", "Link"),
    ]

    def __init__(self, note_id: str, version_id: str) -> None:
        super().__init__()
        self.note_id = note_id
        self.version_id = version_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield _markdown_text_area(read_only=True, id=VERSION_BODY_ID)
        yield LodeFooter()

    def on_mount(self) -> None:
        body = version_body(self.app.db_path, self.note_id, self.version_id)
        self.query_one(f"#{VERSION_BODY_ID}", TextArea).text = body or ""

    def action_open_link(self) -> None:
        """Ctrl+N: open the URL under the cursor, or explain there isn't one (lode-ev5j.3).

        This body ``TextArea`` is ``read_only=True``, so a bare printable key
        would have been reachable too (see ``docs/keybindings.md``'s
        read-only-body exception) -- ``Ctrl+N`` is used anyway, matching
        :class:`~lode.tui.screens.edit.EditScreen`'s binding exactly, so the
        same keypress opens a link on every screen that has one, whether the
        body happens to be editable here or not.
        """
        text_area = self.query_one(f"#{VERSION_BODY_ID}", TextArea)
        open_link_under_cursor(self, text_area)
