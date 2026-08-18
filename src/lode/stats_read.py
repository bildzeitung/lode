"""Read side for ``lode stats`` (lode-tyhy) -- read-only corpus-inspection queries.

Every query here is a bare ``SELECT`` over an already-open connection, no
writes, no queue interaction, no new config knobs -- the same "no bare SQL
in the cli package" split ``lode.jobs_read``/``lode.notes_read`` already
establish (lode-35nu.9): ``lode.cli.stats`` is dispatch + rendering only,
every SQL-touching helper lives here.

Snapshot-status and tombstone-reason breakdowns are scoped to **head**
snapshots only (``externals.head_snapshot_id`` joined to ``snapshots``) --
the same join every other reader in this codebase uses for "an external's
current fetched state" (``lode.reconcile``, ``lode.retrieval``,
``lode.enrichment_view``). A superseded, non-head snapshot is history, not
current corpus state, and is excluded here the same way it is everywhere
else.
"""

import sqlite3
from typing import NamedTuple

from lode.externals import parse_tombstone_reason, tombstone_body


def snapshot_status_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """``(status, count)`` over every external's HEAD snapshot, by status."""
    return conn.execute(
        "SELECT s.status, COUNT(*) FROM externals e "
        "JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id "
        "GROUP BY s.status ORDER BY s.status"
    ).fetchall()


def tombstone_reason_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Head tombstone snapshots bucketed by their parsed reason, most common first."""
    by_body = conn.execute(
        "SELECT s.body, COUNT(*) FROM externals e "
        "JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id "
        "WHERE s.status = 'tombstone' GROUP BY s.body"
    ).fetchall()
    counts: dict[str, int] = {}
    for body, count in by_body:
        reason = parse_tombstone_reason(body)
        counts[reason] = counts.get(reason, 0) + count
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def empty_extract_raw_payload_retained_count(conn: sqlite3.Connection) -> int:
    """Head ``empty_extract`` tombstones that still retain a ``raw_payload``.

    The upper bound on pages a thawed lode-oni (headless-render retrieval)
    could actually re-render -- ``raw_html`` is kept on empty-extract
    tombstones (``lode.externals.ingest_fetch_result``), so this is a
    ``raw_payload IS NOT NULL`` count filtered to the ``empty_extract``
    reason specifically, not every tombstone.
    """
    body = tombstone_body("empty_extract")
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM externals e "
        "JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id "
        "WHERE s.status = 'tombstone' AND s.body = ? AND s.raw_payload IS NOT NULL",
        (body,),
    ).fetchone()
    return count


class NoteCounts(NamedTuple):
    total: int
    live: int
    deleted: int


def note_counts(conn: sqlite3.Connection) -> NoteCounts:
    """Notes total, and split live vs. deleted by head version's ``op``."""
    (total,) = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
    (live,) = conn.execute(
        "SELECT COUNT(*) FROM notes n JOIN versions v "
        "ON v.version_id = n.head_version_id WHERE v.op != 'delete'"
    ).fetchone()
    (deleted,) = conn.execute(
        "SELECT COUNT(*) FROM notes n JOIN versions v "
        "ON v.version_id = n.head_version_id WHERE v.op = 'delete'"
    ).fetchone()
    return NoteCounts(total=total, live=live, deleted=deleted)


class VersionChainStats(NamedTuple):
    total_versions: int
    max_depth: int
    avg_depth: float


def version_chain_stats(conn: sqlite3.Connection) -> VersionChainStats:
    """Total version rows, plus max/avg chain depth (versions per note).

    "Depth" here is the size of a note's whole version chain (every
    ``create``/``update``/``delete`` row for that ``note_id``), not merely
    its live head count -- a note with no versions at all cannot exist
    (``notes.head_version_id`` is populated atomically with the root
    version), so every note counted here has depth >= 1.
    """
    total_versions, max_depth, avg_depth = conn.execute(
        "SELECT SUM(depth), MAX(depth), AVG(depth) FROM "
        "(SELECT COUNT(*) AS depth FROM versions GROUP BY note_id)"
    ).fetchone()
    # All three are NULL on an empty ``versions`` table (no groups to aggregate).
    return VersionChainStats(
        total_versions=total_versions or 0,
        max_depth=max_depth or 0,
        avg_depth=avg_depth or 0.0,
    )


def externals_by_source_type(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """``(source_type, count)`` over ``externals``, ordered by source_type."""
    return conn.execute(
        "SELECT source_type, COUNT(*) FROM externals GROUP BY source_type "
        "ORDER BY source_type"
    ).fetchall()


def externals_no_egress_count(conn: sqlite3.Connection) -> int:
    """Count of ``externals`` rows with ``no_egress`` set."""
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM externals WHERE no_egress = 1"
    ).fetchone()
    return count


class PassageIndexStats(NamedTuple):
    passages: int
    embeddings: int
    targets_with_embeddings: int
    targets_without_embeddings: int


def passage_index_stats(conn: sqlite3.Connection) -> PassageIndexStats:
    """Passage/embedding counts, plus index coverage over ``passages.target_version``.

    ``target_version`` is polymorphic (a ``version_id`` or a ``snapshot_id``,
    ``schema.sql``) -- coverage is computed over that shared unit, not
    versions alone, so an externals-derived passage counts toward coverage
    the same way a note version's does.
    """
    (passages,) = conn.execute("SELECT COUNT(*) FROM passages").fetchone()
    (embeddings,) = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
    (total_targets,) = conn.execute(
        "SELECT COUNT(DISTINCT target_version) FROM passages"
    ).fetchone()
    (with_embeddings,) = conn.execute(
        "SELECT COUNT(DISTINCT p.target_version) FROM passages p "
        "JOIN embeddings e ON e.passage_id = p.passage_id"
    ).fetchone()
    return PassageIndexStats(
        passages=passages,
        embeddings=embeddings,
        targets_with_embeddings=with_embeddings,
        targets_without_embeddings=total_targets - with_embeddings,
    )


def edges_by_status(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """``(status, count)`` over ``edges``, ordered by status."""
    return conn.execute(
        "SELECT status, COUNT(*) FROM edges GROUP BY status ORDER BY status"
    ).fetchall()


def edges_by_source(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """``(source, count)`` over ``edges``, ordered by source."""
    return conn.execute(
        "SELECT source, COUNT(*) FROM edges GROUP BY source ORDER BY source"
    ).fetchall()
