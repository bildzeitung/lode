"""Open a lode SQLite database and apply the data-shape schema.

The schema itself lives in :data:`schema.sql` (the full data shape from
``docs/storage.md`` §8); this module is the thin seam that loads it onto a fresh
or existing database. It is deliberately minimal — the version-chain, queue, and
repository logic land in later storage-core tickets; this just guarantees a
correctly-initialised file.

Two settings the schema can't carry on its own:

- ``PRAGMA journal_mode = WAL`` is persistent (stored in the DB header) so it
  lives in ``schema.sql``.
- ``PRAGMA foreign_keys`` is **per-connection** and is *not* persisted, so
  :func:`init_db` sets it on every connection it returns. The schema's
  ``DEFERRABLE INITIALLY DEFERRED`` foreign keys only bite when it is on.

Forward-only column migrations (columns added after a table was first deployed)
are applied by :func:`_apply_migrations` after the schema DDL runs.  SQLite does
not support ``ALTER TABLE … ADD COLUMN IF NOT EXISTS``, so the function catches
the ``OperationalError`` that a re-run raises and treats it as a no-op.
"""

import sqlite3
from importlib import resources
from pathlib import Path

_SCHEMA_RESOURCE = "schema.sql"


def schema_sql() -> str:
    """Return the packaged data-shape DDL (``schema.sql``)."""
    return (resources.files("lode") / _SCHEMA_RESOURCE).read_text(encoding="utf-8")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply forward-only column additions for databases created before a column landed.

    ``CREATE TABLE IF NOT EXISTS`` in ``schema.sql`` is idempotent for new
    databases — the column definition is included from the start — but does not
    add new columns to tables that already exist.  This function fills that gap
    for any column added to an existing table after its initial deployment.

    Each ``ALTER TABLE`` is wrapped in its own try/except so a partially-migrated
    database (one column already added, another not yet) is handled gracefully.
    """
    _migrations = [
        # lode-npx.3: span-anchor for re-anchor rules (staleness.py)
        "ALTER TABLE annotations ADD COLUMN quoted_text TEXT",
        "ALTER TABLE edges ADD COLUMN quoted_text TEXT",
        # lode-aor: crash-reclaim signal for jobs stuck in status='running'
        "ALTER TABLE jobs ADD COLUMN claimed_at TEXT",
        # lode-pig: backoff schedule for jobs (2f2379d added it to CREATE TABLE
        # only). Added nullable — SQLite rejects ADD COLUMN with the schema's
        # strftime() expression default ("Cannot add a column with non-constant
        # default"). Backfilled below so pre-existing rows are not left NULL
        # (NULL fails the ``next_attempt_at <= now`` claim predicate → invisible).
        "ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT",
    ]
    for ddl in _migrations:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already present — idempotent

    # Backfill next_attempt_at for rows that predate the column. ``created`` is a
    # past timestamp, so migrated jobs become immediately due (correct — they
    # were enqueued before the crash). No-op once every row is non-NULL.
    conn.execute(
        "UPDATE jobs SET next_attempt_at = created WHERE next_attempt_at IS NULL"
    )


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Open ``db_path`` (creating the file if absent) and apply the schema.

    Enables foreign-key enforcement on the connection, applies the full
    data-shape schema in WAL mode, runs forward-only column migrations for
    existing databases, and commits. Idempotent — every statement is
    ``CREATE … IF NOT EXISTS`` (plus migration guards), so re-running on an
    existing database is a no-op. Returns the open connection for the caller to
    use and close.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql())
    _apply_migrations(conn)
    conn.commit()
    return conn
