"""Tests for lode.storage / schema.sql — the data-shape schema (lode-s2f.1).

Asserts the acceptance criteria: schema.sql creates every data-shape table in a
fresh WAL DB, and a round-trip insert/select on notes+versions succeeds.
"""

import re
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from lode import storage
from lode.jobs import LIVE_JOB_STATUSES, now_iso
from lode.storage import (
    _SCHEMA_VERSION,
    _migrate_v1_egress_log_tool_purpose,
    init_db,
    schema_sql,
)
from lode.worker import _claim_one

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

        # lode-uk1i: not just visible to the predicate in isolation -- actually
        # claimable by the real worker primitive, on a DB shaped exactly like
        # every pre-lode-pig deployment (nullable next_attempt_at, no SQL
        # DEFAULT at all, since ALTER TABLE ADD COLUMN cannot carry one).
        claimed_id = _claim_one(conn, ("enrich",), now_iso())
        assert claimed_id is not None, "migrated+backfilled job must be claimable"
    finally:
        conn.close()

    # Idempotent: re-running init_db on the migrated DB must not raise.
    conn2 = init_db(db)
    conn2.close()


# ---------------------------------------------------------------------------
# lode-u6he: forward migration for jobs.batch_collect_failures
# ---------------------------------------------------------------------------


def test_batch_collect_failures_migrated_onto_pre_existing_jobs_table(
    tmp_path: Path,
) -> None:
    """A jobs table predating lode-u6he gets ``batch_collect_failures`` added.

    Mirrors ``test_next_attempt_at_migrated_onto_pre_existing_jobs_table``.
    Worth its own pin because ``_apply_migrations`` swallows every
    ``sqlite3.OperationalError`` to stay idempotent, so a DDL that is simply
    *wrong* fails silently and only surfaces later as "no such column". The
    ``NOT NULL DEFAULT 0`` is the specific risk (see the migration's own
    comment): a pre-existing row must read back 0, not NULL, or
    ``batch_collect_failures + 1`` evaluates to NULL and the budget never
    bites.
    """
    db = tmp_path / "lode.db"
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
    seed.execute(
        "INSERT INTO jobs (type, target_version, status, batch_handle) "
        "VALUES ('enrich', 'ver-1', 'running', 'batch-1')"
    )
    seed.commit()
    seed.close()

    conn = init_db(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "batch_collect_failures" in cols, (
            "migration must add jobs.batch_collect_failures"
        )

        assert (
            conn.execute("SELECT batch_collect_failures FROM jobs").fetchone()[0] == 0
        ), "pre-existing row must migrate to 0, not NULL"

        # The increment the budget depends on works on the migrated row.
        with conn:
            conn.execute(
                "UPDATE jobs SET batch_collect_failures = batch_collect_failures + 1 "
                "WHERE batch_handle = 'batch-1' AND status = 'running'"
            )
        assert (
            conn.execute("SELECT batch_collect_failures FROM jobs").fetchone()[0] == 1
        )
    finally:
        conn.close()

    # Idempotent: re-running init_db on the migrated DB must not raise.
    conn2 = init_db(db)
    conn2.close()


# ---------------------------------------------------------------------------
# lode-568v.4: forward migration for annotations.provider / egress_log.provider
# ---------------------------------------------------------------------------


def test_provider_column_migrated_onto_pre_existing_annotations_and_egress_log(
    tmp_path: Path,
) -> None:
    """Pre-seam annotations/egress_log tables (no ``provider`` column) get one added.

    Mirrors ``test_next_attempt_at_migrated_onto_pre_existing_jobs_table``: a DB
    created before lode-568v.4 landed has neither column, so
    ``_apply_migrations`` must add both. Per the pinned design (lode-568v.1,
    ``docs/decisions.md``), ``NULL`` means "anthropic" by convention — no
    backfill ``UPDATE`` is expected, so a pre-existing row must read back
    ``provider IS NULL`` after migration, not some default sentinel.
    """
    db = tmp_path / "lode.db"
    seed = sqlite3.connect(db)
    seed.execute(
        "CREATE TABLE annotations ("
        "  id INTEGER PRIMARY KEY,"
        "  target TEXT NOT NULL,"
        "  source_version TEXT,"
        "  kind TEXT NOT NULL,"
        "  payload TEXT NOT NULL,"
        "  source TEXT NOT NULL,"
        "  status TEXT NOT NULL,"
        "  model TEXT,"
        "  prompt_ver TEXT,"
        "  confidence REAL,"
        "  created TEXT NOT NULL DEFAULT '2026-01-01T00:00:00.000Z'"
        ")"
    )
    seed.execute(
        "INSERT INTO annotations (target, kind, payload, source, status, model) "
        "VALUES ('note-1', 'tag', '\"x\"', 'ai', 'fresh', 'claude-haiku-4-5')"
    )
    seed.execute(
        "CREATE TABLE egress_log ("
        "  id INTEGER PRIMARY KEY,"
        "  ts TEXT NOT NULL DEFAULT '2026-01-01T00:00:00.000Z',"
        "  purpose TEXT NOT NULL,"
        "  model TEXT NOT NULL,"
        "  sent_targets TEXT NOT NULL,"
        "  redactions TEXT"
        ")"
    )
    seed.execute(
        "INSERT INTO egress_log (purpose, model, sent_targets) "
        "VALUES ('enrich', 'claude-haiku-4-5', '[\"ver-1\"]')"
    )
    seed.commit()
    seed.close()

    conn = init_db(db)
    try:
        ann_cols = {r[1] for r in conn.execute("PRAGMA table_info(annotations)")}
        egress_cols = {r[1] for r in conn.execute("PRAGMA table_info(egress_log)")}
        assert "provider" in ann_cols, "migration must add annotations.provider"
        assert "provider" in egress_cols, "migration must add egress_log.provider"

        ann_provider = conn.execute(
            "SELECT provider FROM annotations WHERE target = 'note-1'"
        ).fetchone()[0]
        egress_provider = conn.execute(
            "SELECT provider FROM egress_log WHERE purpose = 'enrich'"
        ).fetchone()[0]
        assert ann_provider is None, "no backfill — NULL means anthropic by convention"
        assert egress_provider is None, (
            "no backfill — NULL means anthropic by convention"
        )
    finally:
        conn.close()

    # Idempotent: re-running init_db on the migrated DB must not raise.
    conn2 = init_db(db)
    conn2.close()


# ---------------------------------------------------------------------------
# lode-35nu.11.7: egress_log rebuild (purpose='tool', destination, arguments,
# model nullable) + externals.discovered_via
# ---------------------------------------------------------------------------


def _seed_pre_lode_35nu_11_7_db(db: Path) -> None:
    """A DB shaped exactly like every deployment before this migration landed.

    egress_log: purpose CHECK (enrich|qa) only, model NOT NULL, no destination/
    arguments. externals: no discovered_via. Seeded with one row each so the
    migration's data-preservation and rebuild behavior are actually exercised,
    not just schema presence.

    The DDL below is the *verbatim* pre-change ``schema.sql`` text for these
    tables -- real ``strftime`` defaults, the ``no_egress`` CHECK, the deferred
    FK -- not a convenient paraphrase. That matters because
    ``test_fresh_and_migrated_db_have_identical_egress_log_and_externals_schema``
    compares defaults and constraints: a fixture that swapped in literal
    timestamp defaults would report a fresh/migrated mismatch that no real
    deployment has, and hiding that would mean weakening the comparison instead
    of the fixture.
    """
    seed = sqlite3.connect(db)
    # init_db's is_fresh check keys off the notes table -- a real pre-existing
    # DB always has one; without it here the fixture would misreport as fresh
    # and skip the migration entirely.
    seed.execute(
        "CREATE TABLE notes ("
        "  note_id TEXT PRIMARY KEY,"
        "  head_version_id TEXT,"
        "  created TEXT NOT NULL DEFAULT '2026-01-01T00:00:00.000Z'"
        ")"
    )
    seed.execute(
        "CREATE TABLE egress_log ("
        "    id           INTEGER PRIMARY KEY,"
        "    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
        "    purpose      TEXT NOT NULL CHECK (purpose IN ('enrich', 'qa')),"
        "    model        TEXT NOT NULL,"
        "    provider     TEXT,"
        "    sent_targets TEXT NOT NULL,"
        "    redactions   TEXT"
        ")"
    )
    seed.execute(
        "INSERT INTO egress_log (purpose, model, sent_targets, redactions) "
        "VALUES ('qa', 'claude-sonnet-4-6', '[\"ver-1\"]', '3')"
    )
    seed.execute(
        "CREATE TABLE externals ("
        "    external_id      TEXT PRIMARY KEY,"
        "    source_type      TEXT NOT NULL,"
        "    head_snapshot_id TEXT,"
        "    no_egress        INTEGER NOT NULL DEFAULT 0 CHECK (no_egress IN (0, 1)),"
        "    api_base         TEXT,"
        "    created          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
        "    FOREIGN KEY (head_snapshot_id) REFERENCES snapshots (snapshot_id)"
        "        DEFERRABLE INITIALLY DEFERRED"
        ")"
    )
    seed.execute(
        "INSERT INTO externals (external_id, source_type) VALUES ('ext-1', 'web')"
    )
    seed.commit()
    seed.close()


def test_egress_log_rebuilt_onto_pre_existing_db_no_data_loss(tmp_path: Path) -> None:
    """A pre-migration DB's egress_log/externals rows survive the rebuild.

    Reproduces the acceptance criteria directly: an existing lode DB opens
    cleanly and ends up with the new egress_log shape and
    externals.discovered_via, with no data loss from the rebuild.
    """
    db = tmp_path / "lode.db"
    _seed_pre_lode_35nu_11_7_db(db)

    conn = init_db(db)
    try:
        egress_cols = {r[1] for r in conn.execute("PRAGMA table_info(egress_log)")}
        assert {"destination", "arguments", "model"} <= egress_cols
        externals_cols = {r[1] for r in conn.execute("PRAGMA table_info(externals)")}
        assert "discovered_via" in externals_cols

        # The pre-existing egress_log row survived the rebuild untouched.
        row = conn.execute(
            "SELECT purpose, model, sent_targets, redactions, destination, arguments "
            "FROM egress_log WHERE sent_targets = '[\"ver-1\"]'"
        ).fetchone()
        assert row == ("qa", "claude-sonnet-4-6", '["ver-1"]', "3", None, None)

        # The pre-existing externals row survived, discovered_via defaults NULL
        # (draw-down origin) — never backfilled to a sentinel.
        discovered_via = conn.execute(
            "SELECT discovered_via FROM externals WHERE external_id = 'ext-1'"
        ).fetchone()[0]
        assert discovered_via is None
    finally:
        conn.close()


def test_egress_log_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "lode.db"
    _seed_pre_lode_35nu_11_7_db(db)

    init_db(db).close()
    # Re-running init_db a second (and third) time must not raise, and must not
    # duplicate or drop the already-migrated row.
    init_db(db).close()
    conn = init_db(db)
    try:
        count = conn.execute("SELECT count(*) FROM egress_log").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def _structural_fingerprint(conn: sqlite3.Connection, table: str) -> tuple:
    """Everything about ``table``'s shape that two DBs must agree on.

    Every ``PRAGMA table_info`` field -- declared type, NOT NULL, DEFAULT and
    primary-key flag, not just a subset -- plus every index and trigger attached
    to the table.

    Keyed by column NAME, deliberately dropping ``cid``: physical column order
    legitimately differs between a fresh DB and a migrated one, because
    ``ALTER TABLE … ADD COLUMN`` can only append. ``externals.discovered_via``
    lands before ``created`` in ``schema.sql`` and after it on a migrated DB --
    as ``api_base`` and ``annotations.provider`` already do from the older
    migration mechanism. That is a pre-existing property of column-add
    migrations, not something this ticket introduced, and nothing reads these
    tables positionally (every query in ``src/lode`` names its columns).

    CHECK constraints are invisible to all of these pragmas, so they are covered
    behaviourally instead (see
    :func:`_assert_egress_log_check_constraints_hold`) -- a fingerprint
    comparison alone would pass on a table that had silently lost the CHECKs the
    rebuild exists to install.
    """
    columns = {r[1]: tuple(r[2:]) for r in conn.execute(f"PRAGMA table_info({table})")}
    indexes = tuple(
        sorted(
            (r[1], r[2], r[3])
            for r in conn.execute(f"PRAGMA index_list({table})").fetchall()
        )
    )
    triggers = tuple(
        sorted(
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = ?",
                (table,),
            ).fetchall()
        )
    )
    return (columns, indexes, triggers)


def _assert_egress_log_check_constraints_hold(conn: sqlite3.Connection) -> None:
    """The CHECKs the rebuild exists to install actually bind on ``conn``.

    Invisible to every PRAGMA, so asserted by behaviour: 'tool' admitted with a
    NULL model, the purpose enum still closed, and an LLM send still forced to
    name its model.
    """
    conn.execute(
        "INSERT INTO egress_log (purpose, destination, arguments, sent_targets) "
        "VALUES ('tool', 'https://acme.atlassian.net', '{\"jql\": \"x\"}', '[]')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO egress_log (purpose, model, sent_targets) "
            "VALUES ('bogus', 'claude-sonnet-4-6', '[]')"
        )
    # model stays mandatory for a real LLM send -- the pre-migration NOT NULL
    # guarantee is narrowed to purpose='tool', not dropped.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO egress_log (purpose, model, sent_targets) "
            "VALUES ('qa', NULL, '[]')"
        )
    conn.rollback()


def test_fresh_and_migrated_db_have_identical_egress_log_and_externals_schema(
    tmp_path: Path,
) -> None:
    """A migrated DB is structurally indistinguishable from a freshly-created one.

    The acceptance criterion is "a fresh DB and a migrated DB have identical
    schemas", so this compares the full structural fingerprint (all of
    table_info, plus indexes and triggers) and the schema version stamp, and
    exercises the CHECK constraints -- which no pragma exposes -- on *both*
    sides.
    """
    fresh_conn = init_db(tmp_path / "fresh.db")
    try:
        fresh = (
            _structural_fingerprint(fresh_conn, "egress_log"),
            _structural_fingerprint(fresh_conn, "externals"),
        )
        fresh_version = fresh_conn.execute("PRAGMA user_version").fetchone()[0]
        _assert_egress_log_check_constraints_hold(fresh_conn)
    finally:
        fresh_conn.close()

    migrated_db = tmp_path / "migrated.db"
    _seed_pre_lode_35nu_11_7_db(migrated_db)
    migrated_conn = init_db(migrated_db)
    try:
        migrated = (
            _structural_fingerprint(migrated_conn, "egress_log"),
            _structural_fingerprint(migrated_conn, "externals"),
        )
        migrated_version = migrated_conn.execute("PRAGMA user_version").fetchone()[0]
        _assert_egress_log_check_constraints_hold(migrated_conn)
    finally:
        migrated_conn.close()

    assert fresh == migrated
    assert fresh_version == migrated_version == _SCHEMA_VERSION


def test_migration_is_atomic_when_a_step_fails(tmp_path: Path) -> None:
    """A rebuild that dies partway leaves the DB exactly as it was.

    The savepoint in ``_apply_versioned_migrations`` is the whole guarantee: the
    half-rebuilt table must not survive, ``user_version`` must not advance, and
    the original rows must still be there -- so the next open simply retries.
    """
    db = tmp_path / "lode.db"
    _seed_pre_lode_35nu_11_7_db(db)

    def _explode(conn: sqlite3.Connection) -> None:
        _migrate_v1_egress_log_tool_purpose(conn)
        raise RuntimeError("simulated crash partway through the migration")

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with (
            mock.patch.object(storage, "_VERSIONED_MIGRATIONS", [(1, _explode)]),
            pytest.raises(RuntimeError),
        ):
            storage._apply_versioned_migrations(conn)
        conn.commit()

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        # No half-built table left behind, and the pre-migration shape intact.
        leftovers = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'egress%'"
            )
        ]
        assert leftovers == ["egress_log"]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(egress_log)")}
        assert "destination" not in cols
        assert conn.execute("SELECT count(*) FROM egress_log").fetchone()[0] == 1
    finally:
        conn.close()

    # And the retry on the next open succeeds normally.
    retried = init_db(db)
    try:
        assert retried.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        assert retried.execute("SELECT count(*) FROM egress_log").fetchone()[0] == 1
    finally:
        retried.close()


def test_egress_log_accepts_tool_purpose_with_destination_and_arguments(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        conn.execute(
            "INSERT INTO egress_log (purpose, destination, arguments, sent_targets) "
            "VALUES ('tool', 'https://acme.atlassian.net', '{\"jql\": \"x\"}', '[]')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT purpose, model, destination, arguments FROM egress_log "
            "WHERE purpose = 'tool'"
        ).fetchone()
        assert row == ("tool", None, "https://acme.atlassian.net", '{"jql": "x"}')
    finally:
        conn.close()


def test_existing_qa_enrich_egress_log_insert_unchanged(tmp_path: Path) -> None:
    """Existing purpose='qa'/'enrich' writers (egress.log_egress) are unaffected."""
    from lode.egress import log_egress

    conn = init_db(tmp_path / "lode.db")
    try:
        log_id = log_egress(conn, "qa", "claude-sonnet-4-6", ["ver-1"], redactions=2)
        row = conn.execute(
            "SELECT purpose, model, sent_targets, redactions FROM egress_log "
            "WHERE id = ?",
            (log_id,),
        ).fetchone()
        assert row == ("qa", "claude-sonnet-4-6", '["ver-1"]', "2")
    finally:
        conn.close()


def _seed_pre_lode_uri7_db(db: Path) -> None:
    """A pre-migration DB: ``jobs`` shaped correctly, ``idx_jobs_live`` scoped
    to ``pending``/``running`` only (the pre-lode-uri7 shape) -- and carrying
    two duplicate-live-key states the widened index cannot tolerate:

    - ``ver-a``: a ``failed`` row alongside a live ``pending`` row for the
      same key (the lode-8a37 shape).
    - ``ver-b``: two ``failed`` rows sharing a key, no live sibling.
    """
    seed = sqlite3.connect(db)
    seed.execute(
        "CREATE TABLE notes ("
        "  note_id TEXT PRIMARY KEY,"
        "  head_version_id TEXT,"
        "  created TEXT NOT NULL DEFAULT '2026-01-01T00:00:00.000Z'"
        ")"
    )
    seed.execute(
        "CREATE TABLE jobs ("
        "    id                     INTEGER PRIMARY KEY,"
        "    type                   TEXT NOT NULL,"
        "    target_version         TEXT NOT NULL,"
        "    prompt_ver             TEXT,"
        "    status                 TEXT NOT NULL DEFAULT 'pending',"
        "    attempts               INTEGER NOT NULL DEFAULT 0,"
        "    last_error             TEXT,"
        "    batch_handle           TEXT,"
        "    batch_collect_failures INTEGER NOT NULL DEFAULT 0,"
        "    claimed_at             TEXT,"
        "    next_attempt_at        TEXT NOT NULL,"
        "    created                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ")"
    )
    seed.execute(
        "CREATE UNIQUE INDEX idx_jobs_live ON jobs ("
        "  type, target_version, COALESCE(prompt_ver, '')"
        ") WHERE status IN ('pending', 'running')"
    )
    seed.executemany(
        "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00.000Z')",
        [
            ("embed", "ver-a", "failed"),
            ("embed", "ver-a", "pending"),
            ("embed", "ver-b", "failed"),
            ("embed", "ver-b", "failed"),
        ],
    )
    seed.commit()
    seed.close()


def test_jobs_live_index_migration_dedupes_and_widens(tmp_path: Path) -> None:
    """lode-uri7: ``idx_jobs_live`` widens to include ``failed``, dedup first.

    Reproduces both duplicate shapes a deployed DB could hold from before the
    widening -- a failed row shadowed by a live pending sibling (lode-8a37's
    exact bug), and two failed rows sharing a key with no live sibling -- and
    asserts each key ends up with exactly one live row, plus that the
    resulting index really is widened (a fresh insert violating it now
    raises, where it previously wouldn't have).
    """
    db = tmp_path / "lode.db"
    _seed_pre_lode_uri7_db(db)

    conn = init_db(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION

        ver_a_rows = conn.execute(
            "SELECT status FROM jobs WHERE target_version = 'ver-a' "
            "AND status IN ('pending', 'running', 'failed')"
        ).fetchall()
        assert ver_a_rows == [("pending",)]  # the failed shadow was dropped

        ver_b_rows = conn.execute(
            "SELECT status FROM jobs WHERE target_version = 'ver-b' "
            "AND status IN ('pending', 'running', 'failed')"
        ).fetchall()
        assert ver_b_rows == [("failed",)]  # one of the two duplicates survives

        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_jobs_live'"
        ).fetchone()[0]
        assert "'failed'" in index_sql

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
                "VALUES ('embed', 'ver-b', 'pending', '2026-01-01T00:00:00.000Z')"
            )
    finally:
        conn.close()


def test_jobs_live_index_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "lode.db"
    _seed_pre_lode_uri7_db(db)

    init_db(db).close()
    # Re-running init_db must not raise (DROP INDEX IF EXISTS / CREATE UNIQUE
    # INDEX with no name collision), and must not re-run the dedup DELETEs
    # against rows that no longer exist.
    conn = init_db(db)
    try:
        remaining = conn.execute(
            "SELECT count(*) FROM jobs WHERE status IN ('pending', 'running', 'failed')"
        ).fetchone()[0]
        assert remaining == 2  # ver-a's pending + ver-b's surviving failed
    finally:
        conn.close()


def test_schema_index_predicate_matches_live_job_statuses() -> None:
    """``idx_jobs_live``'s WHERE clause and ``jobs.LIVE_JOB_STATUSES`` are one set.

    ``schema.sql`` spells the live statuses as SQL literals and Python spells
    them as a tuple; nothing but this assertion binds the two, and the whole
    point of the constant (lode-uri7) is that adding a fourth status cannot
    silently leave the index behind.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(storage.schema_sql())
        (index_sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_jobs_live'"
        ).fetchone()
    finally:
        conn.close()

    predicate = index_sql.split("WHERE", 1)[1]
    assert set(re.findall(r"'([a-z]+)'", predicate)) == set(LIVE_JOB_STATUSES)
