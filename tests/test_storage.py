"""Tests for lode.storage / schema.sql — the data-shape schema (lode-s2f.1).

Asserts the acceptance criteria: schema.sql creates every data-shape table in a
fresh WAL DB, and a round-trip insert/select on notes+versions succeeds.
"""

from pathlib import Path

import pytest

from lode.storage import init_db, schema_sql

# Every table in the docs/storage.md §8 data shape.
DATA_SHAPE_TABLES = {
    "notes",
    "versions",
    "externals",
    "snapshots",
    "annotations",
    "passages",
    "embeddings",
    "edges",
    "jobs",
    "egress_log",
}


def test_fresh_db_is_wal(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_schema_creates_every_data_shape_table(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        tables = {name for (name,) in rows}
        assert DATA_SHAPE_TABLES <= tables
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "lode.db"
    init_db(db).close()
    # Re-applying onto an existing file must not raise.
    conn = init_db(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        assert DATA_SHAPE_TABLES <= {name for (name,) in rows}
    finally:
        conn.close()


def test_round_trip_note_and_version(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        # Atomic save: insert the note (head pointing at a not-yet-written
        # version, allowed by the DEFERRABLE FK) and its root version, then
        # commit — the real create path.
        conn.execute(
            "INSERT INTO notes (note_id, head_version_id) VALUES (?, ?)",
            ("note-1", "ver-1"),
        )
        conn.execute(
            "INSERT INTO versions (version_id, note_id, parent_version_id, body, op) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ver-1", "note-1", None, "hello world", "create"),
        )
        conn.commit()

        body, op = conn.execute(
            "SELECT v.body, v.op FROM notes n "
            "JOIN versions v ON v.version_id = n.head_version_id "
            "WHERE n.note_id = ?",
            ("note-1",),
        ).fetchone()
        assert body == "hello world"
        assert op == "create"
    finally:
        conn.close()


def test_check_constraint_rejects_bad_op(tmp_path: Path) -> None:
    import sqlite3

    conn = init_db(tmp_path / "lode.db")
    try:
        conn.execute(
            "INSERT INTO notes (note_id, head_version_id) VALUES (?, ?)",
            ("note-1", "ver-1"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO versions (version_id, note_id, body, op) "
                "VALUES (?, ?, ?, ?)",
                ("ver-1", "note-1", "x", "frobnicate"),
            )
    finally:
        conn.rollback()
        conn.close()


def test_schema_sql_is_packaged() -> None:
    # The DDL is loadable as package data (not just a repo file).
    assert "CREATE TABLE IF NOT EXISTS notes" in schema_sql()
