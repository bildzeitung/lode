"""Structural staleness + re-anchor rules for AI-derived annotations and edges (lode-npx.3).

Staleness is structural: an AI annotation or edge goes stale when the note's
head pointer moves past the version it was derived from (``source_version !=
head_version_id``).  This module implements the re-anchor step that runs after a
note update to classify each AI-derived row against the new body.

Re-anchor rules
---------------
Span anchor: ``quoted_text`` (the verbatim text in the note body that the
annotation/edge was derived from) + version, **never** byte offsets.  Offsets
break on any edit; a quoted substring survives minor edits elsewhere.

With ``quoted_text`` set:

- ``quoted_text`` found verbatim in new body → **fresh** (``source_version``
  advanced to the new head version_id so the row tracks the head).
- ``quoted_text`` absent but anchor value (``payload`` string / ``to_id``)
  still present in body → **stale** (the exact quote changed context but the
  concept is still mentioned; ``source_version`` is NOT advanced).
- Both absent → **orphaned** (the annotation's subject is gone from the note).

Without ``quoted_text`` (whole-note items or rows written before lode-npx.3):

- Anchor value in body → **fresh** (``source_version`` advanced).
- Anchor value absent → **orphaned**.
  There is no "stale" state without a quoted anchor — the concept is either
  present or gone.

User annotations and edges (``source='user'``) are **never re-anchored**.  User
curation is irreplaceable and attaches to the logical note, not a specific
version.
"""

import json
import logging
import sqlite3

from lode.ids import short_version_id

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal classifier
# ---------------------------------------------------------------------------


def _classify(
    anchor_value: str,
    quoted_text: str | None,
    new_body: str,
) -> str:
    """Classify a single row against ``new_body``, returning its new status.

    - ``"fresh"``    — verbatim ``quoted_text`` match, or no ``quoted_text`` and
      anchor value found.  Fresh rows advance ``source_version`` to the new head.
    - ``"stale"``    — ``quoted_text`` gone but anchor value still present.
    - ``"orphaned"`` — both absent, or no ``quoted_text`` and anchor absent.
    """
    if quoted_text is not None:
        if quoted_text in new_body:
            return "fresh"
        if anchor_value in new_body:
            return "stale"
        return "orphaned"
    # No quoted_text: fall back to anchor value only (fresh or orphaned, no stale).
    if anchor_value in new_body:
        return "fresh"
    return "orphaned"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reanchor_annotations(
    conn: sqlite3.Connection,
    note_id: str,
    new_version_id: str,
    new_body: str,
) -> dict[str, int]:
    """Re-anchor AI annotations for ``note_id`` against the new head version body.

    Reads every ``source='ai'`` annotation targeting ``note_id``, applies
    re-anchor classification against ``new_body``, and writes the new ``status``
    (and ``source_version`` when the row is fresh) back. ``source='user'``
    annotations are never touched.

    Does **not** commit — the caller owns the transaction boundary, so this can
    be composed into a larger atomic write (e.g. :meth:`lode.repository.
    Repository.save`, which runs this inside its own ``with conn:``). A caller
    invoking this standalone is responsible for committing afterward.

    :param conn: Open SQLite connection.
    :param note_id: The note whose AI annotations should be re-anchored.
    :param new_version_id: The version_id the head just moved to; fresh rows
        advance their ``source_version`` to this value.
    :param new_body: The body text of the new head version.
    :returns: Count dict ``{"fresh": n, "stale": n, "orphaned": n}``.
    """
    rows = conn.execute(
        "SELECT id, payload, quoted_text FROM annotations "
        "WHERE target = ? AND source = 'ai'",
        (note_id,),
    ).fetchall()

    counts: dict[str, int] = {"fresh": 0, "stale": 0, "orphaned": 0}
    if not rows:
        return counts

    for row_id, payload_json, quoted_text in rows:
        anchor_value = str(json.loads(payload_json))
        new_status = _classify(anchor_value, quoted_text, new_body)
        if new_status == "fresh":
            conn.execute(
                "UPDATE annotations SET status = ?, source_version = ? WHERE id = ?",
                (new_status, new_version_id, row_id),
            )
        else:
            conn.execute(
                "UPDATE annotations SET status = ? WHERE id = ?",
                (new_status, row_id),
            )
        counts[new_status] += 1

    log.debug(
        "reanchor_annotations: note=%s new_ver=%s fresh=%d stale=%d orphaned=%d",
        note_id[:12],
        short_version_id(new_version_id),
        counts["fresh"],
        counts["stale"],
        counts["orphaned"],
    )
    return counts


def reanchor_edges(
    conn: sqlite3.Connection,
    note_id: str,
    new_version_id: str,
    new_body: str,
) -> dict[str, int]:
    """Re-anchor AI edges from ``note_id`` against the new head version body.

    Reads every ``source='ai'`` edge where ``from_id = note_id``, applies
    re-anchor classification against ``new_body`` using ``to_id`` as the anchor
    value, and writes the new ``status`` (and ``source_version`` when fresh)
    back. ``source='user'`` edges are never touched.

    Does **not** commit — the caller owns the transaction boundary, so this can
    be composed into a larger atomic write (e.g. :meth:`lode.repository.
    Repository.save`, which runs this inside its own ``with conn:``). A caller
    invoking this standalone is responsible for committing afterward.

    :param conn: Open SQLite connection.
    :param note_id: The note whose AI edges should be re-anchored.
    :param new_version_id: The version_id the head just moved to; fresh rows
        advance their ``source_version`` to this value.
    :param new_body: The body text of the new head version.
    :returns: Count dict ``{"fresh": n, "stale": n, "orphaned": n}``.
    """
    rows = conn.execute(
        "SELECT id, to_id, quoted_text FROM edges WHERE from_id = ? AND source = 'ai'",
        (note_id,),
    ).fetchall()

    counts: dict[str, int] = {"fresh": 0, "stale": 0, "orphaned": 0}
    if not rows:
        return counts

    for row_id, to_id, quoted_text in rows:
        new_status = _classify(to_id, quoted_text, new_body)
        if new_status == "fresh":
            conn.execute(
                "UPDATE edges SET status = ?, source_version = ? WHERE id = ?",
                (new_status, new_version_id, row_id),
            )
        else:
            conn.execute(
                "UPDATE edges SET status = ? WHERE id = ?",
                (new_status, row_id),
            )
        counts[new_status] += 1

    log.debug(
        "reanchor_edges: note=%s new_ver=%s fresh=%d stale=%d orphaned=%d",
        note_id[:12],
        short_version_id(new_version_id),
        counts["fresh"],
        counts["stale"],
        counts["orphaned"],
    )
    return counts
