"""Read side for the browse screen (lode-0wj.5) -- list live notes, newest first.

``docs/design.md``'s browse screen shows three columns per live note: **Date**
(``notes.created``), **Version** (the edit count / chain length, *not* the
content-hash ``version_id`` -- rendered by the screen as ``v{n}``), and
**Summary** (the head version's ``kind='summary'`` AI annotation, lode-0wj.9,
falling back to the note's first line when no summary annotation exists yet --
a fresh/un-enriched note, since enrichment is async).

This is a **new read function**, not UI-only, mirroring the split every other
E11 screen already uses (:mod:`lode.tui.capture` / :mod:`lode.tui.ask` /
:mod:`lode.tui.related`): pure I/O, no widget/App state, so it is
unit-testable without spinning up a Textual app.

**Live notes only.** A tombstoned note (its head version's ``op = 'delete'``)
is excluded by the same ``v.op != 'delete'`` guard :func:`lode.retrieval.
live_head_versions` and :func:`lode.reconcile`'s gap queries already use for
"the current, non-deleted head" -- this module reimplements the same one-line
filter rather than importing a retrieval-pipeline module the browse screen has
no other reason to depend on.

**Chain length.** Per-note version chains are strictly linear and CAS-guarded
(``docs/storage.md`` "event-sourced, linear per-note chains") -- a note never
branches -- so counting every row in ``versions`` for a given ``note_id`` is
exactly equal to walking ``parent_version_id`` from the head back to the root
and counting steps, without the extra recursive-CTE machinery a branching
chain would need.

**Summary lookup.** The head version's summary annotation is the
``kind='summary'`` row whose ``source_version`` equals the note's current
``head_version_id`` and whose ``status = 'fresh'`` (:mod:`lode.staleness`'s
re-anchor keeps this invariant -- a summary's ``source_version`` only ever
advances to a new head when the row is freshly re-anchored). No such row means
the note hasn't been enriched yet (or the summary was orphaned by an edit and
a fresh one hasn't landed), so the note's first non-blank line stands in.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from lode.storage import init_db


@dataclass(frozen=True, slots=True)
class NoteRow:
    """One live note as the browse list shows it."""

    note_id: str
    created: str
    version: int
    summary: str


def list_notes(db_path: Path) -> list[NoteRow]:
    """Return every live note, newest-first, for the browse screen's table.

    Opens its own short-lived connection (:func:`lode.storage.init_db`), same
    convention as :func:`lode.tui.capture.save_capture` / :func:`lode.tui.ask.
    run_ask` -- this is a plain top-level read, not tied to any open
    connection a caller might hold.
    """
    conn = init_db(db_path)
    try:
        return _list_notes(conn)
    finally:
        conn.close()


def _list_notes(conn: sqlite3.Connection) -> list[NoteRow]:
    rows = conn.execute(
        "SELECT n.note_id, n.created, n.head_version_id, v.body, "
        "(SELECT COUNT(*) FROM versions vc WHERE vc.note_id = n.note_id) "
        "FROM notes n "
        "JOIN versions v ON v.version_id = n.head_version_id "
        "WHERE v.op != 'delete' "
        "ORDER BY n.created DESC"
    ).fetchall()
    return [
        NoteRow(
            note_id=note_id,
            created=created,
            version=chain_length,
            summary=_head_summary(conn, note_id, head_version_id, body),
        )
        for note_id, created, head_version_id, body, chain_length in rows
    ]


def _head_summary(
    conn: sqlite3.Connection, note_id: str, head_version_id: str, head_body: str
) -> str:
    """The head's ``kind='summary'`` AI annotation, or the note's first line."""
    row = conn.execute(
        "SELECT payload FROM annotations "
        "WHERE target = ? AND kind = 'summary' AND source = 'ai' "
        "AND status = 'fresh' AND source_version = ?",
        (note_id, head_version_id),
    ).fetchone()
    if row is not None:
        return json.loads(row[0])
    return _first_line(head_body)


def _first_line(body: str) -> str:
    """The first non-blank line of ``body``, or ``""`` for an all-blank body."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def note_body(db_path: Path, note_id: str) -> str | None:
    """Return ``note_id``'s live head body, or ``None`` if absent/deleted.

    The browse list's row-select opens a read-only view of the full body
    (:class:`~lode.tui.screens.browse.NoteViewScreen`), which needs more than
    :class:`NoteRow`'s summary -- this is that lookup, gated to a live head the
    same way :func:`list_notes` is.
    """
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT v.body FROM notes n "
            "JOIN versions v ON v.version_id = n.head_version_id "
            "WHERE n.note_id = ? AND v.op != 'delete'",
            (note_id,),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()
