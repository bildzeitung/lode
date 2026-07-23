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
-- api_base (lode-gpzn.2): the inferred-or-configured Atlassian Cloud API base
-- (e.g. "https://acme.atlassian.net"), persisted synchronously at link-
-- detection time for a JIRA/Confluence external whose external_id is a
-- SEMANTIC key (issue key / page id), not a fetchable URL -- so the async
-- refresh handler can rebuild {api_base}+{external_id} without a network
-- round-trip. NULL for every web external (external_id IS its own fetchable
-- URL there, docs/externals.md "External identity") -- a general seam for any
-- future non-URL-keyed connector, not Atlassian-specific machinery.
CREATE TABLE IF NOT EXISTS externals (
    external_id      TEXT PRIMARY KEY,
    source_type      TEXT NOT NULL,
    head_snapshot_id TEXT,
    no_egress        INTEGER NOT NULL DEFAULT 0 CHECK (no_egress IN (0, 1)),
    api_base         TEXT,
    created          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (head_snapshot_id) REFERENCES snapshots (snapshot_id)
        DEFERRABLE INITIALLY DEFERRED
);

-- snapshots — immutable mirrored external content (UNUSED until connectors),
-- with ONE deliberate exception: fetched_at (see below, lode-9tj4). Every
-- other column (snapshot_id, external_id, body, raw_payload, status) is
-- write-once for the row's lifetime.
-- snapshot_id = H(framed: external_id, body). body is the extracted text;
-- raw_payload is the original fetched bytes/markup; status tombstones link rot.
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL,
    body        TEXT NOT NULL,
    raw_payload TEXT,
    -- fetched_at: production MUST NOT fall through to this DEFAULT (lode-bmg9).
    -- lode.externals.ingest_snapshot -- currently the only production writer of
    -- this table -- stamps it from jobs.now_iso(), the forward-ratcheted queue
    -- clock, because worker._refresh_dead_letter_hook's late-success guard
    -- (lode-uda1) compares this column against the always-ratcheted
    -- jobs.claimed_at. A raw CLOCK_REALTIME value here defeats that guard after
    -- a backward clock step and lets a tombstone clobber a successful fetch.
    -- The DEFAULT is retained for test/ad-hoc inserts; a NEW production writer
    -- of snapshots must pass fetched_at=jobs.now_iso() explicitly.
    --
    -- fetched_at is ALSO the one column ingest_snapshot mutates on an
    -- EXISTING row (lode-9tj4): a successful ("ok") dedup -- an identical
    -- refetch of the current head -- bumps this forward to jobs.now_iso()
    -- rather than leaving it pinned at the original fetch time. Every other
    -- column, and this one for a "tombstone" row, stays write-once. See
    -- docs/storage.md "The guard's blind spot" for why and
    -- src/lode/externals.py's ingest_snapshot docstring for the mechanism.
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
-- quoted_text is the verbatim text span that anchors the annotation to the note
-- body (lode-npx.3); NULL for whole-note items that have no per-span anchor.
-- Re-anchor logic (staleness.py): if quoted_text is set, a verbatim match in
-- the new body → fresh; quote absent but payload value present → stale; both
-- absent → orphaned.  Without quoted_text, payload value presence → fresh vs
-- orphaned (no stale state).
--
-- provider (lode-568v.4, design pinned lode-568v.1): the LLM vendor identity
-- alongside `model`, so a cross-provider corpus stays legible once a second
-- provider exists (lode-568v). NULL means "anthropic" by convention -- every
-- row written before this column existed, and every row written today (only
-- Anthropic is a valid `settings.llm_provider` value so far), is implicitly
-- Anthropic -- so no backfill is needed. A future non-Anthropic provider
-- writes its literal name here.
CREATE TABLE IF NOT EXISTS annotations (
    id             INTEGER PRIMARY KEY,
    target         TEXT NOT NULL,
    source_version TEXT,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('ai', 'user')),
    status         TEXT NOT NULL CHECK (status IN ('fresh', 'stale', 'orphaned')),
    model          TEXT,
    provider       TEXT,
    prompt_ver     TEXT,
    confidence     REAL,
    quoted_text    TEXT,
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

-- passages_fts — regenerable lexical cache; SQLite FTS5 over the SAME passage
-- unit as the vector leg (docs/retrieval.md "FTS5 indexes passages too"), so the
-- two legs rank apples to apples for app-side RRF. Written SYNCHRONOUSLY on the
-- save path — it is model-free, so a just-saved note is keyword-findable before
-- any async embedding runs (docs/design.md save path, docs/retrieval.md "FTS5 is
-- the synchronous index"). `passage_id` / `target_version` are UNINDEXED metadata
-- (passage_id joins back to the `passages` row; target_version is the head this
-- row belongs to, the unit replaced per head change); `text` is the one indexed
-- column. Lives in the SQLite container next to `versions` (docs/stack.md "FTS5
-- sits next to versions") — not LanceDB; lexical stays here, fusion is app-side.
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5 (
    passage_id UNINDEXED,
    target_version UNINDEXED,
    text
);

-- embeddings — derived cache, one per passage. The vector physically lives in
-- LanceDB in the running system (docs/stack.md "Why a split store"); this table
-- is the data-shape row and the sqlite-vec fallback home for the vector blob.
-- model_revision is the resolved HuggingFace revision (commit SHA) the
-- embedder produced this vector under -- nullable (a row predating this field,
-- or a resolution failure at embed time, carries NULL); the manifest for
-- lode-crh8.1's per-vector mismatch-behavior decision
-- (docs/storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81)
-- is the aggregate of this column, not a separate artifact -- see the live
-- LanceDB embeddings table (src/lode/vectorstore.py::VectorStore._schema),
-- which carries the same shape; nothing writes this SQLite table today.
CREATE TABLE IF NOT EXISTS embeddings (
    passage_id     TEXT PRIMARY KEY,
    vector         BLOB NOT NULL,
    model          TEXT NOT NULL,
    model_revision TEXT,
    FOREIGN KEY (passage_id) REFERENCES passages (passage_id)
);

-- edges — the knowledge graph (links between notes/externals). Traversed
-- in-memory via networkx over these rows (docs/stack.md). `from_id`/`to_id` are
-- the doc's `from`/`to` (renamed to avoid the SQL reserved words). source='user'
-- edges are user curation (irreplaceable); source='ai' edges are regenerable.
-- quoted_text is the verbatim text span in the source note body that triggered
-- the inferred edge (lode-npx.3). Re-anchor logic mirrors annotations: verbatim
-- match → fresh; quote absent but to_id value present → stale; both absent →
-- orphaned.  Without quoted_text, to_id presence in body → fresh vs orphaned.
CREATE TABLE IF NOT EXISTS edges (
    id             INTEGER PRIMARY KEY,
    from_id        TEXT NOT NULL,
    to_id          TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('ai', 'user')),
    reason         TEXT,
    confidence     REAL,
    source_version TEXT,
    quoted_text    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('fresh', 'stale', 'orphaned'))
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON edges (from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges (to_id);

-- jobs — the durable async work queue (docs/storage.md "The async work queue").
-- Single-owner SQLite queue, idempotent by key; every job is re-runnable. The
-- one non-reconstructable bit of state is an in-flight batch_handle (a submitted
-- Claude Batch a reconciliation scan can't see, so it must survive a restart).
--
-- Idempotency key (PINNED 2026-06-28, lode-i05.6): identity is
--   (type, target_version) for embed  (prompt_ver is always NULL)
--   (type, target_version, prompt_ver) for enrich
-- NULL prompt_ver would be DISTINCT in a naive UNIQUE, so embed rows cannot be
-- deduped that way. Instead, a partial UNIQUE index over COALESCE(prompt_ver, '')
-- scoped to live (pending/running) jobs enforces deduplication; enqueue uses
-- INSERT ... ON CONFLICT DO NOTHING. Scoping to pending/running is load-bearing:
-- it dedupes in-flight work but STILL ALLOWS a re-enqueue after done/dead (a
-- prompt_ver bump or re-derive must be able to enqueue again).
--
-- Backoff scheduling (PINNED 2026-06-28, lode-i05.6): next_attempt_at (ISO-8601
-- UTC) lets the worker durably schedule a retry; claim selects WHERE
-- next_attempt_at <= now. Without it a restart mid-backoff retries immediately.
--
-- Status lifecycle (PINNED 2026-06-28, lode-i05.6):
--   pending -> running -> done            (success)
--                      -> failed          (transient error; worker resets to pending)
--              failed  -> pending         (retry)
--                      -> dead            (terminal: max-attempts gate)
-- 'dead' is the poison terminal; 'failed' is the transient last-error state
-- retries reset from. They are DISTINCT so the worker can distinguish "retry me"
-- from "give up". The UI surfaces 'dead' rows as dead-letters.
--
-- Crash reclaim (lode-aor): claimed_at (ISO-8601 UTC, set only when a claim
-- flips a row 'pending' -> 'running') is the signal lode.worker's
-- _reclaim_stale_running step uses to detect a job left 'running' by a crash
-- (SIGKILL between claim and completion) -- otherwise such a row is invisible
-- to every claim query (selects 'pending' only) and every reconcile gap query
-- (excludes anything != 'dead'), so nothing would ever pick it back up.
-- NULL for jobs never claimed (still 'pending') or that predate this column.
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    type            TEXT NOT NULL CHECK (type IN ('embed', 'enrich', 'refresh')),
    target_version  TEXT NOT NULL,
    prompt_ver      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'done', 'failed', 'dead')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    batch_handle    TEXT,
    claimed_at      TEXT,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);

-- Partial unique index for live-job idempotency (see notes above). Scoped to
-- status IN ('pending','running') so done/dead rows do not block re-enqueue.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_live ON jobs (
    type, target_version, COALESCE(prompt_ver, '')
) WHERE status IN ('pending', 'running');

-- egress_log — cloud-egress audit trail (docs/storage.md §8, externals.md
-- privacy). One row per time content leaves the box, so exposure is auditable.
-- sent_targets / redactions are JSON summaries.
--
-- provider (lode-568v.4, design pinned lode-568v.1): same treatment as
-- annotations.provider above -- an audit trail's whole point is which vendor
-- content went to, so it carries the same NULL-means-anthropic convention.
CREATE TABLE IF NOT EXISTS egress_log (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    purpose      TEXT NOT NULL CHECK (purpose IN ('enrich', 'qa')),
    model        TEXT NOT NULL,
    provider     TEXT,
    sent_targets TEXT NOT NULL,
    redactions   TEXT
);
