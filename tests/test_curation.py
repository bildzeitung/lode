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
# delete_annotation / delete_edge
# ---------------------------------------------------------------------------


def test_delete_annotation_converts_to_user_orphaned_tombstone(
    conn: sqlite3.Connection,
) -> None:
    row_id = _insert_annotation(conn, source="ai", status="fresh")

    delete_annotation(conn, row_id)

    row = _annotation_row(conn, row_id)
    assert row["source"] == "user"
    assert row["status"] == "orphaned"
    assert row["source_version"] is None
    # target/kind/payload survive so future suppression matching still works.
    assert row["target"] == "note-1"
    assert row["kind"] == "tag"
    assert row["payload"] == "python"


def test_delete_annotation_is_idempotent(conn: sqlite3.Connection) -> None:
    row_id = _insert_annotation(conn)
    delete_annotation(conn, row_id)
    delete_annotation(conn, row_id)  # no error, no change in outcome
    row = _annotation_row(conn, row_id)
    assert row["source"] == "user"
    assert row["status"] == "orphaned"


def test_delete_annotation_missing_id_raises_keyerror(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        delete_annotation(conn, 999)


def test_delete_edge_converts_to_user_orphaned_tombstone(
    conn: sqlite3.Connection,
) -> None:
    row_id = _insert_edge(conn, source="ai", status="fresh")

    delete_edge(conn, row_id)

    row = _edge_row(conn, row_id)
    assert row["source"] == "user"
    assert row["status"] == "orphaned"
    assert row["source_version"] is None
    assert row["from_id"] == "note-1"
    assert row["to_id"] == "jwt-topic"


def test_delete_edge_missing_id_raises_keyerror(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        delete_edge(conn, 999)


# ---------------------------------------------------------------------------
# is_annotation_suppressed / is_edge_suppressed
# ---------------------------------------------------------------------------


def test_annotation_not_suppressed_when_no_user_row(conn: sqlite3.Connection) -> None:
    assert (
        is_annotation_suppressed(conn, "note-1", "tag", json.dumps("python")) is False
    )


def test_annotation_suppressed_after_delete(conn: sqlite3.Connection) -> None:
    row_id = _insert_annotation(conn, kind="tag", payload_value="python", source="ai")
    delete_annotation(conn, row_id)

    assert is_annotation_suppressed(conn, "note-1", "tag", json.dumps("python")) is True


def test_annotation_suppressed_by_user_authored_row(conn: sqlite3.Connection) -> None:
    """A user-added annotation also suppresses a matching future AI duplicate."""
    _insert_annotation(
        conn, kind="tag", payload_value="python", source="user", status="fresh"
    )

    assert is_annotation_suppressed(conn, "note-1", "tag", json.dumps("python")) is True


def test_edge_not_suppressed_when_no_user_row(conn: sqlite3.Connection) -> None:
    assert is_edge_suppressed(conn, "note-1", "jwt-topic") is False


def test_edge_suppressed_after_delete(conn: sqlite3.Connection) -> None:
    row_id = _insert_edge(conn, to_id="jwt-topic", source="ai")
    delete_edge(conn, row_id)

    assert is_edge_suppressed(conn, "note-1", "jwt-topic") is True
