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

import sqlite3
from pathlib import Path

from lode.storage import init_db
from lode.versions import set_no_egress


def _read_no_egress(conn: sqlite3.Connection, note_id: str) -> bool:
    """The one ``SELECT no_egress`` in this module -- both public functions read
    through here rather than each spelling the column/table name out again.

    ``False`` if the note has no row: a missing row is "not withheld" rather
    than an error, since both callers are display-or-toggle paths holding a
    note a screen already listed.
    """
    row = conn.execute(
        "SELECT no_egress FROM notes WHERE note_id = ?", (note_id,)
    ).fetchone()
    return bool(row[0]) if row is not None else False


def note_no_egress_conn(conn: sqlite3.Connection, note_id: str) -> bool:
    """Connection-taking core of :func:`note_no_egress` (lode-nnqp).

    Split out so a caller that already holds an open connection -- e.g.
    :meth:`~lode.tui.screens.edit.EditScreen.on_mount`, which also needs
    :func:`~lode.tui.services.edit.load_head_conn` -- can read the flag
    without paying :func:`lode.storage.init_db`'s schema-DDL-plus-migration
    cost a second time. Same public/private split as :func:`_read_no_egress`
    itself, just exported one layer up for reuse across modules.
    """
    return _read_no_egress(conn, note_id)


def note_no_egress(db_path: Path, note_id: str) -> bool:
    """Whether ``note_id`` is currently marked no_egress."""
    conn = init_db(db_path)
    try:
        return note_no_egress_conn(conn, note_id)
    finally:
        conn.close()


def toggle_note_no_egress(db_path: Path, note_id: str) -> bool:
    """Flip ``note_id``'s no_egress flag and return the resulting state.

    Reads the current value, then writes the opposite through
    :func:`lode.versions.set_no_egress` on the same connection --
    :class:`~lode.tui.screens.browse.BrowseScreen`'s toggle
    (:meth:`~lode.tui.screens.browse.BrowseScreen.action_toggle_no_egress`)
    calls this and nowhere else writes the column from the TUI, so there is
    exactly one write path.
    """
    conn = init_db(db_path)
    try:
        new_state = not _read_no_egress(conn, note_id)
        set_no_egress(conn, note_id, no_egress=new_state)
        return new_state
    finally:
        conn.close()
