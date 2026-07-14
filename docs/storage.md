# lode — Storage, invalidation & data shape

Covers the foundational data decisions: the ownership boundary (§3), the event-sourced version
chains (§4), how staleness is detected and migrated (§5), and the concrete data shape (§8). See
[design.md](design.md) for the thesis and [stack.md](stack.md) for how this shape maps onto the
chosen engines.

---

## The ownership boundary

*(§3 — foundational decision)*

**The user does CRUD on notes. The AI never touches note content.** Anything the AI produces
— annotations, links, tags, extracted items, embeddings — lives in a **parallel derived layer**
keyed to the note. User notes are the source of truth; the AI layer is a sidecar that can be
regenerated or thrown away without ever risking the original.

Test of a clean separation: **drop the AI-derived cache — embeddings, AI annotations, inferred
edges — and you lose zero user data; it rebuilds from the notes.**

**One caveat the build must honor:** *user* corrections (`source: user` — a fixed tag, a confirmed
or deleted link) live in the derived layer but are **not** AI output and **not** regenerable —
they're genuine user decisions. So the real partition is not *owned vs derived* but
**irreplaceable** (owned content **+** user curation) **vs regenerable cache** (everything the AI
produced). The irreplaceable set is what must be backed up; the cache is rebuildable
(see [stack.md](stack.md)).

This constraint doesn't simplify the design — it *forces* solving invalidation (below).

---

## Storage model: event-sourced, linear per-note chains

*(§4)*

Notes are stored as an **append-only version chain**. Each mutating operation **at save time**
(create / update / delete — not per keystroke) creates a new **immutable node**.

- **create** → new root node
- **update** → new node parented to the prior version
- **delete** → a tombstone node (soft delete; recovery = repoint the head)

Version ids are content-addressed (`note_id` + `parent` + `body`), so a delete of an unchanged,
recovered note is **idempotent**: re-deleting from the same recovered head reproduces the exact
inputs of the earlier tombstone and repoints the head to that existing row rather than minting a
new one (`versions.delete`, lode-n8q). Editing the body (or deleting from a different parent) still
mints a new tombstone as usual.

This was chosen specifically *because* the AI sidecar is the whole point. It hands us, for free:
immutability **by construction**, precise staleness, deterministic annotation migration, full
provenance, and undo. Without the AI layer this would be over-engineering; with it, it pays.

```mermaid
flowchart LR
    subgraph CHAIN["Version lineage — one note's history (linear, immutable)"]
        direction LR
        V1["v1<br>op: create"] --> V2["v2<br>op: update"] --> V3["v3<br>op: update"]
        V3 -.->|soft delete| T["v4<br>op: delete<br>(tombstone)"]
    end

    HEAD(["head pointer<br>note_id → version_id"]) -.->|points at current| V3

    subgraph DERIVED["Derived layer — keyed by what it anchors to"]
        A_AI["AI annotation<br>source: ai<br>source_version = v2"]
        A_USER["user correction<br>source: user<br>attaches to note_id"]
        EMB["embedding<br>target_version = v3 (head)"]
    end

    A_AI -.->|derived from| V2
    A_USER -.->|rides the logical note| HEAD
    EMB -.->|head only| V3

    STALE_NOTE["v2 moved past → AI annotation on v2 is STALE<br>user correction is pinned, never goes stale"]

    classDef owned fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    classDef ai fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    classDef usr fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    class V1,V2,V3,T owned;
    class A_AI,EMB ai;
    class A_USER usr;
```

### Ordering a version chain — never by `created` (lode-t1y)

**The wall clock is not a total order.** `created` (and every other wall-clock column) comes from
SQLite's `strftime('now')`, which reads `CLOCK_REALTIME` — and the OS is free to step that clock
**backward** (NTP correction, or hypervisor catch-up after the guest was descheduled; observed
directly on this host, under load, between two back-to-back `INSERT`s). So a *later* version can
carry an *earlier* `created` than its own parent. That is not a tie a secondary sort key can break —
it is an outright wrong primary order, and `ORDER BY created, rowid` is just as broken as
`ORDER BY created`. **Never sort a version chain by `created`, with or without a tiebreaker.**
`created` is for display and lineage, not for ordering.

The chain's real total order is **insertion order**, and there are exactly two sound ways to read it:

- **`ORDER BY rowid`** — the sanctioned form for a whole-chain sweep, and what `lode.versions.purge`
  uses. `versions` is not `WITHOUT ROWID` (see `schema.sql`), so it has SQLite's implicit rowid, and
  insertion order *is* chain order: a child's `parent_version_id` FK must already exist, so a parent
  is always inserted before its child. It stays correct under any future change to the table, because
  SQLite assigns a new rowid as `max(rowid) + 1` — an inserted row outranks every row still present,
  even if rows are ever deleted (today none are: `purge` overwrites bodies in place). *Caveat:* the
  rowid here is **implicit** (the PK is `version_id TEXT`), and SQLite reserves the right to renumber
  implicit rowids during `VACUUM`. In practice `VACUUM` rewrites rows in rowid order, so *relative*
  order survives, and nothing in lode runs `VACUUM` today — but if a `lode vacuum` ever lands, revisit
  this.
- **Walking `parent_version_id`** root-to-head — use this where the code already holds a
  `version_id → version` map and a sort would be redundant.

The same rule applies to the **jobs queue**, and there it is free: `jobs.id` is `INTEGER PRIMARY KEY`
— a true rowid alias — so `ORDER BY id` is insertion order by construction. `lode.worker`'s claim and
batch-submit queries use it. Never order the queue by `created` either.

### Two graphs — do not conflate them

- **Version lineage** — per-note history. With the decisions below, this is a **linear chain**,
  not a branching DAG.
- **The knowledge graph** — links *between* notes (and later, external resources). This is the
  valuable graph and the actual product. It lives in [externals.md](externals.md).

### Single-user, single-instance: linear chains, no merge

**Decision: single person, single instance, no sync.** This is a **scope boundary**, not a runtime
invariant: it says we will never build the only genuinely hard distributed problem (CRDT / merge
conflict resolution), because the branches that need merging only arise from concurrent edits across
synced devices. We are explicitly not doing that.

The version "graph" is therefore a **linear chain per note**. Two separate mechanisms keep it linear
— and the doc should lean on the mechanisms, not on the "single instance" assertion:

- **Branch prevention = head compare-and-swap (CAS).** Every save parents the current head and is
  **rejected if the head moved since the editor loaded it.** This is the load-bearing guard and it
  holds *regardless of process count* — including two editor panes on the same note inside one
  running app. Correctness here comes from the CAS (plus SQLite serializing writes), not from there
  being one process.
- **Single-instance = a startup advisory lock** (lockfile/PID beside the DB; refuse to start if
  held, pointing at the running PID). This is **not** needed for data integrity — CAS + SQLite cover
  that — but the **async workers (see [design.md](design.md) save path) need a single owner**: two
  instances would run duplicate, racing enrichment + embedding loops and double-spend on Claude
  Batches. That, not corruption, is why we enforce one instance.

Do not pay for merge semantics we will never use.

#### What the user sees when CAS rejects a save

Scoping out *merge* is not the same as ignoring *conflict* — the CAS above can reject a save (two
editor panes on one note, or an edit made while a slow save was in flight), and the design must say
what happens then. It is **manual reconciliation, never auto-merge and never clobber**:

- The save is refused with **"this note changed since you opened it."**
- The user is shown a **diff of their buffer against the new head**, and chooses to **re-apply**
  (their edit re-parented onto the new head as the next version) or **discard**.
- The **rejected buffer is preserved as a draft** until they resolve it, so an unlucky CAS loss
  never costs the unsaved work.

**Where the split falls (decided).** The *storage* layer's contract stops at the honest CAS reject
plus the **buffer-preserving structured conflict** it hands back — the rejected buffer alongside the
new head (version id + body) for the diff — and it persists nothing itself. *Persisting* that buffer
as a durable draft is the **consumer's** job: the `lode add` CLI already writes a `*.draft` beside
the DB, and the TUI (E11) owns the interactive re-apply/discard store. A dedicated `drafts`
table/mechanism is **deferred** until the system is exercised in production.

This keeps the chain linear (the resolved save parents the *current* head) without any merge
machinery — the conflict is surfaced honestly and resolved by the one person who can.

### Identity vs version

Two distinct ids:

- `note_id` — the **logical** note, stable across its whole lineage.
- `version_id` — the immutable node; `version_id` = **`H(len(note_id)‖note_id ‖ len(parent)‖parent ‖ len(body)‖body)`**
  (git's model). Folding in `note_id` makes cross-note collisions impossible (two different notes
  both containing `"TODO"` would otherwise alias); folding in the parent keeps each chain position
  distinct even on a revert to an earlier body (otherwise the reverted node aliases the original and
  `parent_version_id` becomes ambiguous).
- **head pointer**: `note_id → current version_id`.

**Framing is length-prefixed, not bare concatenation.** A plain `note_id ‖ parent ‖ body` has
ambiguous field boundaries — `H("a","bc") == H("ab","c")` — a latent aliasing bug in a
content-addressed store. The **frozen encoding** length-prefixes *every* field: each field is its
UTF-8 bytes preceded by an **8-byte big-endian unsigned length**, and the framed fields are
concatenated in order — `framed(note_id) ‖ framed(parent) ‖ framed(body)` for `version_id`, and
`framed(external_id) ‖ framed(body)` for `snapshot_id`. A root `create` has no parent: the empty
string, framed as an 8-byte zero length followed by no bytes. `H` returns a lowercase hex digest.
(A hash-of-sub-hashes form `H(H(note_id) ‖ H(parent) ‖ H(body))` is *also* unambiguous, but it hashes
to a **different** value, so it is not interchangeable; the length-prefixed form above is the one
canonical encoding.) This is implemented once in `lode.hashing.content_version_id` /
`content_snapshot_id` — the single source of truth that the version-save path and the eval seed
fixture both import, so no two callers can frame ids differently. The `blake2b-128` fallback frames
identically, so it produces the same ids as `xxh3-128` would for the same encoding choice.

**`H` is a fast *non-cryptographic* hash (e.g. xxh3-128), not SHA/BLAKE.** Content addressing here
needs only **low accidental-collision probability**, not adversarial collision resistance: lode is
**single-user, single-instance, no sync** ([above](#single-user-single-instance-linear-chains-no-merge)),
so there is no untrusted party who could craft a colliding body, and the store is never reconciled
against a copy held by someone else. 128 bits keeps accidental collisions negligible at personal-KB
scale while costing far less than a crypto hash. (`H` is a build constant, not a runtime knob — see
[configuration.md](configuration.md); changing it re-keys every node, so pick once. blake2b-128 from
the Python stdlib is the zero-dependency fallback if avoiding the `xxhash` dep is preferred.)

**Dedup of no-op saves is an explicit guard, not hash luck:** before writing, compare the proposed
body to the *head's* body; if equal, return the head and write nothing. (With the parent-inclusive
hash a re-save parents the current head, so it would *not* auto-collide — the dedup has to be an
explicit check.)

Store **full content snapshot per save** (notes are small; do not prematurely delta-compress).
History grows forever — fine for years of personal notes. A compaction/squash policy can come
much later; the no-op-save guard above keeps the chain from growing on saves that change nothing.

---

## Invalidation: the problem the ownership boundary forces

*(§5)*

Because CRUD includes **update**, and AI may not fix the note to match, the derived layer must
*know* when it is stale and re-derive. The event-sourced model makes this **structural rather
than a maintained flag**:

- Each AI annotation records the `version_id` it was derived from.
- If the note's head pointer has moved past that version, the annotation is **stale** — read
  directly off the graph. No hashing, no flag to keep in sync.

### Re-anchoring is a deterministic graph op

Because old and new versions are both retained and linked, on update we diff them and migrate
annotations forward by rule:

- anchored quote **unchanged** → carry annotation forward as **fresh**
- anchored quote **changed** → mark **stale**
- anchored quote **gone** → mark **orphaned**

### Anchoring strategy

- **Whole-note annotations** (tags, summary, links, extracted items): default; trivially robust.
- **Span annotations** (highlight a sentence): anchor by **quoted text + version**, never raw
  character offsets (offsets shatter on any edit above them). On edit, fuzzy-match the quote to
  re-anchor; if no match, mark orphaned rather than guess.

### Stale-display policy (decided, implemented lode-npx.4)

- **Tags / links:** show, but flagged stale (avoids UI flicker on every typo fix).
- **Assertive items (extracted action items, etc.):** hide until re-enrichment is fresh — the
  cost of a wrong action item is higher than a wrong tag.

Stale annotations are never treated as ground truth.

`lode.display` is the single place this policy is applied: `classify_annotation_display(kind,
source, status)` / `classify_edge_display(source, status)` return a `(visible, stale)` decision so
every consumer (CLI, later the TUI, E11) renders the same rule instead of re-deriving it.
`ASSERTIVE_KINDS` (a build constant, [configuration.md](configuration.md)) names the kinds that hide
rather than flag; no extractor emits one yet (lode-npx.1 only produces `tag`/`entity` annotations and
inferred edges, all show-flagged), so this is a forward-compatible hook for action-item extraction.
`display_annotations`/`display_edges` are the DB-reading convenience wrappers.

### Enrichment view-model: three-valued state (decided, implemented lode-ay5.1)

`lode.enrichment_view.enrichment_view(db_path, note_id)` is the ONE shared read seam the TUI
inspector modal and the CLI (`lode show`) both consume for "what did enrichment produce for this
note" — built entirely on `display_annotations`/`display_edges` above (no second copy of the
stale-display policy) plus a head-keyed `enrichment_state`.

**Content vs. state are two different keys.** Content (summary/tags/entities/edges) is
`note_id`-scoped and spans every version in the chain, exactly as `display_annotations`/
`display_edges` already read it — never suppressed by state. `enrichment_state` instead is keyed on
the note's **head version**, because "did the current head finish enriching" is a different question
than "what's the latest known content." One consequence: a note mid-re-enrichment legitimately shows
`enrichment_state="pending"` **alongside** its stale last-known content — that co-occurrence is
intended, not a bug, and follows directly from the "show-flagged, never hide" stale-display policy.

**Staleness is structured data on every field, not a baked-in string (decided, implemented
lode-0qc).** `summary`/`tags`/`entities` carry a frozen `EnrichmentItem(value: str, stale: bool)`
rather than a pre-rendered string with a `" [stale]"` suffix — symmetric with `edges`, which already
carried a structured `stale: bool` on `EnrichmentEdge`. The seam hands back the `stale` bit as data;
rendering it is entirely the consumer's call (the TUI styles a stale item, e.g. dimmed or iconed; the
CLI prints the `" [stale]"` suffix). This was originally decided the other way (`tags`/`entities`/
`summary` as bare strings, per the epic's pinned design) but revised before either consumer (the
lode-ay5.2 modal, the lode-ay5.3 CLI parity work) was built: a string-only seam would force the TUI to
string-sniff the suffix just to style a stale tag differently — exactly the format-as-protocol
coupling this shared view-model seam exists to prevent, and cheap to fix only while both consumers
were still unbuilt.

**The predicate** (pinned 2026-07-08, bd `lode-ay5.1`; `"failed"` bucket corrected 2026-07-08, bd
`lode-bvg`), evaluated against the head's `target_version`/`source_version`:

- `"pending"` — the head has a live (`pending`/`running`/`failed`) `type='enrich'` job. `worker.py`
  writes `status='failed'` only in the else-branch of its max-attempts gate, so a `'failed'` job
  always has a retry coming — it is pending work, not a terminal outcome.
- `"failed"` — no live job, but a dead-lettered one (`status='dead'`) exists AND zero `source='ai'`
  rows (annotations or edges) exist for the head's `source_version` — a dead-lettered enrich job
  surfaced honestly rather than misread as "enriched, nothing found."
- `"ready"` — otherwise: either the head has real AI output, or there was never an enrich job for it.

Async enrichment makes an empty section ambiguous ("hasn't run yet" vs. "ran and found nothing" vs.
"ran and died") — this three-way split is what makes all three distinguishable on both surfaces.

**The TUI inspector modal (implemented lode-ay5.2).** `BrowseScreen`'s `i` binding (`Esc` to
dismiss) pushes `EnrichmentModalScreen` (`src/lode/tui/screens/browse.py`), a scrollable popup that
renders `enrichment_view` directly — summary, tags, entities, edges (reason+confidence+stale),
embed status, `enrichment_state` — with no DB access or stale-display logic of its own. Consistent
with the `EnrichmentItem`/`EnrichmentEdge` `stale` bit being structured data rather than a baked-in
suffix, the modal styles a stale item dim (a `rich.text.Text` span) instead of printing a marker;
`lode show` (lode-ay5.3) is the surface that prints the `" [stale]"` suffix instead — same field,
different rendering per medium, exactly the split this seam exists to allow.

**`enrichment_view_conn(conn, note_id)` (implemented lode-ay5.3)** is the connection-taking sibling of
`enrichment_view(db_path, note_id)` — same return value, but reuses a connection the caller already
holds instead of opening a second one. Originally left private (`_enrichment_view`) by lode-ay5.1's
review — "a public API with no caller and no test is speculative" — and promoted once `lode show`
(which already holds an open `conn` and a resolved `note_id`) became that caller. `lode show` now
renders entirely from this seam (no independent `display_annotations`/`display_edges` assembly of its
own) and is at CONTENT parity with the TUI modal: it gained edge `reason`/`confidence` (compact, e.g.
`-> to_id (reason, 0.82)[stale]`) and an `enrichment:` status line ({pending, failed, ready}), neither
of which the pre-ay5.3 CLI printed.

### Provenance & user override

- **Provenance on every annotation:** model id, prompt/version, source `version_id`, timestamp,
  confidence. Enables re-running enrichment after a model upgrade, auditing a bad link, bulk
  purge. Cheap now, painful to retrofit.
- **`source: ai | user` on the annotation layer.** Users *will* correct an AI tag or link. That
  correction is still metadata (doesn't touch note content), and it is **pinned**:
  - **AI annotations are version-scoped** — regenerable, allowed to go stale, re-derived per head.
  - **User annotations attach to `note_id`** (the logical identity) — they ride across every
    version automatically, so re-enrichment never re-adds a link the user just removed.

**Deletion mechanism (implemented lode-npx.4): a user delete is a pin, not a physical `DELETE`.**
`lode.curation.delete_annotation`/`delete_edge` convert the row *in place* to a **suppression
tombstone** — `source='user', status='orphaned'`, `source_version` cleared — keeping the same
`target`/`kind`/`payload` (or `from_id`/`to_id`) so it stays matchable. Two things fall out of the
row staying present rather than vanishing:
- **Never re-anchored:** `lode.staleness` only touches `source='ai'` rows, so a tombstone is inert
  under structural re-anchoring forever.
- **Never re-added:** before inserting a suggestion, `lode.enrich._write_enrichment` calls
  `lode.curation.is_annotation_suppressed`/`is_edge_suppressed`, which checks for *any*
  `source='user'` row on the same `(target, kind, payload)` (or `(from_id, to_id)`) — tombstone or a
  user-authored row the user chose to keep, either way the AI duplicate is skipped. This is what
  makes "re-enrichment never re-adds a link the user just removed" true rather than aspirational:
  without it, a fresh Haiku call on the next version would simply re-suggest the same tag/edge and
  the deletion wouldn't stick.
- `status='orphaned'` here does not mean *structurally gone* (the §5 re-anchor meaning) — for
  `source='user'` rows the column instead flags "this is a suppression tombstone, not an active
  annotation," which `lode.display` reads to keep it hidden. Re-anchor and display are the only two
  readers of `status`, and re-anchor never looks at `source='user'` rows at all, so the two meanings
  never collide in practice.

### Purge: the note-wide hard cascade (decided)

`purge` (the [hard delete](externals.md#hard-delete-the-deliberate-immutability-break-corrective-half),
E8) is the one op that sweeps a note's **whole chain**, not just its head — and it is implemented as
two halves split along the ownership boundary:

- **Irreplaceable side** (`lode.versions.purge`): in one transaction, overwrite *every* version body
  in the chain — live head, prior updates, and soft-delete tombstones alike — with the `[purged
  YYYY-MM-DD]` marker and set `purged_at`; `version_id` / `parent_version_id` / `op` / `created`
  survive, so lineage does. `purged_at` **is** the "purged" flag (no separate column): a non-null
  `purged_at` means the body no longer hashes to its `version_id`, which stays as the historical id.
  The chain's regenerable `source='ai'` annotations are dropped here too (they are version-scoped, so
  keyed by `source_version`); `source='user'` corrections survive (curation, not content).
- **Cache side** (`lode.repository.Repository.purge`): the cascade runs **through the cache seam**,
  never reaching into an engine module — every swept version is `evict`-ed, so the `CompositeCache`
  fans the drop to every engine (LanceDB vectors, FTS rows, future graph). This is exactly the
  note-wide drop the per-head soft-delete `evict` *omits*: a normal update/delete only indexes the
  new head, so each superseded version's cache rows linger (retrieval filters to the live head) —
  purge is the only op that clears them all. The live head is then **re-derived locally** from the
  now-purged marker body (`index`), so the note stays present in the index as `[purged …]` with no
  leaked content — *unless* the head is a soft-delete tombstone, which carries no passages of its own
  and is left evicted (mirroring the normal delete path). "Every cache row referencing the purged
  version is gone" means the **secret-bearing** rows: after the cascade no cache row carries the
  original content, only the marker.

---

## The async work queue

The "capture stays instant" promise ([design.md](design.md) §1) is structurally a promise that the
derived work happens *later*: chunk + embed, enrich via Claude, propose inferred edges, refresh
externals, and re-derive the corpus when `prompt_ver` or the model bumps. That backlog needs a home.

### One property makes this easy: lag is safe by construction

Because every derived row records its `source_version` and **staleness is structural** (see
[invalidation](#invalidation-the-problem-the-ownership-boundary-forces)), workers can **lag
arbitrarily without corrupting anything.** A job that finishes late just writes a possibly-stale
annotation, which the head-pointer comparison flags for re-derivation. So:

- the queue needs **no locking against edits**, and
- **every job is idempotent by key** (`type + target_version` for embed, or
  `type + target_version + prompt_ver` for enrich) — re-running overwrites or
  no-ops. The safe default on any failure is simply *do it again*.

### Shape: a durable `jobs` table + a reconciliation safety net

- **Durable rows in SQLite, enqueued in the same transaction as the save.** `write version row +
  enqueue its derive jobs` is **atomic**, so a crash can never leave a saved note with no pending
  work. (This does put more cache-ish state in the "irreplaceable" file — see
  [the partition is by rows](stack.md#the-partition-is-by-rows-not-by-file).)
- **Job types:** `embed(version)` — fast, local, **high priority**; `enrich(version, prompt_ver)` —
  slow, Claude; `refresh(external)` — implemented with the first connector (`lode-w0h.3`): the web
  draw-down trigger enqueues it via `enqueue_derive_jobs(..., types=("refresh",))` when a note-save
  detects a pasted URL, atomically with the edge + version write. The shared fetch→ingest handler
  is `lode.drawdown.refresh_external`; `lode-w0h.6`'s later refresh policy reuses it unchanged.
  Priority `embed > enrich` so semantic recall lands fast while tags/edges lag (`refresh` carries
  no priority ordering against the other two — it is never in contention with a note capture's own
  derive jobs, since it targets an external, not a version).
- **Reconciliation scan on startup + periodically** re-enqueues any head version missing a fresh
  embedding/enrichment. This is the self-healing net for crashes, dropped jobs, and `prompt_ver`
  bumps (a bump makes every note's enrichment stale → the scan re-enqueues the corpus). Idempotency
  makes running it anytime safe. Implemented in `lode.reconcile` (lode-i05.4): a step registry
  mirrors the worker handler-registry shape; the Phase-A ``embed_gap`` step is registered at module
  load; E7 appends the enrich-gap step; `lode-w0h.6` appends the `refresh_stale` step — the web
  connector's refresh policy, re-enqueueing a `refresh` job for any external past its TTL
  (`docs/externals.md` "Refresh policy"). Each step uses `enqueue_derive_jobs` with `ON CONFLICT DO
  NOTHING` (the ``idx_jobs_live`` live-job index), so running it repeatedly enqueues no
  duplicates. Runs at the start of each `lode work` drain pass (startup + every ``--loop`` tick).
- **Single owner** (the startup advisory lock, above) is what lets a one-claimer SQLite queue stay
  correct with no distributed locking.

### Enqueue ownership, atomicity, and layering — pinned 2026-06-28 (lode-i05.1)

**Ownership:** `Repository.save` is the **sole enqueue site** for derive jobs. It
already owns the irreplaceable version-write + cache index/evict seam; enqueueing a
head version's derive jobs is the third thing that must happen on a head change.
Jobs are operational/irreplaceable rows, so enqueue belongs at the Repository seam,
**not in the CLI and not buried in `versions.py`**.

**Atomicity / seam:** `Repository.save` wraps `lode.versions._save_core` (the
CAS-guarded version-write, which does not open its own transaction) and
`lode.jobs.enqueue_derive_jobs` (a plain `INSERT … ON CONFLICT DO NOTHING` on the
caller's connection) in a **single `with conn:`** context, so "write version row +
enqueue its derive jobs" commits atomically. A crash at any point in that block
rolls back both — no version without its jobs, no jobs without the version.

**Layering contract:**
- `lode.versions._save_core` — raw CAS write, **no transaction boundary** (caller
  owns the txn).
- `lode.versions.save` — convenience wrapper: `_save_core` inside its own `with
  conn:` for direct standalone callers (tests, etc.).
- `lode.jobs.enqueue_derive_jobs` — plain `executemany` INSERT, **no transaction
  boundary** (caller owns the txn).
- `lode.repository.Repository.save` — the authoritative save+enqueue entry point:
  calls `_save_core` + `enqueue_derive_jobs` in one `with conn:`, then drives the
  cache backend after the commit. **Direct callers (e.g. the CLI) must go through
  `Repository.save`, never call `enqueue_derive_jobs` separately.**

**Deduped no-op:** a save whose body equals the live head returns without writing
any row and without enqueuing anything — the version chain and the job queue are
both unchanged.

**Builds on lode-i05.6:** enqueue uses `ON CONFLICT DO NOTHING` against the
`idx_jobs_live` partial unique index (below), so a duplicate or reconcile
re-enqueue of a live job is silently dropped.

### Schema decisions — pinned 2026-06-28 (lode-i05.6)

**Idempotency key — partial UNIQUE index with COALESCE.**
Job identity is `(type, target_version)` for `embed` (prompt_ver always NULL) and
`(type, target_version, prompt_ver)` for `enrich`. A naive `UNIQUE(type,
target_version, prompt_ver)` would not dedupe embed jobs because SQLite treats
NULLs as distinct in UNIQUE constraints. Instead the schema uses a partial unique
index over a COALESCE expression, scoped to live (pending/running) jobs only:

```sql
CREATE UNIQUE INDEX idx_jobs_live ON jobs(type, target_version, COALESCE(prompt_ver, ''))
    WHERE status IN ('pending', 'running');
```

Scoping to `pending`/`running` is load-bearing: it dedupes in-flight work but
**still allows** a re-enqueue after the prior job reached `done`/`dead` (a
`prompt_ver` bump or re-derive must be able to enqueue again — the reconciliation
scan depends on this). Enqueue uses `INSERT ... ON CONFLICT DO NOTHING`.

**Backoff scheduling — `next_attempt_at`.**
The `next_attempt_at TEXT NOT NULL DEFAULT (strftime(...'now'))` column (ISO-8601
UTC) lets the worker durably schedule a retry: claim selects `WHERE status =
'pending' AND next_attempt_at <= now`. Without this column a restart mid-backoff
retries immediately.

**Dead-letter terminal — distinct `dead` status.**
The status lifecycle is:
```
pending -> running -> done                    (success)
           running -> failed -> pending       (transient error; worker resets for retry)
                      failed -> dead          (terminal: max-attempts gate)
```
`dead` is the poison terminal reached at the max-attempts gate. `failed` is the
*transient* last-error state that retries reset from. They are distinct so the
worker can distinguish "retry me" from "give up", and the UI surfaces `dead` rows
as dead-letters (not `failed` rows).

### Transient vs. permanent job failures — pinned (lode-9yy)

`run_one`'s handler-failure catch is deliberately broad (`except Exception`) because
the whole `pending -> failed -> dead` lifecycle above assumes a failure is
*transient* — safe to retry with backoff, and eventually dead-letter as a last
resort. That is the right default for a flaky network call, a rate limit, or a
Batches API hiccup. It is **wrong** for a failure that retrying can never fix: most
concretely `lode.auth.AuthError`, raised by `lode.auth.build_client` when no
Anthropic credentials can be resolved. Retrying an unauthenticated call burns the
retry budget on something structurally certain to fail again, and the operator
never sees `build_client`'s actionable "set `ANTHROPIC_API_KEY` / run `ant auth
login`" message — they see an ordinary failed, then dead-lettered, job.

The taxonomy: **permanent** = `lode.auth.AuthError` (today's only member; a future
config-shape error belongs here too, same treatment) — never retried, never
charged against `attempts`, must reach the operator directly. Everything else is
**transient** — the existing `except Exception` accounting, unchanged.

`run_one` and `_batch_submit_enrich` special-case `AuthError` ahead of their
generic catch: the claimed job is reset straight back to `'pending'` with
`attempts` untouched (never `'failed'` with backoff, never `'dead'`), then the
exception is **re-raised** rather than absorbed. What happens next depends on the
caller — the queue mechanism itself has no opinion here, by design; each caller
decides how loud "surface it" should be:

- `lode work` (`lode.worker.drain`) lets it reach the CLI, which prints
  `build_client`'s actionable message to stderr and exits non-zero — the same
  clean, traceback-free treatment `lode ask` already gives `AuthError`.
- `lode add`'s opportunistic immediate-enrich fast path
  (`lode.cli._enrich_immediately`) catches and discards it instead: capture must
  stay instant (`design.md` §1) regardless of whether Anthropic credentials are
  configured. The job is already back at `'pending'`, uncharged, for the next
  explicit `lode work` to report loudly.
- `_batch_collect_enrich` (the *other* batch pre-step, polling an in-flight
  request) needs no special case of its own: it never wraps its `build_client()` /
  `collect_enrich_batch()` calls in a broad `except`, so an `AuthError` there
  already propagates out of it — the swallow this section fixes never existed on
  that path. `drain` handles it identically to `_batch_submit_enrich`'s, below.

**A missing credential must not starve the credential-free work.** Both enrich
batch pre-steps run *before* `drain`'s reclaim, retry-reset, and main claim/run
loop — so a pre-step that raised on the spot would abort the entire drain. That
is not acceptable: `embed` jobs are produced by the **local** fastembed model and
have nothing to do with Anthropic credentials, and a pending enrich job is
essentially always present (every `add` enqueues one). Raising from the pre-step
would therefore abort *every* `lode work` before the first embed ever ran, leaving
an unkeyed user's embeds pending forever and silently killing the dense half of
retrieval — trading "enrich is retried forever" for "the whole queue stops",
which is strictly worse.

So `drain` **stashes** the pre-step's `AuthError`, completes the reclaim, the
retry-reset and the main claim/run loop, and re-raises it only at the end. The
main loop drains `embed` ahead of `enrich` (`_claim_one` orders on type), so the
embeds land before any residual enrich job re-raises out of `run_one`. Net effect
for an operator with no credentials: embeds keep draining, enrichment stays
pending and uncharged, and `lode work` still exits non-zero with the actionable
message.

### The queue's clock must never go backward — nor lag the wall clock (lode-t1y)

The eligibility predicate is `next_attempt_at <= now`, and `drain`'s loop stops at the **first** miss —
so a `now` that reads even momentarily low doesn't just delay one job, it strands every job behind it
for the rest of the pass. The OS wall clock (`CLOCK_REALTIME`) is not safe to use directly: it can be
stepped **backward** (NTP correction; hypervisor catch-up after the guest was descheduled — observed
on this host under load, between two back-to-back reads in the same loop).

`lode.jobs.now` is the clock for that comparison, and it guarantees two things, both load-bearing
and pulling in opposite directions:

1. **Readings never decrease** — a backward step is absorbed, so no job is spuriously "not ready yet".
2. **Readings are never *behind* `CLOCK_REALTIME`** — because *not every timestamp it is compared
   against comes from it.* `jobs.next_attempt_at` defaults to SQLite's own `strftime('now')`, and a
   job is typically enqueued by one process (CLI/TUI) and claimed by another (`lode work`). A clock
   that merely never went backward would fall permanently behind those writers after a *forward* step,
   make every freshly enqueued job look not-yet-due, and strand it exactly as in (1) — trading one bug
   for a worse one.

Guarantee (2) is what makes rows stamped from a raw wall clock — SQLite's column default, or another
process — safe to compare against `now()` without routing every writer through it. A monotonic clock
alone would **not** have been safe. The cost of absorbing a backward step is running slightly ahead of
true time until the wall clock catches up: a job retried a hair late, which beats a job stranded.

The clock lives in `lode.jobs` (moved from `lode.worker` in lode-ajda), not because the worker stopped
needing it, but so `lode.enrich`'s own retry/backoff transition (`_mark_job_failed`, applied to an
errored/expired/canceled Batches API result) could share it too instead of reading a second, raw
`datetime.now(UTC)` of its own — previously a second, independently-drifting copy of both the backoff
formula and the failed/dead-letter state transition. Both `lode.worker.run_one` and
`lode.enrich._mark_job_failed` now call the one shared `lode.jobs.record_job_failure`.

### Crash reclaim: a job stuck in `running` — pinned (lode-aor)

Neither the claim query (`WHERE status = 'pending'`) nor either reconciliation
gap query (`WHERE ... status != 'dead'`) will ever pick a `'running'` row back
up — so a worker (or the CLI's inline immediate-enrich fast path, lode-npx.2)
that crashes or is killed between claiming a job and completing it leaves that
row **stuck forever** with no self-healing net, unless something explicitly
watches for it.

That something is `claimed_at` (set only by the claim `UPDATE`, ISO-8601 UTC)
plus a dedicated step, `lode.worker._reclaim_stale_running`, run at the top of
every `drain()` pass (before `_reset_retryable`): any job still `'running'`
with `claimed_at` older than `settings.stale_running_timeout_s` (default 15
minutes, `runtime`) is put through the *same* attempts/backoff/dead-letter
accounting `run_one` uses for a transient handler failure — no parallel retry
policy. Applies uniformly to `embed`, `enrich`, and `refresh`.

**Batch-backed enrich jobs are excluded** (`batch_handle IS NOT NULL`) — their
long-lived `'running'` status is intentional (the prior section's
resume-on-restart contract, lode-i05.5); reclaiming one here would risk
resubmitting a Batches API request still in flight.

**Every writer that *fails or resets* a `'running'` claim is CAS-guarded on the
exact claim — `AND status = 'running' AND claimed_at IS <the value read at
claim time>` — pinned lode-3jte, tightened lode-nggm.** All four sites —
`jobs.record_job_failure`'s two UPDATEs, `_reclaim_stale_running`'s two
UPDATEs, and `run_one`'s `AuthError` reset — go through one shared primitive,
`jobs.cas_update_running` (lode-nggm hole 3: this idiom used to be hand-rolled
at each site, so a change to the guard shape had to be made by hand in every
one). A caller can reach these writers with no worker lock held
(`cli._enrich_immediately` via `claim_and_run_one`), so a concurrent
`_reclaim_stale_running` can dead-letter the row (firing its dead-letter hook)
*while the stalled handler is still in flight*; a status-only guard would then
resurrect that dead-lettered job back to `'failed'` — double-charging
`attempts` and leaving a second, spurious dead-letter hook run on the table
(the race lode-3jte closed for `record_job_failure`). Guarding on `claimed_at`
too (not just `status`) additionally closes the **ABA case** lode-3jte left
open: `status='running'` is not a unique claim identity, it is a state a row
can cycle back through — reclaimed to `'failed'`, reset to `'pending'`, and
re-claimed to `'running'` again (a *different* `claimed_at`, possibly a
different worker) — all inside one stall that already exceeds
`stale_running_timeout_s`, before the *original* stalled caller's write, still
holding its now-stale `claimed_at`, finally lands. A status-only guard cannot
tell that later claim from its own and would clobber it; comparing the exact
`claimed_at` means it no longer matches. `record_job_failure` reports a third
value, `claim_lost`, when `cas_update_running`'s rowcount was 0; `run_one`
checks it and, when true, leaves the row exactly as it found it and skips the
dead-letter hook rather than running it again. `enrich._mark_job_failed`
ignores `claim_lost` — it only ever runs against batch-submitted jobs, which
`_reclaim_stale_running`'s SELECT above already excludes from this race
(nothing ever clears `batch_handle` back to `NULL`, so a row that reaches it
is excluded for life).

**The *success* transitions are deliberately left unguarded — a late success
WINS over a reclaim's dead-letter verdict (settled, lode-nggm hole 1 →
lode-37gg).** Three sites write an unguarded `status = 'done'`: `run_one`'s two
(`worker.py:495`, stamping `prompt_ver`, and `worker.py:499`) and
`submit_enrich_batch`'s gated-out `skip_ids` write (`enrich.py:616`). The same
race described above can let a late-succeeding handler resurrect a job whose
dead-letter hook already fired — and that is intentional, not a hole left
open.

**Why: success is monotonic; a dead-letter is a verdict.** "This work
completed" is a fact about the world that no later event invalidates. A
reclaim's dead-letter is a *prediction* that the work never will complete.
When the prediction and the fact disagree, the fact wins — which is exactly
what the unguarded success UPDATE already does.

**The obvious "symmetry with the failure path" fix was considered and
rejected.** CAS-guarding all three success writers with
`jobs.cas_update_running`, for consistency with the guard the failure path
now has, sounds tidy but conflates two different things. A late *failure*
write clobbers a *different* claim's state (the ABA case above) or
double-fires the dead-letter hook — genuine corruptions, correctly guarded. A
late *success* write instead replaces the reclaim's pessimistic guess with
ground truth. Guarding it wouldn't prevent a corruption; it would suppress a
true fact in favor of a stale prediction.

**The decisive fact: for enrich, the success UPDATE *is* the idempotency
receipt.** `prompt_ver` is only ever stamped by a *success* write — at
`worker.py:495` here, and at `enrich.py:783` on the batch-collect path (which
is `batch_handle`-set, so it is exempt from this race entirely). There is no
other writer: a job that does not succeed never gets a `prompt_ver`.
reconcile's enrich gap query (`reconcile.py:326-338`) re-enqueues any live
head version with no pending/running/failed enrich job *and* no `'done'`
enrich job carrying a matching `prompt_ver` — a `'dead'` row satisfies both.
So CAS-guarding the success write would mean: the Haiku call completes, the
enrichment is durably written (the handler commits its side effects *before*
`run_one`'s `with conn:` opens), the guard no-ops the status write, the row
stays `'dead'`, the `prompt_ver` receipt never lands, and reconcile — unable
to see the work was done — re-enqueues and pays Haiku a **second time** for a
result already sitting in the database. The guard would not prevent a
corruption; it would destroy the record of a completed, paid-for API call.

`embed` is the same shape, cheaper: `reconcile.py:239` is `NOT EXISTS (embed
job WHERE status != 'dead')`, so a guarded-out embed success would leave
`'dead'` → re-enqueued → redundantly re-embedded.

`refresh` is the exception that proves the rule: guarding it would change
nothing, because its sweep never reads the job row's terminal status at all.
The refresh sweep keys on the TTL (`s.fetched_at <= now - refresh_ttl_s`,
`reconcile.py:439`) and suppresses only on an *in-flight* job (`status IN
('pending', 'running')`) — there is **no dead-job arm**, so a `'dead'` refresh
row neither blocks nor triggers a re-fetch. A guarded-out refresh success would
therefore leave only an inert lie in the job row. What the sweep *does* exclude
is a tombstoned head (`s.status != 'tombstone'`, `reconcile.py:438`) — and that
exclusion, not the job status, is what makes the separate lode-uda1 corruption
below permanently absorbing.

**The ABA case is benign for success**, unlike for failure: a stale success
stamping `'done'` on a row a *different* claim now holds just means that
second claim redundantly redoes work that already succeeded; if it later
fails, its CAS-guarded failure write correctly no-ops against `status =
'done'`. Nothing breaks. Contrast the failure path, where ABA is a real
corruption — that is what the `claimed_at` guard above exists to close.

`run_one`'s two sites are exposed for the obvious reason: the handler stalls
across a network call, which is exactly the window `_reclaim_stale_running`
exists to sweep. The two *other* enrich `'done'` writers each need a word,
because the `batch_handle` exemption covers one of them and not the other:

- **`submit_enrich_batch`'s gated-out `skip_ids` UPDATE** (`enrich.py:616`) is
  also exposed — those rows were pre-claimed to `'running'` by
  `_batch_submit_enrich` with `batch_handle` still `NULL` at that point
  (nothing has been submitted yet), so they sit squarely inside
  `_reclaim_stale_running`'s `batch_handle IS NULL` SELECT. Its window is far
  narrower than `run_one`'s — the gated-out write happens in-process, before
  the network call, so reaching it takes a `stale_running_timeout_s` stall
  between the pre-claim and a purely local gating step — but "narrow" is not
  "excluded", and the same settled rule applies: unguarded, intentionally.
- **`collect_enrich_batch`'s two `status = 'done'` UPDATEs are *not* exposed**
  to this race at all, despite writing the same-shaped unguarded UPDATE: like
  `_mark_job_failed` above, they only ever run against rows selected by
  `batch_handle = ?`, which `_reclaim_stale_running` excludes for life.

A genuine, separate corruption exists in the same neighborhood — the refresh
dead-letter hook's tombstone can race the handler's real snapshot and leave a
permanently-absorbing tombstone for a fetch that succeeded — but CAS-guarding
the success write above would **not** have fixed it (the tombstone is already
written by the time the guard would run); it is tracked as its own ticket
(lode-uda1), not folded into this decision.

### A dead-letter hook's write can race a late success too — settled (lode-uda1)

The paragraphs above are about a job's own `status` transition racing a reclaim. There is a
**second, distinct** race, in the `refresh` **dead-letter hook** itself
(`lode.worker._refresh_dead_letter_hook`, lode-at8): the hook doesn't just flip a status column, it
**writes a snapshot** (`lode.externals.ingest_snapshot`, `status='tombstone'`) that moves the
external's `head_snapshot_id`. That write can race the still-in-flight handler's own real snapshot
write, independently of which job-status update ends up winning:

1. `_reclaim_stale_running` selects the row (still `'running'`, `claimed_at` past
   `stale_running_timeout_s`) and dead-letters it, firing the hook.
2. Meanwhile the original handler — genuinely mid-fetch, not crashed — finishes and commits its
   **real** snapshot via `ingest_snapshot`.

Whichever of these two `ingest_snapshot` calls lands *last* wins the head. The bad ordering — the
handler's real snapshot lands first, then the hook's tombstone overwrites it — leaves
`head_snapshot_id` pointing at a tombstone **even though the fetch succeeded**, and this is not a
transient wrong reading: reconcile's refresh sweep and embed sweep both filter
`AND s.status != 'tombstone'` (deliberately, for a genuine permanent failure — see "Fetch-outcome
taxonomy" in `docs/externals.md`), so nothing ever revisits that external again. **Absorbing**, not
self-correcting.

**Settled: a fact beats a verdict, same principle as the job-status races above.** The hook is
passed the dead-lettered job's own `claimed_at` and skips its tombstone write when the external's
current head is already a non-tombstone snapshot fetched **at or after** that claim — that can only
mean a real fetch (this job's own racing handler, or some other refresh) already landed content
newer than the point at which the dead-letter verdict was decided, so the verdict is stale and must
not clobber it. A head fetched *before* the claim is unaffected — that is the pre-existing,
intentional case where a *later* refresh (the staleness policy, not this race) exhausts its retries
and correctly tombstones over older, still-referenced content regardless (no "unless there's prior
content" carve-out — see the hook's own docstring).

This is deliberately narrower than, and independent of, the `run_one` success-UPDATE question above:
guarding the hook's *snapshot write* on a fact/verdict basis is uncontroversial once stated (the
tombstone is documentation of a fetch outcome, and documenting a stale outcome over a real one serves
nobody), whereas guarding the *job row's* `status='done'` transition trades off against the enrich
`prompt_ver` receipt (see the note above) and needed its own ticket. This guard neither depends on
nor prejudges how that one is settled.

**What this guard does *not* do: it narrows the window, it does not close it.** The check is a
read-then-write — `externals.head_snapshot_info` reads the head, and `ingest_snapshot` then opens its
*own* `with conn:` transaction to write the tombstone — so the read is not atomic with respect to the
write. A handler committing its real snapshot in the gap *between* the hook's read and the hook's
write is still clobbered. That residual window is the few microseconds of one `SELECT` plus a hash
and an `INSERT`, against the seconds-wide window it replaces (the whole span from the handler's
snapshot commit through to `run_one`'s terminal `UPDATE`), so this is strictly better rather than a
race merely moved — but it is a narrowing, not a proof. Closing it outright means holding the write
lock across the read (`BEGIN IMMEDIATE`), which would be this codebase's first use of explicit
transaction control and would couple the hook to `ingest_snapshot`'s internal transaction boundary;
that is deliberately left as its own decision rather than smuggled into this fix. In practice the
exposure is small for a second reason: `refresh` handlers only ever run inside `worker.drain`, which
holds the single-instance `lock.WorkerLock`, so a *live* handler racing a reclaim needs two live
workers on one DB — which the lock prevents in the normal single-machine case, though it explicitly
disclaims being a data-integrity guard ("CAS + SQLite serialization own correctness").

**A second residual — a clock-domain mismatch, not a race — has since been closed (lode-bmg9).** The
guard's `head_fetched_at >= claimed_at` compared two *different* clocks: `snapshots.fetched_at` came
from the schema's raw SQLite `DEFAULT strftime('now')` (`CLOCK_REALTIME`), while `jobs.claimed_at` is
always stamped from `jobs.now_iso()`, the forward-ratcheted queue clock (`lode-t1y`) that — by its own
documented guarantee — deliberately runs *ahead* of `CLOCK_REALTIME` after a backward NTP/hypervisor
step, until real time catches up. For every other comparison in the queue that bias is the safe
direction (`jobs.now()`'s own docstring: it is only ever compared against a raw-stamped column as an
*upper* bound, e.g. `next_attempt_at <= now()`). This comparison needed the opposite: it reads a
raw-stamped column (`fetched_at`) against a *historical* ratchet reading (`claimed_at`), not a live
one, so a claim taken while the ratchet was running ahead could out-read a real, later fetch's raw
timestamp — exactly reproducing this guard's own clobber, just via a clock-skew precondition instead
of a read/write race. **Fix:** `ingest_snapshot` now stamps `fetched_at` explicitly from
`jobs.now_iso()` instead of falling through to the DEFAULT, so both sides of the guard's comparison
are the same clock — closed outright, not narrowed (`src/lode/externals.py`'s `ingest_snapshot`
docstring carries the same cross-reference). Regression test:
`test_reclaim_dead_letter_hook_survives_a_backward_clock_step` (`tests/test_worker.py`) forces a
backward wall-clock step between the claim and the ingest and asserts the guard still recognizes the
real fetch.

*The one deliberate ripple this leaves:* `reconcile.py`'s refresh-staleness sweep (`_refresh_stale_step`,
around `cutoff = jobs.iso(datetime.now(UTC) - ...)`) still computes its cutoff from the **raw** wall
clock, on purpose (its own comment: "a backward wall-clock step here only refreshes an external late;
it cannot strand one"). Now that `fetched_at` is ratchet-stamped, a row written during an active
backward-step skew can read slightly *ahead* of true real time, same direction `jobs.now()` always
biases in — so `s.fetched_at <= cutoff` becomes marginally less likely to fire in that narrow,
self-correcting window (skew clears once real elapsed time exceeds the step's magnitude). That is the
exact "refreshes late, cannot strand" outcome the sweep's own comment already accepts, not a new
instance of the tombstone-clobber bug class this section exists to close — so it is left as-is rather
than folded into this fix.

### The one thing reconciliation can't reconstruct: a submitted Batch

Almost all "what work remains" is *derivable* by scanning content vs derived outputs — **except a
submitted Claude Batch.** Once POSTed, that's money in flight (`batch_abc123`); a reconciliation
scan would see "not yet enriched" and **resubmit, double-spending.** So **batch handles and their
member jobs are persisted durably and resumed on restart** — re-poll the handle, ingest results,
mark done. This is the requirement that rules out an in-memory queue.

### Enrichment latency: interactive now, batch for bulk

A freshly-captured note enriches via **one immediate Haiku call** (seconds, full Haiku price — tiny
per note) so its tags/entities/edges appear promptly. Only **bulk / backfill / re-enrichment** goes
through the 50%-off **Batches API** (≤24h, non-interactive). Either way the **embedding lands in
seconds**, so the note is retrievable immediately regardless.

**How the immediate path stays a fast path, not a second job system (lode-npx.2, lode-a3x).**
`save()` enqueues the `enrich` job exactly like `embed` — no special case — so a `pending` row
exists the instant the version write commits. The capture path then opportunistically **claims and
runs that specific job inline** — the claim is scoped to the just-saved `target_version`, not just
the job's type, so a backlog of other pending enrich jobs (a burst of prior adds, an idle worker)
can never cause an unrelated older note to be claimed and enriched instead of the one just saved.
It reuses the identical claim primitive `lode work` uses (a single atomic
`UPDATE ... WHERE status = 'pending'`), narrowed with an `AND target_version = ?` clause, so it
never races the reconciliation scan: there is no window where the version is live but no enrich job
exists for `enrich_gap` to misdetect as missing and re-enqueue. If a concurrent `lode work` wins
that claim instead, the capture path simply does nothing further — the note is enriched a moment
later via the normal worker path rather than instantly. A failed immediate run is handled by the
job's own attempts/backoff/dead-letter accounting (the same state machine any worker-claimed job
uses), not by a bespoke retry in the CLI.

```mermaid
flowchart LR
    SAVE["save (txn)"] --> Q[("jobs<br>(SQLite, durable)")]
    REC["reconcile<br>startup + periodic"] -.->|enqueue gaps| Q
    Q --> CLAIM["claim<br>(single owner)"]
    CLAIM --> RUN["run<br>embed · enrich · refresh"]
    RUN -->|ok| DONE["done<br>(idempotent by key)"]
    RUN -->|transient err| RETRY["backoff + retry"]
    RETRY --> Q
    RUN -->|poison| DEAD["dead-letter<br>attempts, last_error → UI"]
    RUN -.->|enrich = batch| BATCH["submit Batch<br>persist handle"]
    BATCH -.->|resume poll on restart| RUN

    classDef store fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef bad fill:#f2dede,stroke:#a94442,color:#1b1b1b;
    class Q store;
    class DEAD bad;
```

---

## Data shape (sketch)

*(§8)*

```
notes        note_id, head_version_id, no_egress, created              # logical identity
versions     version_id(=H(framed: note_id,parent,body)), note_id,
             parent_version_id, body, op(create|update|delete),
             purged_at?, created                                       # immutable, owned
externals    external_id, source_type, head_snapshot_id, no_egress,    # logical identity
             created
snapshots    snapshot_id(=H(framed: external_id,body)), external_id, body,
             raw_payload, fetched_at, status(ok|tombstone)             # immutable, mirrored
annotations  id, target(note_id|external_id), source_version,          # derived layer
             kind, payload, source(ai|user),
             status(fresh|stale|orphaned),
             model, prompt_ver, confidence, created
passages     passage_id, target_version(version_id|snapshot_id), ord,  # derived; heads only
             char_range, text, parent_block                            #   structure-aware chunks
embeddings   passage_id, vector, model                                 # derived; one per passage
edges        from, to, source(ai|user), reason, confidence,            # the knowledge graph
             source_version, status
jobs         id, type(embed|enrich|refresh), target_version,           # async work queue
             prompt_ver?, status(pending|running|done|failed|dead),    #   durable, single-owner
             attempts, last_error?, batch_handle?, claimed_at?,        #   lifecycle: pending->
             next_attempt_at, created                                  #   running->{done|failed->
                                                                       #   pending|dead}
egress_log   id, ts, purpose(enrich|qa), model,                        # cloud-egress audit trail
             sent_targets(version_id|passage_id …), redactions
```

`no_egress` on `notes`/`externals` marks content that is **indexed locally but never sent to Claude**
(no enrichment, excluded from cloud Q&A context — see [externals.md](externals.md#privacy-consequence-of-aggregation)).
`egress_log` records every time content leaves the box, so exposure is auditable.

The UI composes `content node + its annotations` at render time. Nothing is ever written back
into `versions.body` / `snapshots.body`.

**Passages are the retrieval unit** (see [retrieval.md](retrieval.md#chunking-passages-are-the-retrieval-unit)):
a version's body is chunked into structure-aware passages, each embedded and lexically indexed
separately, with `parent_block` recording the enclosing section/note for small-to-big context
expansion. They are **regenerable cache** — re-chunked and re-embedded from the body on every new
head version (deterministic, local, cheap). This is *distinct* from the §5 span-annotation
anchoring: passages are **regenerated per version**, never fuzzy-migrated like user span marks —
different lifecycles, do not conflate.

This maps onto the store ([stack.md](stack.md)), but **by rows, not by file**
([the partition is by rows](stack.md#the-partition-is-by-rows-not-by-file)). The **irreplaceable**
rows — `notes`, `versions`, `externals`, `snapshots`, plus the `annotations`/`edges` rows where
`source = user` — live in **SQLite**; the same file *also* holds rebuildable cache, so `cp lode.db`
is a harmless *superset* backup (the DB, vector store, logs, lock, and config all live under one
root, `$LODE_HOME` — see [configuration.md](configuration.md#paths--locations)). **Regenerable cache** — `passages`, `embeddings`, the `source = ai`
`annotations`/`edges`, and the lexical index — is rebuildable: passage vectors in **LanceDB**,
lexical in **SQLite FTS5** (also per passage), and the `edges` knowledge graph traversed **in-memory
with networkx** (loaded from the edge rows). The whole shape sits behind a thin repository interface, so the cache engine is
swappable without touching the core.

**The cache slot holds one engine that may be many.** The repository exposes a *single* cache behind
a two-method seam (`index` on each head change, `evict` on a delete tombstone), but the regenerable
cache is several engines that must all see every head change — passage vectors (LanceDB), the FTS5
lexical index, and later the networkx graph. They compose through a **`CompositeCache`** that is
itself a cache backend and simply fans each `index`/`evict` out to its member engines in order. So
the repository never grows a second slot or learns the engine list: adding the FTS leg (lode-x6r.4)
is appending one engine to the composite at the wiring point, and every engine — the vector
`EmbeddingCacheBackend`, the FTS index, the graph — plugs into the *same* seam rather than inventing
its own. Fan-out runs only after the irreplaceable write commits, so a failing engine costs a
rebuild, never data (lode-1f9).

**Capture-path cache composition (settled lode-xyb):** the `CompositeCache` wired into `cli.py add`
contains **only the `LexicalCacheBackend`** — the model-free FTS leg. `LexicalCacheBackend.index()`
calls `chunk()` (deterministic, no model) and writes both the `passages` rows (structure, char_range,
parent_block — needed by `expand_parents` on the read side) and the `passages_fts` FTS5 rows
synchronously after the version commit, so a just-saved note is keyword-findable and
context-expandable before any async work runs. The `EmbeddingCacheBackend` (vector leg) is **not** in
the capture-path composite — embedding stays async: only the `lode work` worker calls `embed()` to
produce LanceDB vectors. The reconciliation embed-gap signal is the embed job status (non-dead =
vector work is tracked or done; all-dead/missing = re-enqueue), because `passages` rows now exist
right after save and can no longer distinguish "embed ran" from "save ran."

The `jobs` table is **operational state** in SQLite: mostly regenerable (the reconciliation scan
rebuilds the backlog from the content↔derived diff), with **one durable exception — in-flight
`batch_handle`s**, which a scan can't reconstruct without double-spending. So it doesn't fit cleanly
on either side of the irreplaceable/regenerable line — another reason the partition is by *rows*,
not by *file* ([the partition is by rows](stack.md#the-partition-is-by-rows-not-by-file)).
