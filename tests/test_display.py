"""Tests for lode.display -- stale-display policy (lode-npx.4).

Acceptance criteria (bd show lode-npx.4):
- Stale tags/links show flagged while assertive items hide.

Strategy: the pure classifiers (`classify_annotation_display` /
`classify_edge_display`) are tested directly against every (kind, source,
status) combination the policy distinguishes; the DB-reading wrappers
(`display_annotations` / `display_edges`) are tested against a real SQLite DB
(init_db) to exercise the actual row shape end to end.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from lode.display import (
    ASSERTIVE_KINDS,
    classify_annotation_display,
    classify_edge_display,
    display_annotations,
    display_edges,
)
from lode.storage import init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


def _insert_annotation(
    conn: sqlite3.Connection,
    *,
    target: str = "note-1",
    kind: str = "tag",
    payload_value: str = "python",
    source: str = "ai",
    status: str = "fresh",
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO annotations (target, kind, payload, source, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (target, kind, json.dumps(payload_value), source, status),
        )
        return cur.lastrowid  # type: ignore[return-value]


def _insert_edge(
    conn: sqlite3.Connection,
    *,
    from_id: str = "note-1",
    to_id: str = "jwt-topic",
    source: str = "ai",
    status: str = "fresh",
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, status) "
            "VALUES (?, ?, ?, 'test reason', 0.8, ?)",
            (from_id, to_id, source, status),
        )
        return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# classify_annotation_display / classify_edge_display -- pure classifiers
#
# Non-assertive annotation kinds (tag, entity) and edges are symmetric: all
# three are always visible regardless of status, only the `stale` flag moves.
# classify_edge_display has no `kind` parameter (edges are never assertive),
# so each classifier is wrapped down to a shared (source, status) call shape
# -- the same "normalize the call, share the body" technique used for
# reanchor_annotations/reanchor_edges in test_staleness.py. lode-b4w.3: this
# folds the file's original tag/entity/ai_edge trio (each already
# status-parametrized) into one test: 3 -> 1 (now a kind x status table).
# The same wrappers also fold the user-sourced annotation/edge pairs below:
# 2 -> 1 each, no assertion dropped from any of the five originals.
# ---------------------------------------------------------------------------


def _classify_tag(source: str, status: str):
    return classify_annotation_display("tag", source, status)


def _classify_entity(source: str, status: str):
    return classify_annotation_display("entity", source, status)


NON_ASSERTIVE_KINDS = [
    pytest.param(_classify_tag, id="tag"),
    pytest.param(_classify_entity, id="entity"),
    pytest.param(classify_edge_display, id="ai_edge"),
]

# Only the annotation/edge pair -- user-sourced curation tests never varied
# kind (fixed at "tag" in the originals), so entity isn't part of this axis.
ANNOTATION_AND_EDGE = [
    pytest.param(_classify_tag, id="annotation"),
    pytest.param(classify_edge_display, id="edge"),
]


@pytest.mark.parametrize("classify_fn", NON_ASSERTIVE_KINDS)
@pytest.mark.parametrize("status", ["fresh", "stale", "orphaned"])
def test_non_assertive_kind_is_always_visible(classify_fn, status: str) -> None:
    """Tags, entities, and edges (non-assertive) show at every status -- only
    the stale flag moves."""
    decision = classify_fn("ai", status)
    assert decision.visible is True
    assert decision.stale == (status != "fresh")


def test_assertive_kind_visible_when_fresh() -> None:
    kind = next(iter(ASSERTIVE_KINDS))
    decision = classify_annotation_display(kind, "ai", "fresh")
    assert decision.visible is True
    assert decision.stale is False


@pytest.mark.parametrize("status", ["stale", "orphaned"])
def test_assertive_kind_hidden_when_not_fresh(status: str) -> None:
    """Assertive items (action items) hide until re-enrichment is fresh."""
    kind = next(iter(ASSERTIVE_KINDS))
    decision = classify_annotation_display(kind, "ai", status)
    assert decision.visible is False


@pytest.mark.parametrize("classify_fn", ANNOTATION_AND_EDGE)
def test_user_fresh_is_visible_not_stale(classify_fn) -> None:
    decision = classify_fn("user", "fresh")
    assert decision.visible is True
    assert decision.stale is False


@pytest.mark.parametrize("classify_fn", ANNOTATION_AND_EDGE)
def test_user_orphaned_is_a_hidden_tombstone(classify_fn) -> None:
    """source='user' + status='orphaned' is a curation tombstone -- never shown."""
    decision = classify_fn("user", "orphaned")
    assert decision.visible is False


# ---------------------------------------------------------------------------
# display_annotations / display_edges -- DB-reading wrappers
# ---------------------------------------------------------------------------


def test_display_annotations_flags_stale_tags_but_keeps_them_visible(
    conn: sqlite3.Connection,
) -> None:
    _insert_annotation(conn, kind="tag", payload_value="fresh-tag", status="fresh")
    _insert_annotation(conn, kind="tag", payload_value="stale-tag", status="stale")
    _insert_annotation(
        conn, kind="tag", payload_value="orphaned-tag", status="orphaned"
    )

    rows = display_annotations(conn, "note-1")

    assert len(rows) == 3
    by_payload = {r["payload"]: r for r in rows}
    assert by_payload["fresh-tag"]["stale"] is False
    assert by_payload["stale-tag"]["stale"] is True
    assert by_payload["orphaned-tag"]["stale"] is True


def test_display_annotations_hides_stale_assertive_items(
    conn: sqlite3.Connection,
) -> None:
    kind = next(iter(ASSERTIVE_KINDS))
    _insert_annotation(conn, kind=kind, payload_value="call the vendor", status="fresh")
    _insert_annotation(conn, kind=kind, payload_value="stale action", status="stale")

    rows = display_annotations(conn, "note-1")

    payloads = {r["payload"] for r in rows}
    assert "call the vendor" in payloads
    assert "stale action" not in payloads


def test_display_annotations_hides_user_deletion_tombstones(
    conn: sqlite3.Connection,
) -> None:
    _insert_annotation(
        conn, kind="tag", payload_value="deleted-tag", source="user", status="orphaned"
    )
    _insert_annotation(
        conn, kind="tag", payload_value="kept-tag", source="ai", status="fresh"
    )

    rows = display_annotations(conn, "note-1")

    payloads = {r["payload"] for r in rows}
    assert "deleted-tag" not in payloads
    assert "kept-tag" in payloads


def test_display_edges_flags_stale_but_keeps_visible(conn: sqlite3.Connection) -> None:
    _insert_edge(conn, to_id="fresh-topic", status="fresh")
    _insert_edge(conn, to_id="stale-topic", status="stale")

    rows = display_edges(conn, "note-1")

    by_to = {r["to_id"]: r for r in rows}
    assert by_to["fresh-topic"]["stale"] is False
    assert by_to["stale-topic"]["stale"] is True


def test_display_edges_hides_user_deletion_tombstones(conn: sqlite3.Connection) -> None:
    _insert_edge(conn, to_id="deleted-topic", source="user", status="orphaned")
    _insert_edge(conn, to_id="kept-topic", source="ai", status="fresh")

    rows = display_edges(conn, "note-1")

    to_ids = {r["to_id"] for r in rows}
    assert "deleted-topic" not in to_ids
    assert "kept-topic" in to_ids
