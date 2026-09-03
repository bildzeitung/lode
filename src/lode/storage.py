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

Some changes cannot be expressed as a bare ``ADD COLUMN`` at all — SQLite has no
``ALTER TABLE … ALTER COLUMN`` / ``DROP CONSTRAINT``, so widening a ``CHECK`` or
relaxing a ``NOT NULL`` requires rebuilding the table (create the new shape, copy
every row across, drop the old table, rename). That is a multi-statement,
non-idempotent-by-accident operation, so it needs real bookkeeping rather than
the catch-and-ignore trick above: :func:`_apply_versioned_migrations` gates each
such step behind SQLite's built-in ``PRAGMA user_version`` (an integer stored in
the DB header, defaulting to 0) — a migration numbered *N* runs only while
``user_version < N``, and bumps it to *N* immediately after (lode-35nu.11.7, the
first user of this mechanism: the ``egress_log`` rebuild for ``purpose='tool'``).
Each step is atomic with its own version bump; :func:`init_db` decides which
databases run them at all.

The two mechanisms are not peers going forward: ``user_version`` stepping
subsumes what :func:`_apply_migrations` does, so :data:`_VERSIONED_MIGRATIONS`
is where a **new** migration belongs — including a plain ``ADD COLUMN``, which
gets a run-once gate there instead of a swallowed error on every open.
:func:`_apply_migrations` stays only to carry the databases already in the field
past the column adds that predate versioning; it is closed to new entries.
"""

import sqlite3
from collections.abc import Callable
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
        # only). Added nullable, and unavoidably so: SQLite rejected the
        # strftime() expression default schema.sql carried then ("Cannot add a
        # column with non-constant default"), and rejects a bare NOT NULL now
        # that lode-uk1i has dropped that default (ADD COLUMN NOT NULL requires
        # a non-NULL constant default). So every DB predating lode-pig has this
        # column nullable with no default, whatever schema.sql says — which is
        # why the migration test also drains one.
        # Backfilled below so pre-existing rows are not left NULL
        # (NULL fails the ``next_attempt_at <= now`` claim predicate → invisible).
        "ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT",
        # lode-gpzn.2: persisted Atlassian API base (see schema.sql's comment on
        # the column) — nullable, NULL for every pre-existing (web) row.
        "ALTER TABLE externals ADD COLUMN api_base TEXT",
        # lode-568v.4: LLM provider identity alongside `model` (see schema.sql's
        # comment on the column) — nullable, NULL means "anthropic" by
        # convention, no backfill needed for pre-existing rows.
        "ALTER TABLE annotations ADD COLUMN provider TEXT",
        "ALTER TABLE egress_log ADD COLUMN provider TEXT",
        # lode-u6he: consecutive collect_enrich_batch() failure budget per
        # batch_handle (see schema.sql's comment on the column). Nullable is
        # unnecessary here (unlike next_attempt_at above) -- 0 is a valid,
        # constant default SQLite accepts on ADD COLUMN.
        "ALTER TABLE jobs ADD COLUMN batch_collect_failures INTEGER NOT NULL DEFAULT 0",
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


def _migrate_v1_egress_log_tool_purpose(conn: sqlite3.Connection) -> None:
    """lode-35nu.11.7: rebuild ``egress_log`` and add ``externals.discovered_via``.

    ``egress_log.purpose`` is ``CHECK (purpose IN ('enrich', 'qa'))`` and SQLite
    cannot ``ALTER`` a ``CHECK`` — admitting ``'tool'`` (a non-LLM cloud call, e.g.
    a JIRA/Confluence/web query from the tool-augmented Ask path) requires a full
    table rebuild, not a column add, and ``model`` moves from ``NOT NULL`` to
    nullable in the same rebuild (a tool call has no model). ``destination``
    (where the call went) and ``arguments`` (as sent, post-redaction) are new,
    always-nullable columns — NULL for every existing ``enrich``/``qa`` row,
    which carries no data loss since neither concept applies to an LLM call.
    ``model`` goes nullable only *for a tool call*: the table-level
    ``CHECK (purpose = 'tool' OR model IS NOT NULL)`` preserves the guarantee the
    old ``NOT NULL`` gave for ``enrich``/``qa``, which an audit trail cannot
    afford to drop.

    The DDL below is deliberately a literal, not derived from
    :func:`schema_sql` — a migration is pinned to the shape *this version*
    produced, so a later ticket editing ``schema.sql`` must not retroactively
    change what v1 builds. While v1 is the newest step the two are necessarily
    identical (``tests/test_storage.py`` asserts it); once a v2 lands, this
    literal freezes and that identity assertion follows the newest step
    instead.

    Runs strictly after :func:`_apply_migrations`, so ``egress_log.provider``
    (lode-568v.4) is already present on the source table by the time this reads
    it. ``externals.discovered_via`` is an ordinary column add and does not need
    a rebuild — folded in here rather than into ``_apply_migrations`` because it
    ships in the same ticket/version step as the audit-trail change above.

    ``egress_log`` carries no index, no trigger, and is the target of no foreign
    key (checked against ``schema.sql``), so the ``DROP``/``RENAME`` below needs
    no index/trigger recreation and is unaffected by the connection's
    ``PRAGMA foreign_keys = ON``. A future rebuild of a table that *is* referenced
    must re-check both before reusing this shape.
    """
    conn.execute(
        "CREATE TABLE egress_log_new ("
        "    id           INTEGER PRIMARY KEY,"
        "    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
        "    purpose      TEXT NOT NULL CHECK (purpose IN ('enrich', 'qa', 'tool')),"
        "    model        TEXT,"
        "    provider     TEXT,"
        "    destination  TEXT,"
        "    arguments    TEXT,"
        "    sent_targets TEXT NOT NULL,"
        "    redactions   TEXT,"
        "    CHECK (purpose = 'tool' OR model IS NOT NULL)"
        ")"
    )
    conn.execute(
        "INSERT INTO egress_log_new "
        "(id, ts, purpose, model, provider, sent_targets, redactions) "
        "SELECT id, ts, purpose, model, provider, sent_targets, redactions "
        "FROM egress_log"
    )
    conn.execute("DROP TABLE egress_log")
    conn.execute("ALTER TABLE egress_log_new RENAME TO egress_log")

    # The catch-and-ignore guard is load-bearing, not belt-and-braces: the
    # user_version gate makes this run once per database, but `externals` may
    # already carry the column at that point. init_db runs executescript BEFORE
    # the migrations, so any table a partially-populated database was missing is
    # created fresh -- in the *target* shape, discovered_via included -- and the
    # ADD COLUMN then hits a duplicate. Removing this raises on exactly that
    # case (proven by the pre-existing migration fixtures in
    # tests/test_storage.py, which seed a jobs table and nothing else).
    try:
        conn.execute("ALTER TABLE externals ADD COLUMN discovered_via TEXT")
    except sqlite3.OperationalError:
        pass  # column already present — idempotent


def _migrate_v2_jobs_live_includes_failed(conn: sqlite3.Connection) -> None:
    """lode-uri7: widen ``idx_jobs_live`` to cover ``status='failed'`` too.

    ``schema.sql``'s partial unique index on ``(type, target_version,
    COALESCE(prompt_ver, ''))`` was scoped to ``pending``/``running`` only, so
    a ``failed`` job in backoff was invisible to it and a duplicate
    ``pending`` row for the same key could be (and, on at least one deployed
    install, was — lode-8a37) enqueued alongside it. Widening the index to
    include ``failed`` makes that duplicate state unrepresentable at the DB
    layer, retiring the compensation code lode-8a37 added to
    :func:`lode.worker._reset_retryable` to survive it.

    A `CREATE UNIQUE INDEX` over data that already violates the new
    constraint fails outright, so this must run **before** the index is
    rebuilt: collapse every existing duplicate live-key group (any row with
    ``status IN ('pending', 'running', 'failed')`` sharing a key) down to one
    row, in two passes --

    1. A ``failed`` row that shares a key with a ``pending``/``running``
       sibling is redundant (drain will pick the live row up on its own) —
       delete it, discarding its `last_error` history.
    2. Among any remaining `failed`-only duplicate group (no `pending`/
       `running` sibling), keep the newest (highest ``id``, most recent
       attempt/`last_error`) and delete the rest — mirrors
       ``_reset_retryable``'s own per-key grouping, just picking the newest
       row instead of the oldest since there is no "resume it next pass"
       future for the ones being deleted here.

    Once no duplicate remains, the index is dropped and recreated in the
    widened shape — ``schema.sql``'s ``CREATE INDEX IF NOT EXISTS`` would
    otherwise leave the old, narrower index in place under the same name.
    """
    conn.execute(
        "DELETE FROM jobs WHERE status = 'failed' AND EXISTS ("
        "  SELECT 1 FROM jobs live"
        "  WHERE live.type = jobs.type"
        "    AND live.target_version = jobs.target_version"
        "    AND COALESCE(live.prompt_ver, '') = COALESCE(jobs.prompt_ver, '')"
        "    AND live.status IN ('pending', 'running')"
        ")"
    )
    conn.execute(
        "DELETE FROM jobs WHERE status = 'failed' AND id NOT IN ("
        "  SELECT MAX(id) FROM jobs WHERE status = 'failed'"
        "  GROUP BY type, target_version, COALESCE(prompt_ver, '')"
        ")"
    )
    conn.execute("DROP INDEX IF EXISTS idx_jobs_live")
    conn.execute(
        "CREATE UNIQUE INDEX idx_jobs_live ON jobs ("
        "  type, target_version, COALESCE(prompt_ver, '')"
        ") WHERE status IN ('pending', 'running', 'failed')"
    )


_SCHEMA_VERSION = 2
"""Target ``PRAGMA user_version`` — bump alongside a new entry in
:data:`_VERSIONED_MIGRATIONS`."""

_VERSIONED_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migrate_v1_egress_log_tool_purpose),
    (2, _migrate_v2_jobs_live_includes_failed),
]
"""Rebuild-shaped migrations, gated by ``PRAGMA user_version`` (see module
docstring). Ordered ascending; each entry runs once, the first time
``user_version`` is below its target."""


def _apply_versioned_migrations(conn: sqlite3.Connection) -> None:
    """Run any pending entry in :data:`_VERSIONED_MIGRATIONS`, bumping ``user_version``.

    Only reached for a *pre-existing* database — :func:`init_db` explains why a
    fresh one is stamped straight at :data:`_SCHEMA_VERSION` instead.

    Each step runs inside its own ``SAVEPOINT``, so a migration and its
    ``user_version`` bump commit or roll back as one unit: a crash partway
    through a table rebuild leaves the database exactly as it was, still below
    that version, and the next open retries it from the top. The savepoint is
    what makes that a *guarantee* rather than a side effect of CPython's
    implicit-``BEGIN`` rules (which already changed once, in 3.12) — without it,
    whether the rebuild is atomic depends on whether some earlier statement in
    :func:`init_db` happened to leave a transaction open. ``SAVEPOINT`` is the
    right primitive rather than ``BEGIN`` precisely because it is correct either
    way: it nests inside an open transaction and starts one when there is none.
    """
    (current,) = conn.execute("PRAGMA user_version").fetchone()
    for version, migrate in _VERSIONED_MIGRATIONS:
        if current < version:
            conn.execute("SAVEPOINT lode_migration")
            try:
                migrate(conn)
                conn.execute(f"PRAGMA user_version = {version}")
            except BaseException:
                conn.execute("ROLLBACK TO lode_migration")
                raise
            finally:
                conn.execute("RELEASE lode_migration")
            current = version


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Open ``db_path`` (creating the file if absent) and apply the schema.

    Enables foreign-key enforcement on the connection, applies the full
    data-shape schema in WAL mode, runs forward-only column migrations for
    existing databases, and commits. Idempotent — every statement is
    ``CREATE … IF NOT EXISTS`` (plus migration guards), so re-running on an
    existing database is a no-op. Returns the open connection for the caller to
    use and close.

    A brand-new database (no ``notes`` table yet) is stamped directly at
    :data:`_SCHEMA_VERSION` — its tables come out of ``schema.sql`` already in
    the target shape, so the rebuild-shaped migrations in
    :data:`_VERSIONED_MIGRATIONS` would be redundant (and, for a plain column
    add, would error on the column already existing). A pre-existing database
    instead runs whichever of those migrations it hasn't seen yet.
    ``user_version`` cannot make this call on its own — it is ``0`` both for a
    fresh file and for every database written before the mechanism existed,
    which is exactly the pair that must be told apart.

    The probe keys on ``notes`` specifically, not "has any table at all":
    ``notes`` is the oldest table in the schema, so every real deployment has
    one, while a *partially* populated database (some tables, e.g. a test
    fixture seeding only ``jobs``) is better treated as fresh — the tables it
    lacks are about to be created in the target shape anyway. A versioned
    migration must still tolerate meeting a table already in that shape; see
    :func:`_migrate_v1_egress_log_tool_purpose`.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    (is_fresh,) = conn.execute(
        "SELECT count(*) = 0 FROM sqlite_master WHERE type = 'table' AND name = 'notes'"
    ).fetchone()
    conn.executescript(schema_sql())
    _apply_migrations(conn)
    if is_fresh:
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    else:
        _apply_versioned_migrations(conn)
    conn.commit()
    return conn
