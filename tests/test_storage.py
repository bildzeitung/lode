"""Tests for lode.storage / schema.sql — the data-shape schema (lode-s2f.1).

Asserts the acceptance criteria: schema.sql creates every data-shape table in a
fresh WAL DB, and a round-trip insert/select on notes+versions succeeds.
"""

import sqlite3
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


# ---------------------------------------------------------------------------
# lode-pig: forward migration for jobs.next_attempt_at
# ---------------------------------------------------------------------------


def test_next_attempt_at_migrated_onto_pre_existing_jobs_table(tmp_path: Path) -> None:
    """A jobs table created before next_attempt_at landed is migrated and backfilled.

    Reproduces the `lode work` crash (OperationalError: no such column:
    next_attempt_at): CREATE TABLE IF NOT EXISTS won't add the column to an
    existing table, so init_db's _apply_migrations must. The backfill sets
    next_attempt_at = created so the pre-existing job stays due (a NULL would
    fail the `next_attempt_at <= now` claim predicate and vanish).
    """
    db = tmp_path / "lode.db"
    # The original jobs table (commit 5c8a189): prompt_ver/batch_handle present,
    # next_attempt_at/claimed_at not yet added. Modelled exactly so init_db's
    # executescript (which builds the prompt_ver idempotency index) succeeds —
    # matching the real DBs that hit the crash on the next_attempt_at SELECT.
    seed = sqlite3.connect(db)
    seed.execute(
        "CREATE TABLE jobs ("
        "  id INTEGER PRIMARY KEY,"
        "  type TEXT NOT NULL,"
        "  target_version TEXT NOT NULL,"
        "  prompt_ver TEXT,"
        "  status TEXT NOT NULL DEFAULT 'pending',"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  last_error TEXT,"
        "  batch_handle TEXT,"
        "  created TEXT NOT NULL DEFAULT '2026-01-01T00:00:00.000Z'"
        ")"
    )
    seed.execute("INSERT INTO jobs (type, target_version) VALUES ('enrich', 'ver-1')")
    seed.commit()
    seed.close()

    conn = init_db(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "next_attempt_at" in cols, "migration must add jobs.next_attempt_at"

        # The pre-existing row is backfilled (non-NULL) and due now.
        due = conn.execute(
            "SELECT count(*) FROM jobs "
            "WHERE next_attempt_at IS NOT NULL "
            "AND next_attempt_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()[0]
        assert due == 1, "backfilled job must be visible to the claim predicate"
    finally:
        conn.close()

    # Idempotent: re-running init_db on the migrated DB must not raise.
    conn2 = init_db(db)
    conn2.close()
