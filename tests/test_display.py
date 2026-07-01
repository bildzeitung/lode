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
# classify_annotation_display -- pure classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["fresh", "stale", "orphaned"])
def test_tag_is_always_visible(status: str) -> None:
    """Tags (non-assertive) show at every status -- only the stale flag moves."""
    decision = classify_annotation_display("tag", "ai", status)
    assert decision.visible is True
    assert decision.stale == (status != "fresh")


@pytest.mark.parametrize("status", ["fresh", "stale", "orphaned"])
def test_entity_is_always_visible(status: str) -> None:
    decision = classify_annotation_display("entity", "ai", status)
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


def test_user_orphaned_annotation_is_a_hidden_tombstone() -> None:
    """source='user' + status='orphaned' is a curation tombstone -- never shown."""
    decision = classify_annotation_display("tag", "user", "orphaned")
    assert decision.visible is False


def test_user_fresh_annotation_is_visible_not_stale() -> None:
    decision = classify_annotation_display("tag", "user", "fresh")
    assert decision.visible is True
    assert decision.stale is False


# ---------------------------------------------------------------------------
# classify_edge_display -- pure classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["fresh", "stale", "orphaned"])
def test_ai_edge_is_always_visible(status: str) -> None:
    """Edges (links) are never assertive -- always shown, flagged when not fresh."""
    decision = classify_edge_display("ai", status)
    assert decision.visible is True
    assert decision.stale == (status != "fresh")


def test_user_orphaned_edge_is_a_hidden_tombstone() -> None:
    decision = classify_edge_display("user", "orphaned")
    assert decision.visible is False


def test_user_fresh_edge_is_visible_not_stale() -> None:
    decision = classify_edge_display("user", "fresh")
    assert decision.visible is True
    assert decision.stale is False


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
