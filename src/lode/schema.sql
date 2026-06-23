-- lode storage schema — the full data shape from docs/storage.md §8.
--
-- This is the single SQLite container (docs/stack.md): it holds the
-- irreplaceable rows (owned content + user curation) AND, in the same file,
-- the rebuildable cache and operational `jobs`. The partition is by rows /
-- value, not by file (docs/stack.md "The partition is by rows, not by file").
--
-- Conventions:
--   * `*_id` content addresses (note/version/external/snapshot) are TEXT — the
--     hex digest of the fast non-cryptographic hash H (see docs/storage.md
--     "Identity vs version"; H itself is built in the sibling hashing module,
--     not here).
--   * booleans are INTEGER 0/1 (SQLite has no native bool), constrained by CHECK.
--   * timestamps are ISO-8601 UTC TEXT, defaulted to now.
--   * enum-ish columns are constrained by CHECK so a bad value fails at write.
--
-- WAL is persistent once set, so it lives here. `PRAGMA foreign_keys` is
-- per-connection (not persistent) and is set by lode.storage.init_db on every
-- connection instead.
PRAGMA journal_mode = WAL;

-- notes — logical note identity (irreplaceable). The head pointer
-- (note_id -> current version_id) lives here. The FK to versions is DEFERRABLE
-- INITIALLY DEFERRED so an atomic save can insert the note row and its first
-- version row in either order within one transaction (the chicken-and-egg of a
-- root create), checked only at COMMIT.
CREATE TABLE IF NOT EXISTS notes (
    note_id         TEXT PRIMARY KEY,
    head_version_id TEXT,
    no_egress       INTEGER NOT NULL DEFAULT 0 CHECK (no_egress IN (0, 1)),
    created         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (head_version_id) REFERENCES versions (version_id)
        DEFERRABLE INITIALLY DEFERRED
);

-- versions — immutable owned content; the append-only per-note version chain.
-- version_id = H(framed: note_id, parent, body) (docs/storage.md). parent is
-- NULL on a create (root); op is the save kind; purged_at marks a hard delete.
CREATE TABLE IF NOT EXISTS versions (
    version_id        TEXT PRIMARY KEY,
    note_id           TEXT NOT NULL,
    parent_version_id TEXT,
    body              TEXT NOT NULL,
    op                TEXT NOT NULL CHECK (op IN ('create', 'update', 'delete')),
    purged_at         TEXT,
    created           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (note_id) REFERENCES notes (note_id),
    FOREIGN KEY (parent_version_id) REFERENCES versions (version_id)
);

CREATE INDEX IF NOT EXISTS idx_versions_note ON versions (note_id);
CREATE INDEX IF NOT EXISTS idx_versions_parent ON versions (parent_version_id);

-- externals — logical identity for an external source (created but UNUSED until
-- the connectors step, docs/storage.md). head_snapshot_id mirrors the note head
-- pointer; same DEFERRABLE rationale as notes.head_version_id.
CREATE TABLE IF NOT EXISTS externals (
    external_id      TEXT PRIMARY KEY,
    source_type      TEXT NOT NULL,
    head_snapshot_id TEXT,
    no_egress        INTEGER NOT NULL DEFAULT 0 CHECK (no_egress IN (0, 1)),
    created          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (head_snapshot_id) REFERENCES snapshots (snapshot_id)
        DEFERRABLE INITIALLY DEFERRED
);

-- snapshots — immutable mirrored external content (UNUSED until connectors).
-- snapshot_id = H(framed: external_id, body). body is the extracted text;
-- raw_payload is the original fetched bytes/markup; status tombstones link rot.
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL,
    body        TEXT NOT NULL,
    raw_payload TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status      TEXT NOT NULL CHECK (status IN ('ok', 'tombstone')),
    FOREIGN KEY (external_id) REFERENCES externals (external_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_external ON snapshots (external_id);

-- annotations — derived layer keyed to what it anchors to. `target` is a
-- polymorphic ref (note_id | external_id) so it carries no FK. AI annotations
-- are version-scoped (source_version = the version_id they were derived from)
-- and may go stale; user corrections (source='user') attach to the logical
-- note_id and ride every version (docs/storage.md "Provenance & user override").
-- Staleness is structural — read off the head-pointer comparison, not a flag.
CREATE TABLE IF NOT EXISTS annotations (
    id             INTEGER PRIMARY KEY,
    target         TEXT NOT NULL,
    source_version TEXT,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('ai', 'user')),
    status         TEXT NOT NULL CHECK (status IN ('fresh', 'stale', 'orphaned')),
    model          TEXT,
    prompt_ver     TEXT,
    confidence     REAL,
    created        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_annotations_target ON annotations (target);

-- passages — regenerable cache; structure-aware chunks, heads only. The
-- retrieval unit (docs/retrieval.md). target_version is a polymorphic ref
-- (version_id | snapshot_id), re-chunked per new head version. char_range
-- locates the chunk in its body; parent_block records the enclosing section
-- for small-to-big context expansion.
CREATE TABLE IF NOT EXISTS passages (
    passage_id     TEXT PRIMARY KEY,
    target_version TEXT NOT NULL,
    ord            INTEGER NOT NULL,
    char_range     TEXT,
    text           TEXT NOT NULL,
    parent_block   TEXT
);

CREATE INDEX IF NOT EXISTS idx_passages_target ON passages (target_version);

-- embeddings — derived cache, one per passage. The vector physically lives in
-- LanceDB in the running system (docs/stack.md "Why a split store"); this table
-- is the data-shape row and the sqlite-vec fallback home for the vector blob.
CREATE TABLE IF NOT EXISTS embeddings (
    passage_id TEXT PRIMARY KEY,
    vector     BLOB NOT NULL,
    model      TEXT NOT NULL,
    FOREIGN KEY (passage_id) REFERENCES passages (passage_id)
);

-- edges — the knowledge graph (links between notes/externals). Traversed
-- in-memory via networkx over these rows (docs/stack.md). `from_id`/`to_id` are
-- the doc's `from`/`to` (renamed to avoid the SQL reserved words). source='user'
-- edges are user curation (irreplaceable); source='ai' edges are regenerable.
CREATE TABLE IF NOT EXISTS edges (
    id             INTEGER PRIMARY KEY,
    from_id        TEXT NOT NULL,
    to_id          TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('ai', 'user')),
    reason         TEXT,
    confidence     REAL,
    source_version TEXT,
    status         TEXT NOT NULL CHECK (status IN ('fresh', 'stale', 'orphaned'))
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON edges (from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges (to_id);

-- jobs — the durable async work queue (docs/storage.md "The async work queue").
-- Single-owner SQLite queue, idempotent by key; every job is re-runnable. The
-- one non-reconstructable bit of state is an in-flight batch_handle (a submitted
-- Claude Batch a reconciliation scan can't see, so it must survive a restart).
CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY,
    type           TEXT NOT NULL CHECK (type IN ('embed', 'enrich', 'refresh')),
    target_version TEXT NOT NULL,
    prompt_ver     TEXT,
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    batch_handle   TEXT,
    created        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);

-- egress_log — cloud-egress audit trail (docs/storage.md §8, externals.md
-- privacy). One row per time content leaves the box, so exposure is auditable.
-- sent_targets / redactions are JSON summaries.
CREATE TABLE IF NOT EXISTS egress_log (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    purpose      TEXT NOT NULL CHECK (purpose IN ('enrich', 'qa')),
    model        TEXT NOT NULL,
    sent_targets TEXT NOT NULL,
    redactions   TEXT
);
