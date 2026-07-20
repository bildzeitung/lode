"""A retrieved external's stored content -- body by default, raw on toggle (lode-0sjj, extracted lode-s5kp.1).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed keyed to one ``snapshot_id`` by
:func:`~lode.tui.screens._content_view._view_note_external_content` -- directly, for
a note with exactly one external edge, or after
:class:`~lode.tui.screens.external_picker.ExternalPickerScreen` resolves which
of several.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import TextArea

from lode.notes_read import SnapshotRow, read_snapshot
from lode.tui.screens._link_open import open_link_under_cursor
from lode.tui.screens._markdown_area import _markdown_text_area
from lode.tui.widgets.lode_footer import LodeFooter

#: The content-viewer modal's body ``TextArea`` widget id (lode-0sjj) -- read
#: back in tests.
SNAPSHOT_VIEWER_BODY_ID = "snapshot-viewer-body"
#: The content-viewer modal's dialog container id -- read back in tests.
SNAPSHOT_VIEWER_DIALOG_ID = "snapshot-viewer-dialog"


class SnapshotViewerScreen(ModalScreen[None]):
    """A retrieved external's stored content -- body by default, raw on toggle (lode-0sjj).

    Pushed keyed to one ``snapshot_id`` by
    :func:`~lode.tui.screens._content_view._view_note_external_content` -- directly,
    for a note with exactly one external edge, or after
    :class:`~lode.tui.screens.external_picker.ExternalPickerScreen` resolves
    which of several. Shows ``snapshots.body`` (the extracted text --
    ``NOT NULL``, ``schema.sql``; even a tombstone snapshot carries a stable
    placeholder body, :func:`lode.externals.tombstone_body`) in a read-only
    ``TextArea`` by default. ``Binding('t', 'toggle_raw', ...)`` switches to
    ``snapshots.raw_payload`` instead -- the same nullable raw-HTML column
    ``lode dump-html`` (lode-olmi.7) prints to stdout -- and back again on a
    second press. Unlike that CLI command, a missing ``raw_payload`` here
    isn't an error: it notifies ``'no raw HTML captured for this source'``
    and stays on the body, since the body is still perfectly viewable and the
    toggle simply has nothing to switch to (never a blank toggle).

    ``Esc`` dismisses -- the same one-level-at-a-time contract every other
    modal in this module uses. Bare printable ``t`` is safe here (unlike
    :class:`~lode.tui.screens.edit.EditScreen`'s own binding for reaching
    this screen, ``docs/keybindings.md``): this screen's body ``TextArea`` is
    ``read_only=True``, so it never intercepts a printable keypress before a
    Screen-level binding sees it.

    **``LodeFooter`` added (lode-ev5j.3), the first of this module's small
    popups to get one.** The confirm-style modals elsewhere in the tree
    (``DiscardConfirmScreen``, ``DeleteConfirmScreen``, ``EnrichmentModalScreen``,
    ``RelatedNoteModalScreen``) are transient glance-and-dismiss popups that
    stay footerless on purpose. This screen already carried two real,
    discoverable actions before this ticket (``Back``, ``Toggle raw HTML``)
    with nowhere to show them; lode-ev5j.3's own acceptance criterion --
    the open-link binding shown in the footer on every one of its three
    target screens, this one included -- makes that gap a blocker rather
    than a pre-existing quirk to leave alone, so it's closed here rather
    than deferred.
    """

    # escape/Back uses the APP-NAMESPACED "app.pop_screen" -- the bare
    # "pop_screen" silently fails on a Screen. See docs/keybindings.md.
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("t", "toggle_raw", "Toggle raw HTML"),
        Binding("ctrl+n", "open_link", "Link"),
    ]

    def __init__(self, snapshot_id: str) -> None:
        super().__init__()
        self.snapshot_id = snapshot_id
        self._snapshot: SnapshotRow | None = None
        self._showing_raw = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            _markdown_text_area(read_only=True, id=SNAPSHOT_VIEWER_BODY_ID),
            id=SNAPSHOT_VIEWER_DIALOG_ID,
        )
        yield LodeFooter()

    def on_mount(self) -> None:
        snapshot = read_snapshot(self.app.db_path, self.snapshot_id)
        if snapshot is None:
            # Only ever pushed for a snapshot_id an already-assembled
            # ExternalView carried, so a missing row here would be a real
            # bug, not a normal race worth a soft fallback -- the same stance
            # EnrichmentModalScreen.on_mount takes for a missing note.
            raise LookupError(f"no snapshot {self.snapshot_id!r} to view")
        self._snapshot = snapshot
        self._show_body()

    def _show_body(self) -> None:
        self._showing_raw = False
        assert self._snapshot is not None
        self.query_one(
            f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea
        ).text = self._snapshot.body

    def action_toggle_raw(self) -> None:
        """``t``: switch to the raw HTML, or back to the body from there."""
        assert self._snapshot is not None
        if self._showing_raw:
            self._show_body()
            return
        if not self._snapshot.raw_payload:
            self.notify("no raw HTML captured for this source", severity="warning")
            return
        self._showing_raw = True
        self.query_one(
            f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea
        ).text = self._snapshot.raw_payload

    def action_open_link(self) -> None:
        """Ctrl+N: open the URL under the cursor, or explain there isn't one (lode-ev5j.3).

        Works against whichever body is currently showing -- the extracted
        text or the raw HTML, per :attr:`_showing_raw` -- since both are the
        same ``TextArea`` (:data:`SNAPSHOT_VIEWER_BODY_ID`), just swapped by
        :meth:`action_toggle_raw`.
        """
        text_area = self.query_one(f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea)
        open_link_under_cursor(self, text_area)
