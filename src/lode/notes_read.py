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
:mod:`lode.tui.services.capture` / :mod:`lode.tui.services.ask` / :mod:`lode.tui.services.related` for
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
:func:`candidate_rows_conn` (lode-l38d.10) is a third variant, for a candidate
*set* rather than a whole-table listing: it resolves a specific list of note
ids spanning BOTH states at once, which the ambiguous-prefix CLI error needs
since ``lode recover``'s ``include_deleted=True`` resolution can raise
:class:`~lode.repository.AmbiguousNoteIdError` across a live and a tombstoned
candidate together.

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

**Quick search (lode-35nu.6).** :func:`search_notes` is a fourth read-side
variant, alongside :func:`list_notes`/:func:`list_deleted_notes`/
:func:`candidate_rows_conn` -- offline, model-free BM25 search over live
notes' current content via the existing ``passages_fts`` FTS5 index, for the
browse screen's quick-search box. See its own docstring for why it is not
just a thin wrapper over :meth:`lode.lexical.LexicalIndex.search`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from lode.lexical import LexicalIndex, build_match_query
from lode.storage import init_db

#: Soft cap on how many passage rows :func:`_search_notes` reads back from
#: :meth:`~lode.lexical.LexicalIndex.search` before ranking/deduping to notes.
#: A single note can contribute more than one matching passage, so this must be
#: comfortably above the number of live notes rather than equal to it; a
#: personal note base is small enough that this never binds in practice.
_QUICK_SEARCH_PASSAGE_LIMIT = 1000

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
    convention as :func:`lode.tui.services.capture.save_capture` / :func:`lode.tui.services.ask.
    run_ask` -- this is a plain top-level read, not tied to any open
    connection a caller might hold. A caller that already holds one wants
    :func:`list_notes_conn` instead.
    """
    conn = init_db(db_path)
    try:
        return _list_notes(conn)
    finally:
        conn.close()


def list_notes_conn(conn: sqlite3.Connection) -> list[NoteRow]:
    """Same as :func:`list_notes`, but reuses a connection you already hold.

    Mirrors the :func:`~lode.enrichment_view.enrichment_view` /
    :func:`~lode.enrichment_view.enrichment_view_conn` split, promoted for the
    same reason and on the same terms (lode-ay5.1's technical review: keep it
    private until a real caller exists, since "a public API with no caller and
    no test is speculative"). ``cli._dump_all_notes`` (``dump-html --all``,
    lode-l38d.8) is that caller: it already holds an open ``conn`` and sweeps
    every note's externals through it, so routing its note listing through
    :func:`list_notes` would open a redundant second connection to the same
    file -- re-running the whole schema/migration pass -- just to issue one
    SELECT. This is that caller.
    """
    return _list_notes(conn)


def _list_notes(
    conn: sqlite3.Connection,
    extra_where: str = "",
    params: Sequence[object] = (),
) -> list[NoteRow]:
    """Build the browse note-list projection, optionally narrowed by ``extra_where``.

    ``extra_where`` is spliced in after the live-note guard and before
    ``ORDER BY`` (its ``?`` placeholders bound by ``params``), so a caller
    that needs the *same* rows through a further filter -- currently only
    :func:`_list_notes_with_all_tags`' per-tag ``EXISTS`` clauses -- reuses this
    one query and row-mapping instead of copying both. Empty ``extra_where``
    (the plain :func:`list_notes` call) leaves the query exactly as it was.

    Sorted ``ORDER BY n.rowid DESC``, NOT ``n.created`` (lode-7h8j).
    ``notes.created`` is a SQLite-side wall-clock ``DEFAULT``
    (millisecond resolution, stamped independently per INSERT), so it can
    both tie AND invert relative to insertion order under real scheduling
    load -- demonstrated live: 5/3200 trials of three back-to-back ``save()``
    calls under CPU load came back with an earlier-saved note's ``created``
    landing *after* a later-saved note's, not merely tied. A tiebreaker on
    ``created`` (e.g. ``ORDER BY n.created DESC, n.rowid DESC``) only helps
    the tie case -- when ``created`` values differ but are simply wrong, the
    tiebreaker never runs and the wrong order ships anyway (verified). The
    fix mirrors :func:`lode.versions.version_ids` (docs/storage.md, lode-t1y):
    drop ``created`` from the sort key entirely and order by ``rowid``
    (insertion order) alone, immune to wall-clock jitter either way. Opposite
    *direction* from that precedent, deliberately: a version chain needs
    oldest-first (``ASC``, parent before child), while this listing needs
    newest-first *display* order (``DESC``) -- a different requirement, not
    a copy-paste of that clause.
    """
    rows = conn.execute(
        "SELECT n.note_id, n.created, n.head_version_id, v.body, "
        "(SELECT COUNT(*) FROM versions vc WHERE vc.note_id = n.note_id) "
        "FROM notes n "
        "JOIN versions v ON v.version_id = n.head_version_id "
        "WHERE v.op != 'delete' " + extra_where + "ORDER BY n.rowid DESC",
        params,
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


def search_notes(db_path: Path, query_text: str) -> list[NoteRow]:
    """BM25 quick search over live notes' current content (lode-35nu.6).

    Offline, model-free -- reuses the existing ``passages_fts`` FTS5 index
    (:class:`~lode.lexical.LexicalIndex`, the same one the retrieval pipeline's
    lexical leg reads), no embedder, no network. Returns the matching live
    notes ordered best-match-first; ``query_text`` with no usable token (empty,
    or all punctuation/whitespace) returns an empty list rather than every note
    -- the browse screen's own "clearing it restores the full list" is a
    caller-side branch on an empty ``query_text``, not something this function
    special-cases.

    Two things distinguish this from a plain :meth:`LexicalIndex.search` call:

    - **Scoped to live *note* head versions only**, via a query local to this
      function -- not :func:`lode.retrieval.live_head_versions`, which also
      admits *external* snapshot heads (``lode.externals`` drives its own FTS
      leg through the same ``passages_fts`` table, keyed by ``snapshot_id``
      rather than a note version). An unscoped or externals-inclusive search
      would surface passages with no owning note to open from Browse.
    - **Prefix-matching, sanitized query** via
      :func:`~lode.lexical.build_match_query` (``prefix=True``) -- safe against FTS5
      syntax injection from a free-typed search box, and matches a
      still-being-typed word (an as-you-type box otherwise shows nothing
      until a whole word is finished, since a bare FTS5 term requires an
      exact token match).

    **Known limitation, not fixed here:** a note saved before the lexical leg
    landed (or before any lexical reindex, since no such command exists yet --
    ``cli.py``'s ``reembed`` explicitly leaves FTS untouched) is not in
    ``passages_fts`` and quietly won't surface here. Recorded as a build
    requirement on lode-35nu.6, not resolved by it.
    """
    conn = init_db(db_path)
    try:
        return _search_notes(conn, query_text)
    finally:
        conn.close()


def _search_notes(conn: sqlite3.Connection, query_text: str) -> list[NoteRow]:
    match = build_match_query(query_text, prefix=True)
    if match is None:
        return []
    head_rows = conn.execute(
        "SELECT n.note_id, v.version_id FROM notes n "
        "JOIN versions v ON v.version_id = n.head_version_id "
        "WHERE v.op != 'delete'"
    ).fetchall()
    # An empty map needs no early return of its own: LexicalIndex.search
    # documents an empty ``target_versions`` collection as matching nothing.
    note_id_by_head_version = {version_id: note_id for note_id, version_id in head_rows}
    hits = LexicalIndex(conn).search(
        match,
        k=_QUICK_SEARCH_PASSAGE_LIMIT,
        target_versions=note_id_by_head_version.keys(),
    )
    # Dedup to the one best (first, since hits are already best-first) hit per
    # note, preserving BM25 rank order.
    ranked_note_ids: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        note_id = note_id_by_head_version[hit.target_version]
        if note_id not in seen:
            seen.add(note_id)
            ranked_note_ids.append(note_id)
    if not ranked_note_ids:
        return []
    placeholders = ", ".join("?" for _ in ranked_note_ids)
    matched = _list_notes(
        conn,
        extra_where=f"AND n.note_id IN ({placeholders}) ",
        params=ranked_note_ids,
    )
    rank = {note_id: index for index, note_id in enumerate(ranked_note_ids)}
    matched.sort(key=lambda row: rank[row.note_id])
    return matched


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
    """Sorted ``ORDER BY n.rowid DESC``, NOT ``n.created`` -- same fix as
    :func:`_list_notes` (lode-7h8j), applied here (lode-y1er). ``notes.created``
    is a SQLite-side wall-clock ``DEFAULT`` stamped independently per INSERT, so
    it can both tie AND invert relative to insertion order under real
    scheduling load -- a tiebreaker on ``created`` only helps the tie case;
    when ``created`` values differ but are simply wrong, the tiebreaker never
    runs and the wrong order ships anyway. Dropping ``created`` from the sort
    key entirely and ordering by ``rowid`` (insertion order) alone is immune
    to wall-clock jitter either way.
    """
    rows = conn.execute(
        "SELECT n.note_id, n.created, v.body, "
        "(SELECT COUNT(*) FROM versions vc WHERE vc.note_id = n.note_id) "
        "FROM notes n "
        "JOIN versions v ON v.version_id = n.head_version_id "
        "WHERE v.op = 'delete' "
        "ORDER BY n.rowid DESC"
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


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """One ambiguous-prefix candidate, as the CLI's error rendering shows it (lode-l38d.10).

    Distinct from :class:`NoteRow` -- that one is always scoped to a single
    state (live-only for :func:`list_notes`, tombstoned-only for
    :func:`list_deleted_notes`); a candidate set can mix both (``lode
    recover``'s ``include_deleted=True`` resolution, whose ambiguity is
    judged across live AND deleted notes at once -- repository.py), so each
    row here carries its own :attr:`deleted` flag rather than relying on
    which function returned it.
    """

    note_id: str
    created: str
    summary: str
    deleted: bool


def candidate_rows_conn(
    conn: sqlite3.Connection, note_ids: Sequence[str]
) -> list[CandidateRow]:
    """Resolve date/summary/deleted-state for each of ``note_ids``, in that order.

    The lookup the ambiguous-prefix error needs (lode-l38d.10): every one of
    :class:`~lode.repository.AmbiguousNoteIdError`'s candidates, regardless of
    whether it is live or tombstoned. Unlike :func:`list_notes`/
    :func:`list_deleted_notes`, each scoped to one state via its own ``op``
    guard, a caller here may hold a candidate set spanning both -- ``recover``
    resolves with ``include_deleted=True``, so a prefix matching one live and
    one deleted note is ambiguous by design (repository.py) and both
    candidates must render, not just the live one.

    A tombstoned candidate's summary skips straight to :func:`_first_line` on
    the same grounds :func:`_list_deleted_notes` already documents: a
    tombstone's ``version_id`` is never the ``source_version`` a summary
    annotation was written against (the annotation targets the pre-delete
    head; the tombstone re-hashes with that head as its parent), so
    :func:`_head_summary` would always miss and fall through to
    :func:`_first_line` anyway -- skip the lookup and go straight there.

    Takes an already-open ``conn`` (the ``Repository``'s own, same one that
    just raised the ``AmbiguousNoteIdError`` this feeds) rather than a
    ``db_path`` -- every current caller already holds one, so there is no
    non-conn caller to serve (the same "no speculative public API" reasoning
    :func:`list_notes_conn` was promoted under).
    """
    if not note_ids:
        return []
    placeholders = ",".join("?" for _ in note_ids)
    found = {
        note_id: (created, head_version_id, body, op)
        for note_id, created, head_version_id, body, op in conn.execute(
            "SELECT n.note_id, n.created, n.head_version_id, v.body, v.op "
            "FROM notes n JOIN versions v ON v.version_id = n.head_version_id "
            f"WHERE n.note_id IN ({placeholders})",
            tuple(note_ids),
        )
    }
    rows = []
    for note_id in note_ids:
        created, head_version_id, body, op = found[note_id]
        deleted = op == "delete"
        summary = (
            _first_line(body)
            if deleted
            else _head_summary(conn, note_id, head_version_id, body)
        )
        rows.append(
            CandidateRow(
                note_id=note_id, created=created, summary=summary, deleted=deleted
            )
        )
    return rows


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

    Feeds :class:`~lode.tui.screens.version_history.VersionHistoryScreen` (lode-0wj.7):
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
    (:class:`~lode.tui.screens.version_view.VersionViewScreen`, lode-0wj.7) -- unlike
    a live-head-only lookup, this is keyed to an exact ``version_id``, live or
    not, since viewing history is precisely about seeing a version that is no
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


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """One external snapshot's stored content, as the content viewer shows it (lode-0sjj).

    ``body`` is the extracted text (``schema.sql``'s ``snapshots.body TEXT NOT
    NULL`` -- even a tombstoned snapshot carries a stable placeholder body,
    :func:`lode.externals.tombstone_body`, so this is never ``None``).
    ``raw_payload`` is the original fetched bytes/markup and *is* nullable --
    a snapshot may simply have never captured raw HTML.
    """

    body: str
    raw_payload: str | None


def read_snapshot(db_path: Path, snapshot_id: str) -> SnapshotRow | None:
    """Return one snapshot's stored body/raw_payload, or ``None`` if missing.

    Feeds :class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` (lode-0sjj):
    a plain ``snapshot_id`` lookup, the read-side counterpart to
    :func:`lode.cli.dump_html`'s (lode-olmi.7) ``raw_payload`` SELECT. That
    query now has a name of its own -- ``cli._raw_payload`` (lode-l38d.8,
    which needed it on two paths) -- but it stays private to ``cli.py``: it
    takes the open ``conn`` that command already holds and returns only
    ``raw_payload``, where this returns a whole :class:`SnapshotRow` from a
    ``db_path``. A later refactor could still unify the two, on the
    :func:`list_notes` / :func:`list_notes_conn` pattern (a shared
    ``read_snapshot_conn``); nothing needs it yet.
    """
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT body, raw_payload FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return SnapshotRow(body=row[0], raw_payload=row[1])
    finally:
        conn.close()


def _visible_tag_where(prefix: str = "") -> str:
    """A live, visible ``kind='tag'`` row's ``WHERE`` fragment (lode-olmi.6).

    Mirrors :func:`lode.display.classify_annotation_display`'s tombstone
    exclusion (a ``source='user' AND status='orphaned'`` row is a curation
    tombstone, never a real tag) without importing that target-scoped helper
    -- the same "reimplement the one filter this module needs" convention
    :func:`list_notes` already uses for its own ``op != 'delete'`` guard. Tags
    are never hidden for staleness alone (unlike :data:`lode.display.
    ASSERTIVE_KINDS`) -- ``docs/storage.md``'s stale-display policy shows a
    stale tag flagged, not hidden -- so this is the only check needed.
    ``prefix`` (e.g. ``"a."``) lets the same fragment work unqualified (the
    top-level ``annotations`` scan in :func:`_list_tags`) or against a table
    alias (the correlated ``EXISTS`` subquery in
    :func:`_list_notes_with_all_tags`).
    """
    return (
        f"{prefix}kind = 'tag' AND "
        f"NOT ({prefix}source = 'user' AND {prefix}status = 'orphaned')"
    )


def list_tags(db_path: Path) -> list[str]:
    """Return every distinct visible tag value across all notes, sorted.

    Tags live in ``annotations`` as ``kind='tag'`` rows (lode-olmi.6) -- there
    is no dedicated tags table -- one row per ``(note, tag)`` pair, ``payload``
    the JSON-encoded tag string. Powers the Tags screen's top panel
    (:class:`~lode.tui.screens.tags.TagsScreen`), which multi-selects across
    this exact set.
    """
    conn = init_db(db_path)
    try:
        return _list_tags(conn)
    finally:
        conn.close()


def _list_tags(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        f"SELECT DISTINCT payload FROM annotations WHERE {_visible_tag_where()}"
    ).fetchall()
    return sorted(json.loads(payload) for (payload,) in rows)


def list_notes_with_all_tags(db_path: Path, tags: Collection[str]) -> list[NoteRow]:
    """Return every live note carrying **every** tag in ``tags`` (AND/intersection).

    The Tags screen's (lode-olmi.6) bottom-panel filter: an empty ``tags``
    means no filter at all, so this returns exactly what :func:`list_notes`
    does (every live note, newest-first). Each selected tag narrows the set
    further via its own ``EXISTS`` clause matched against the tag's *exact*
    JSON-encoded payload -- the same equality :func:`lode.curation.
    is_annotation_suppressed` uses for a single tag, just repeated once per
    tag so a note only qualifies when *every* clause finds a live
    (non-tombstone) row for it.
    """
    conn = init_db(db_path)
    try:
        return _list_notes_with_all_tags(conn, tags)
    finally:
        conn.close()


def _list_notes_with_all_tags(
    conn: sqlite3.Connection, tags: Collection[str]
) -> list[NoteRow]:
    tag_list = list(tags)
    # One EXISTS clause per selected tag (empty selection -> "", i.e. the plain
    # list_notes query): a note qualifies only when a live tag row matches every
    # clause. Delegates the shared SELECT + NoteRow mapping to _list_notes.
    exists_clause = (
        "AND EXISTS (SELECT 1 FROM annotations a WHERE a.target = n.note_id "
        f"AND a.payload = ? AND {_visible_tag_where('a.')}) "
    )
    return _list_notes(
        conn,
        exists_clause * len(tag_list),
        [json.dumps(tag) for tag in tag_list],
    )
