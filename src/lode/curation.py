"""User curation of AI-derived annotations and edges: delete + pin (lode-npx.4).

``docs/storage.md`` "Provenance & user override": user corrections are pinned
to the logical target (``target``/``from_id`` = ``note_id`` or
``external_id``) and irreplaceable -- a tag or link a user removes must never
resurface just because the next enrichment pass produces the same suggestion
again.

**Mechanism.** A user delete does not physically remove the row. It converts
it in place to a **suppression tombstone**: ``source='user',
status='orphaned'``, keeping ``kind``/``payload`` (annotations) or
``from_id``/``to_id`` (edges) so a later :func:`lode.enrich._write_enrichment`
can recognise "the user already decided about this exact item" and skip
re-inserting the matching AI suggestion (see :func:`is_annotation_suppressed`
/ :func:`is_edge_suppressed`). Tombstones are never re-anchored
(:mod:`lode.staleness` only touches ``source='ai'`` rows) and never displayed
(:mod:`lode.display`).

This also covers deleting a *user-authored* row (one the user added
themselves, not an AI suggestion): it becomes a tombstone too, so a future
Haiku pass can't recreate the same item either -- once a human has an opinion
about a given ``(target, kind, payload)`` (or ``(from_id, to_id)``), one row
stays authoritative.
"""

import sqlite3


def delete_annotation(conn: sqlite3.Connection, annotation_id: int) -> None:
    """User-delete annotation ``annotation_id``: pin it as a suppression tombstone.

    Converts the row to ``source='user', status='orphaned'`` and clears
    ``source_version`` (it's no longer version-scoped AI output). Idempotent
    -- deleting an already-tombstoned row just re-writes the same state.
    Raises :class:`KeyError` if the id doesn't exist.
    """
    with conn:
        cur = conn.execute(
            "UPDATE annotations SET source = 'user', status = 'orphaned', "
            "source_version = NULL WHERE id = ?",
            (annotation_id,),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no such annotation: {annotation_id}")


def delete_edge(conn: sqlite3.Connection, edge_id: int) -> None:
    """User-delete edge ``edge_id``: pin it as a suppression tombstone.

    Converts the row to ``source='user', status='orphaned'`` and clears
    ``source_version``. Idempotent; raises :class:`KeyError` if the id
    doesn't exist.
    """
    with conn:
        cur = conn.execute(
            "UPDATE edges SET source = 'user', status = 'orphaned', "
            "source_version = NULL WHERE id = ?",
            (edge_id,),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no such edge: {edge_id}")


def is_annotation_suppressed(
    conn: sqlite3.Connection, target: str, kind: str, payload: str
) -> bool:
    """True if a user row already exists for this exact ``(target, kind, payload)``.

    Covers both a user-added annotation the AI shouldn't duplicate and a
    tombstoned (deleted) one the AI shouldn't resurrect -- either way, once a
    ``source='user'`` row exists for this item, the AI suggestion is skipped.
    ``payload`` must be the same JSON-encoded string
    :func:`lode.enrich._write_enrichment` would write, so the match is exact.
    """
    row = conn.execute(
        "SELECT 1 FROM annotations WHERE target = ? AND kind = ? AND payload = ? "
        "AND source = 'user' LIMIT 1",
        (target, kind, payload),
    ).fetchone()
    return row is not None


def is_edge_suppressed(conn: sqlite3.Connection, from_id: str, to_id: str) -> bool:
    """True if a user row already exists for this exact ``(from_id, to_id)`` edge."""
    row = conn.execute(
        "SELECT 1 FROM edges WHERE from_id = ? AND to_id = ? AND source = 'user' LIMIT 1",
        (from_id, to_id),
    ).fetchone()
    return row is not None
