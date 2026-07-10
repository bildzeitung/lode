"""Tests for lode.curation -- user delete + pin as a suppression tombstone (lode-npx.4).

Acceptance criteria (bd show lode-npx.4): source:user annotations pin to
note_id and survive re-enrichment (a deleted link is not re-added).

Strategy: all tests use a real SQLite DB (init_db) to exercise the actual
schema constraints (the CHECK on `source`/`status` in particular -- a
suppression tombstone must be a legal row under the existing schema, no
migration).
"""

import json
import sqlite3
from pathlib import Path

import pytest

from lode.curation import (
    delete_annotation,
    delete_edge,
    is_annotation_suppressed,
    is_edge_suppressed,
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
    source_version: str = "ver-1",
    kind: str = "tag",
    payload_value: str = "python",
    source: str = "ai",
    status: str = "fresh",
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (target, source_version, kind, json.dumps(payload_value), source, status),
        )
        return cur.lastrowid  # type: ignore[return-value]


def _insert_edge(
    conn: sqlite3.Connection,
    *,
    from_id: str = "note-1",
    to_id: str = "jwt-topic",
    source_version: str = "ver-1",
    source: str = "ai",
    status: str = "fresh",
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO edges "
            "(from_id, to_id, source, reason, confidence, source_version, status) "
            "VALUES (?, ?, ?, 'test reason', 0.8, ?, ?)",
            (from_id, to_id, source, source_version, status),
        )
        return cur.lastrowid  # type: ignore[return-value]


def _annotation_row(conn: sqlite3.Connection, row_id: int) -> dict:
    r = conn.execute(
        "SELECT target, kind, payload, source, status, source_version "
        "FROM annotations WHERE id = ?",
        (row_id,),
    ).fetchone()
    return {
        "target": r[0],
        "kind": r[1],
        "payload": json.loads(r[2]),
        "source": r[3],
        "status": r[4],
        "source_version": r[5],
    }


def _edge_row(conn: sqlite3.Connection, row_id: int) -> dict:
    r = conn.execute(
        "SELECT from_id, to_id, source, status, source_version FROM edges WHERE id = ?",
        (row_id,),
    ).fetchone()
    return {
        "from_id": r[0],
        "to_id": r[1],
        "source": r[2],
        "status": r[3],
        "source_version": r[4],
    }


# ---------------------------------------------------------------------------
# delete_annotation / delete_edge -- mirrored semantics (lode-b4w.3).
#
# Both convert an ai-sourced row to a source='user'/status='orphaned'
# tombstone, both raise KeyError for a missing id -- verified by reading
# src/lode/curation.py, not just name matching. Consolidated via the same
# "normalize the call, share the body" technique as test_staleness.py's
# reanchor_annotations/reanchor_edges: wrap each side's insert/row-read/
# identity-check so one parametrized body covers what were 4 near-identical
# tests (2 converts-to-tombstone + 2 missing-id), no assertion dropped.
# ---------------------------------------------------------------------------


def _check_annotation_identity_survives(row: dict) -> None:
    assert row["target"] == "note-1"
    assert row["kind"] == "tag"
    assert row["payload"] == "python"


def _check_edge_identity_survives(row: dict) -> None:
    assert row["from_id"] == "note-1"
    assert row["to_id"] == "jwt-topic"


DELETE_TARGETS = [
    pytest.param(
        _insert_annotation,
        delete_annotation,
        _annotation_row,
        _check_annotation_identity_survives,
        id="annotation",
    ),
    pytest.param(
        _insert_edge,
        delete_edge,
        _edge_row,
        _check_edge_identity_survives,
        id="edge",
    ),
]


@pytest.mark.parametrize("insert_fn, delete_fn, row_fn, check_identity", DELETE_TARGETS)
def test_delete_converts_to_user_orphaned_tombstone(
    conn: sqlite3.Connection, insert_fn, delete_fn, row_fn, check_identity
) -> None:
    row_id = insert_fn(conn, source="ai", status="fresh")

    delete_fn(conn, row_id)

    row = row_fn(conn, row_id)
    assert row["source"] == "user"
    assert row["status"] == "orphaned"
    assert row["source_version"] is None
    # identifying fields survive so future suppression matching still works.
    check_identity(row)


def test_delete_annotation_is_idempotent(conn: sqlite3.Connection) -> None:
    """No edge-side counterpart exists (delete_edge has no distinct idempotency
    test) -- a pre-existing gap, out of scope for this consolidation ticket."""
    row_id = _insert_annotation(conn)
    delete_annotation(conn, row_id)
    delete_annotation(conn, row_id)  # no error, no change in outcome
    row = _annotation_row(conn, row_id)
    assert row["source"] == "user"
    assert row["status"] == "orphaned"


@pytest.mark.parametrize(
    "delete_fn", [delete_annotation, delete_edge], ids=["annotation", "edge"]
)
def test_delete_missing_id_raises_keyerror(conn: sqlite3.Connection, delete_fn) -> None:
    with pytest.raises(KeyError):
        delete_fn(conn, 999)


# ---------------------------------------------------------------------------
# is_annotation_suppressed / is_edge_suppressed -- mirrored semantics.
#
# Both check "does a source='user' row already exist for this exact
# identity" (confirmed by reading src/lode/curation.py: same one-line SELECT
# shape, different WHERE columns) -- consolidated the same way as the delete
# pair above.
# ---------------------------------------------------------------------------


def _annotation_suppressed(conn: sqlite3.Connection) -> bool:
    return is_annotation_suppressed(conn, "note-1", "tag", json.dumps("python"))


def _edge_suppressed(conn: sqlite3.Connection) -> bool:
    return is_edge_suppressed(conn, "note-1", "jwt-topic")


SUPPRESSED_CHECKS = [
    pytest.param(_annotation_suppressed, id="annotation"),
    pytest.param(_edge_suppressed, id="edge"),
]

SUPPRESSION_TARGETS = [
    pytest.param(
        _insert_annotation, delete_annotation, _annotation_suppressed, id="annotation"
    ),
    pytest.param(_insert_edge, delete_edge, _edge_suppressed, id="edge"),
]

SUPPRESSED_CHECKS_WITH_INSERT = [
    pytest.param(_insert_annotation, _annotation_suppressed, id="annotation"),
    pytest.param(_insert_edge, _edge_suppressed, id="edge"),
]


@pytest.mark.parametrize("is_suppressed", SUPPRESSED_CHECKS)
def test_not_suppressed_when_no_user_row(
    conn: sqlite3.Connection, is_suppressed
) -> None:
    assert is_suppressed(conn) is False


@pytest.mark.parametrize("insert_fn, delete_fn, is_suppressed", SUPPRESSION_TARGETS)
def test_suppressed_after_delete(
    conn: sqlite3.Connection, insert_fn, delete_fn, is_suppressed
) -> None:
    row_id = insert_fn(conn, source="ai")
    delete_fn(conn, row_id)

    assert is_suppressed(conn) is True


# lode-b4w.1's checklist flagged that this annotation-side test had no
# edge-side counterpart and asked to check whether that's a real coverage
# gap before consolidating. It is: is_edge_suppressed (src/lode/curation.py)
# runs the identical "any source='user' row, regardless of status" check as
# is_annotation_suppressed -- a user-authored (never-deleted) edge suppresses
# a future AI duplicate exactly like a user-authored annotation does. Closing
# the gap here (rather than dropping the annotation-side check) so the
# combined test still covers both original assertions plus the previously
# untested edge case.
@pytest.mark.parametrize("insert_fn, is_suppressed", SUPPRESSED_CHECKS_WITH_INSERT)
def test_suppressed_by_user_authored_row(
    conn: sqlite3.Connection, insert_fn, is_suppressed
) -> None:
    """A user-added row also suppresses a matching future AI duplicate."""
    insert_fn(conn, source="user", status="fresh")

    assert is_suppressed(conn) is True
