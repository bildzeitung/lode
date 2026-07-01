"""Stale-display policy for AI-derived annotations and edges (lode-npx.4).

``docs/storage.md`` "Stale-display policy (decided)":

- **Tags / links:** show, but flagged stale (avoids UI flicker on every typo fix).
- **Assertive items** (extracted action items, etc.): hide until re-enrichment
  is fresh -- the cost of a wrong action item is higher than a wrong tag.

This module is the one place that turns an annotation/edge row's
``(kind, source, status)`` into a display decision, so every consumer (the
CLI, and later the TUI, E11) applies the same rule rather than re-deriving
it. It is read-only -- it never writes the DB.

``source='user'`` rows with ``status='orphaned'`` are curation *tombstones*:
:mod:`lode.curation` converts a deleted annotation/edge into one so a future
enrichment pass never re-adds it (``docs/storage.md`` "Provenance & user
override"). A tombstone is pinning state, not a real item -- it is never
shown.
"""

import json
import sqlite3
from dataclasses import dataclass

#: Kinds whose fresh signal is load-bearing enough that a non-fresh reading
#: should hide the item entirely rather than show-with-flag (``docs/
#: storage.md`` "Stale-display policy"; ``docs/configuration.md`` "Build
#: constants"). No extractor produces any of these kinds yet -- lode-npx.1's
#: Haiku enrichment only emits ``tag``/``entity`` annotations and inferred
#: edges, all of which are shown-flagged, not hidden. This is the policy's
#: forward-compatible hook for when action-item extraction lands.
ASSERTIVE_KINDS = frozenset({"action_item"})


@dataclass(frozen=True)
class DisplayDecision:
    """Whether to show a row, and whether to flag it stale."""

    visible: bool
    stale: bool


def classify_annotation_display(kind: str, source: str, status: str) -> DisplayDecision:
    """Apply the stale-display policy to a single annotation row.

    - ``source='user'`` + ``status='orphaned'`` -- a curation tombstone
      (:mod:`lode.curation`); never visible.
    - ``kind`` in :data:`ASSERTIVE_KINDS` and ``status != 'fresh'`` -- hidden
      until re-enrichment produces a fresh row.
    - everything else -- visible; flagged ``stale`` when ``status != 'fresh'``.
    """
    if source == "user" and status == "orphaned":
        return DisplayDecision(visible=False, stale=False)
    if kind in ASSERTIVE_KINDS and status != "fresh":
        return DisplayDecision(visible=False, stale=True)
    return DisplayDecision(visible=True, stale=status != "fresh")


def classify_edge_display(source: str, status: str) -> DisplayDecision:
    """Apply the stale-display policy to a single edge row.

    Edges are the knowledge graph's "links" -- always in the show-flagged
    category (never assertive), so the only hide case is a ``source='user'``
    ``status='orphaned'`` tombstone (:mod:`lode.curation`).
    """
    if source == "user" and status == "orphaned":
        return DisplayDecision(visible=False, stale=False)
    return DisplayDecision(visible=True, stale=status != "fresh")


def display_annotations(conn: sqlite3.Connection, target: str) -> list[dict]:
    """Read every annotation for ``target`` and apply the stale-display policy.

    Returns one dict per row the policy says should be shown -- tombstones
    and hidden assertive items are dropped -- each with ``id``, ``kind``,
    ``payload`` (decoded from its JSON encoding), ``source``, ``status``, and
    the policy's ``stale`` flag.
    """
    rows = conn.execute(
        "SELECT id, kind, payload, source, status FROM annotations WHERE target = ?",
        (target,),
    ).fetchall()
    out = []
    for row_id, kind, payload_json, source, status in rows:
        decision = classify_annotation_display(kind, source, status)
        if not decision.visible:
            continue
        out.append(
            {
                "id": row_id,
                "kind": kind,
                "payload": json.loads(payload_json),
                "source": source,
                "status": status,
                "stale": decision.stale,
            }
        )
    return out


def display_edges(conn: sqlite3.Connection, from_id: str) -> list[dict]:
    """Read every edge from ``from_id`` and apply the stale-display policy.

    Returns one dict per row the policy says should be shown -- tombstones
    are dropped -- each with ``id``, ``to_id``, ``source``, ``reason``,
    ``confidence``, ``status``, and the policy's ``stale`` flag.
    """
    rows = conn.execute(
        "SELECT id, to_id, source, reason, confidence, status "
        "FROM edges WHERE from_id = ?",
        (from_id,),
    ).fetchall()
    out = []
    for row_id, to_id, source, reason, confidence, status in rows:
        decision = classify_edge_display(source, status)
        if not decision.visible:
            continue
        out.append(
            {
                "id": row_id,
                "to_id": to_id,
                "source": source,
                "reason": reason,
                "confidence": confidence,
                "status": status,
                "stale": decision.stale,
            }
        )
    return out
