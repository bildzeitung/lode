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
"""

import sqlite3
from importlib import resources
from pathlib import Path

_SCHEMA_RESOURCE = "schema.sql"


def schema_sql() -> str:
    """Return the packaged data-shape DDL (``schema.sql``)."""
    return (resources.files("lode") / _SCHEMA_RESOURCE).read_text(encoding="utf-8")


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Open ``db_path`` (creating the file if absent) and apply the schema.

    Enables foreign-key enforcement on the connection, applies the full
    data-shape schema in WAL mode, and commits. Idempotent — every statement is
    ``CREATE … IF NOT EXISTS``, so re-running on an existing database is a no-op.
    Returns the open connection for the caller to use and close.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql())
    conn.commit()
    return conn
