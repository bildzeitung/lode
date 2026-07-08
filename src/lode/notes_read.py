"""Read side for live notes -- list them, newest first (lode-0wj.5, lode-1gr.1).

``docs/design.md``'s browse screen shows three columns per live note: **Date**
(``notes.created``), **Version** (the edit count / chain length, *not* the
content-hash ``version_id`` -- rendered by the screen as ``v{n}``), and
**Summary** (the head version's ``kind='summary'`` AI annotation, lode-0wj.9,
falling back to the note's first line when no summary annotation exists yet --
a fresh/un-enriched note, since enrichment is async). The ``lode notes`` CLI
command (lode-1gr.1) reads the same :func:`list_notes` for the same rows,
printed as plain text instead of a ``DataTable``.

This module lives outside :mod:`lode.tui` (relocated by lode-1gr.1) because it
is pure I/O, no widget/App state -- originally written alongside
:mod:`lode.tui.capture` / :mod:`lode.tui.ask` / :mod:`lode.tui.related` for
that same reason, but a CLI command reading through ``lode.tui.*`` would have
the dependency direction backwards (cli -> tui). :mod:`lode.tui.screens.browse`
imports these functions the same way it always has; nothing about them changed
in the move.

**Live notes only.** :func:`list_notes` excludes a tombstoned note (its head
version's ``op = 'delete'``) via the same ``v.op != 'delete'`` guard
:func:`lode.retrieval.live_head_versions` and :func:`lode.reconcile`'s gap
queries already use for "the current, non-deleted head" -- this module
reimplements the same one-line filter rather than importing a
retrieval-pipeline module the browse screen has no other reason to depend on.
:func:`list_deleted_notes` (lode-d32.2) is the flip side -- ``lode notes
--deleted``'s reader, listing *only* tombstoned notes so their full ids stay
reachable after they vanish from Browse and ``lode notes``.

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

**Version history (lode-0wj.7).** :func:`list_versions` and :func:`version_body`
are this same read side's answer to "view prior versions of a note from
browse" -- unlike the three functions above, which only ever look at the live
head, these two walk and read the *whole* chain. :func:`list_versions` walks
``parent_version_id`` back from the head rather than counting/sorting rows, so
it stays correct even under same-tick timestamps; :func:`version_body` is a
plain ``version_id`` lookup, live or not.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from lode.storage import init_db


#: THE short note-id length across the epic (lode-1gr.2's Browse Id column,
#: lode-1gr.5's 'lode show' short refs) -- long enough to feed
#: 'lode purge <prefix>' (lode-1gr.3) unambiguously in practice. Distinct from
#: lode.ids.SHORT_VERSION_ID_LENGTH (12), which abbreviates VERSION-id digests
#: only (lode-0bs).
SHORT_NOTE_ID_LENGTH = 8


def short_note_id(note_id: str) -> str:
    """Abbreviate a note id to its shared 8-char prefix for column/inline displays.

    The one reusable note-id short helper (decided 2026-07-06, lode-1gr.2):
    both Browse's Id column and 'lode show's short refs (lode-1gr.5) call this
    rather than each growing their own truncation. ``lode notes`` itself
    deliberately does *not* use this -- it prints the full id so it stays
    copy-pasteable straight into ``lode purge`` (lode-1gr.1).
    """
    return note_id[:SHORT_NOTE_ID_LENGTH]


@dataclass(frozen=True, slots=True)
class NoteRow:
    """One note as a note list shows it.

    Shared by :func:`list_notes` (live notes -- the browse table and plain
    ``lode notes``) and :func:`list_deleted_notes` (tombstoned notes --
    ``lode notes --deleted``).
    """

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


def list_deleted_notes(db_path: Path) -> list[NoteRow]:
    """Return every tombstoned note, newest-first -- the counterpart to :func:`list_notes`.

    ``lode notes`` (lode-1gr.1) shows only live notes; a soft-deleted note
    (its head version's ``op = 'delete'``, via :func:`lode.versions.delete`)
    otherwise vanishes with no CLI route back to its id (lode-d32.2). This is
    that sibling reader -- same shape and same short-lived-connection
    convention as :func:`list_notes`, but flipping the ``op`` guard rather
    than overloading :func:`list_notes`' live-only contract that browse/purge/
    retrieval/reconcile all depend on.
    """
    conn = init_db(db_path)
    try:
        return _list_deleted_notes(conn)
    finally:
        conn.close()


def _list_deleted_notes(conn: sqlite3.Connection) -> list[NoteRow]:
    rows = conn.execute(
        "SELECT n.note_id, n.created, v.body, "
        "(SELECT COUNT(*) FROM versions vc WHERE vc.note_id = n.note_id) "
        "FROM notes n "
        "JOIN versions v ON v.version_id = n.head_version_id "
        "WHERE v.op = 'delete' "
        "ORDER BY n.created DESC"
    ).fetchall()
    return [
        NoteRow(
            note_id=note_id,
            created=created,
            version=chain_length,
            # The tombstone's carried-forward body stands in for a summary --
            # no annotation row keys off a delete version_id (annotations are
            # written against a content head, which a tombstone never is; the
            # tombstone re-hashes with the head as its parent, so their
            # version_ids differ), so _head_summary would always miss and fall
            # through to the first line anyway -- skip the lookup and the
            # head_version_id it would need, and go straight there.
            summary=_first_line(body),
        )
        for note_id, created, body, chain_length in rows
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


@dataclass(frozen=True, slots=True)
class VersionRow:
    """One version in a note's chain, as the history screen's table shows it.

    ``seq`` is the version's 1-based position in the chain (root ``create`` =
    1), so the current head's ``seq`` always equals :attr:`NoteRow.version`
    (the same chain-length count :func:`list_notes` reports) -- both are
    "how many versions deep is this note," just counted from opposite ends.
    """

    version_id: str
    created: str
    op: str
    seq: int


def list_versions(db_path: Path, note_id: str) -> list[VersionRow]:
    """Return ``note_id``'s full version chain, newest (the head) first.

    Feeds :class:`~lode.tui.screens.browse.VersionHistoryScreen` (lode-0wj.7):
    "list its prior versions" off a note already opened in the browse screen.
    Walks ``parent_version_id`` back from the live head rather than sorting by
    ``created`` -- the chain link is the actual ancestry, so this stays correct
    even if two versions land in the same timestamp tick, and (per
    ``docs/storage.md``'s linear-chain guarantee) never needs recursive-CTE
    machinery. An absent note returns an empty list rather than raising -- this
    module makes no claim about *why* a note might be missing, only what its
    chain looks like when it exists.
    """
    conn = init_db(db_path)
    try:
        return _list_versions(conn, note_id)
    finally:
        conn.close()


def _list_versions(conn: sqlite3.Connection, note_id: str) -> list[VersionRow]:
    row = conn.execute(
        "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    ).fetchone()
    if row is None:
        return []
    versions = {
        version_id: (parent_version_id, created, op)
        for version_id, parent_version_id, created, op in conn.execute(
            "SELECT version_id, parent_version_id, created, op "
            "FROM versions WHERE note_id = ?",
            (note_id,),
        )
    }
    chain: list[str] = []
    current: str | None = row[0]
    while current is not None:
        chain.append(current)
        current = versions[current][0]
    total = len(chain)
    return [
        VersionRow(
            version_id=version_id,
            created=versions[version_id][1],
            op=versions[version_id][2],
            seq=total - i,
        )
        for i, version_id in enumerate(chain)
    ]


def version_body(db_path: Path, note_id: str, version_id: str) -> str | None:
    """Return one specific version's body, or ``None`` if it isn't in this chain.

    The history list's row-select opens a read-only view of a *prior* version
    (:class:`~lode.tui.screens.browse.VersionViewScreen`, lode-0wj.7) -- unlike
    :func:`note_body` this is keyed to an exact ``version_id``, live or not,
    since viewing history is precisely about seeing a version that is no
    longer the head.
    """
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT body FROM versions WHERE version_id = ? AND note_id = ?",
            (version_id, note_id),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()
