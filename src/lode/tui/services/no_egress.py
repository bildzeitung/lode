"""Per-note no_egress read/toggle wiring for the TUI (lode-82wt).

Pure I/O, no widget/App state -- same convention as :mod:`lode.tui.services.edit`
-- so it is unit-testable without spinning up a Textual app.

**The single write path.** :func:`toggle_note_no_egress` is the only place a
TUI screen ever flips ``notes.no_egress`` -- it always goes through
:func:`lode.versions.set_no_egress`, the same setter ``lode no-egress
--note`` (:mod:`lode.cli.egress`) calls. There is no second code path to the
column: a screen that wants to toggle a note's no_egress flag calls this
function, never ``UPDATE notes SET no_egress`` directly.

:func:`note_no_egress` is the read side -- used by
:class:`~lode.tui.screens.edit.EditScreen` to decide whether to show the
no-egress marker on mount, and internally by :func:`toggle_note_no_egress` to
know which way to flip.
"""

from __future__ import annotations

from pathlib import Path

from lode.storage import init_db
from lode.versions import set_no_egress


def note_no_egress(db_path: Path, note_id: str) -> bool:
    """Whether ``note_id`` is currently marked no_egress.

    ``False`` if the note has no row -- should not happen for a live note a
    screen already has in hand, but a missing row is "not withheld" rather
    than an error here, since this is a display-only read.
    """
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT no_egress FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        return bool(row[0]) if row is not None else False
    finally:
        conn.close()


def toggle_note_no_egress(db_path: Path, note_id: str) -> bool:
    """Flip ``note_id``'s no_egress flag and return the resulting state.

    Reads the current value, then writes the opposite through
    :func:`lode.versions.set_no_egress` in the same connection --
    :class:`~lode.tui.screens.browse.BrowseScreen`'s toggle
    (:meth:`~lode.tui.screens.browse.BrowseScreen.action_toggle_no_egress`)
    calls this and nowhere else writes the column from the TUI, so there is
    exactly one write path.
    """
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT no_egress FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        new_state = not bool(row[0]) if row is not None else True
        set_no_egress(conn, note_id, no_egress=new_state)
        return new_state
    finally:
        conn.close()
