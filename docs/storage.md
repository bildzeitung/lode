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

### Two graphs — do not conflate them

- **Version lineage** — per-note history. With the decisions below, this is a **linear chain**,
  not a branching DAG.
- **The knowledge graph** — links *between* notes (and later, external resources). This is the
  valuable graph and the actual product. It lives in [externals.md](externals.md).

### Single-user / single-instance → linear chains, no merge

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

### Identity vs version

Two distinct ids:

- `note_id` — the **logical** note, stable across its whole lineage.
- `version_id` — the immutable node; `version_id` = **`hash(note_id ‖ parent_version_id ‖ body)`**
  (git's model). Folding in `note_id` makes cross-note collisions impossible (two different notes
  both containing `"TODO"` would otherwise alias); folding in the parent keeps each chain position
  distinct even on a revert to an earlier body (otherwise the reverted node aliases the original and
  `parent_version_id` becomes ambiguous).
- **head pointer**: `note_id → current version_id`.

**Dedup of no-op saves is an explicit guard, not hash luck:** before writing, compare the proposed
body to the *head's* body; if equal, return the head and write nothing. (With the parent-inclusive
hash a re-save parents the current head, so it would *not* auto-collide — the dedup has to be an
explicit check.)

Store **full content snapshot per save** (notes are small; do not prematurely delta-compress).
History grows forever — fine for years of personal notes. A compaction/squash policy can come
much later; the no-op-save guard above keeps the chain from growing on saves that change nothing.

---

## Invalidation — the problem the ownership boundary forces

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

### Stale-display policy (decided)

- **Tags / links:** show, but flagged stale (avoids UI flicker on every typo fix).
- **Assertive items (extracted action items, etc.):** hide until re-enrichment is fresh — the
  cost of a wrong action item is higher than a wrong tag.

Stale annotations are never treated as ground truth.

### Provenance & user override

- **Provenance on every annotation:** model id, prompt/version, source `version_id`, timestamp,
  confidence. Enables re-running enrichment after a model upgrade, auditing a bad link, bulk
  purge. Cheap now, painful to retrofit.
- **`source: ai | user` on the annotation layer.** Users *will* correct an AI tag or link. That
  correction is still metadata (doesn't touch note content), and it is **pinned**:
  - **AI annotations are version-scoped** — regenerable, allowed to go stale, re-derived per head.
  - **User annotations attach to `note_id`** (the logical identity) — they ride across every
    version automatically, so re-enrichment never re-adds a link the user just removed.

---

## Data shape (sketch)

*(§8)*

```
notes        note_id, head_version_id, created                         # logical identity
versions     version_id(=hash(note_id‖parent‖body)), note_id,
             parent_version_id, body, op(create|update|delete),
             purged_at?, created                                       # immutable, owned
externals    external_id, source_type, head_snapshot_id, created       # logical identity
snapshots    snapshot_id(=hash(external_id‖body)), external_id, body,
             raw_payload, fetched_at, status(ok|tombstone)             # immutable, mirrored
annotations  id, target(note_id|external_id), source_version,          # derived layer
             kind, payload, source(ai|user),
             status(fresh|stale|orphaned),
             model, prompt_ver, confidence, created
embeddings   target_version(version_id|snapshot_id), vector, model     # derived; heads only
edges        from, to, source(ai|user), reason, confidence,            # the knowledge graph
             source_version, status
```

The UI composes `content node + its annotations` at render time. Nothing is ever written back
into `versions.body` / `snapshots.body`.

This maps onto the split store ([stack.md](stack.md)). **Irreplaceable** — `notes`, `versions`,
`externals`, `snapshots`, plus the `annotations`/`edges` rows where `source = user` — lives in
**SQLite** (one-file backup). **Regenerable cache** — `embeddings`, the `source = ai`
`annotations`/`edges`, and the lexical index — is rebuildable: vectors in **LanceDB**, lexical in
**SQLite FTS5**, and the `edges` knowledge graph traversed **in-memory with networkx** (loaded from
the edge rows). The whole shape sits behind a thin repository interface, so the cache engine is
swappable without touching the core.
