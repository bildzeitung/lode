# lode — Design

> An AI-first, TUI-first personal knowledge base for "things I learn during my day at work" —
> meeting notes, technical instructions, decisions. Fast to capture, intelligent to retrieve.

Status: **design captured, not yet built.** This document records the reasoning and the
decisions from the founding discussion so the build can proceed incrementally without
re-litigating settled questions.

---

## 1. The core problem

The economics of a personal notes pile are lopsided: **capture is cheap, messy, and frequent;
retrieval is rare and high-value.** Notes are usually write-once-read-never — not because
they're worthless, but because finding the right one later costs more than re-deriving it.

The job of AI here is to **flip that economics**: make capture worth it *because* retrieval
is trustworthy. Everything else is supporting.

The UI is a TUI precisely so capture stays instant: get in, dump what you learned, get out.
**No AI in the capture path.** Intelligence is async (background enrichment) or on-demand
(you ask). Capture stays dumb and fast forever.

---

## 2. The primary bet: grounded Q&A over your own notes

Not search — *answers*, with **citations back to the source notes**. Citations are
non-negotiable: they let you verify, and they preserve original context.

> "What did we decide about the auth migration?"
> "How do I rotate the staging certs?"

This is the feature that changes capture behaviour. Once you trust "I can always ask later,"
you stop agonising over filing and just dump. It is also the foundation every other feature
reuses — connection-surfacing, promotion, and synthesis are all "find related notes, then do
something."

**Build this first.** Embeddings on every note + cited Q&A retrieval.

### Supporting features (roughly in value order)

1. **Async enrichment at capture, never blocking.** On save, a background pass extracts
   entities (people, projects, systems, tickets), suggests a title, and tags. User leaves
   immediately; structuring happens after.
2. **Surfacing connections.** When writing a new note, passively show related past notes
   ("you wrote about this 3 weeks ago"). Where a personal KB beats a search box.
3. **Distillation of meeting notes** into decisions / action items / open questions.
4. **Transient → durable promotion.** Meeting notes are transient; technical instructions
   harden into durable runbooks. AI detects when scattered transient notes have become a
   reusable procedure and *proposes* promoting them into a canonical doc (dedup the five
   times you wrote the same deploy steps).
5. **Periodic synthesis.** "What did I learn this week" rollups.

### Explicitly NOT doing

- AI in the capture path (autocomplete, "improve my note", chat-to-add) — friction against
  the one thing a TUI is for.
- Over-automated tagging with no user override.
- Generating content the user didn't say. For a personal KB, fidelity to your own words is
  the value; hallucinated synthesis is worse than none. Hence the hard citation requirement.

---

## 3. The ownership boundary (foundational decision)

**The user does CRUD on notes. The AI never touches note content.** Anything the AI produces
— annotations, links, tags, extracted items, embeddings — lives in a **parallel derived layer**
keyed to the note. User notes are the source of truth; the AI layer is a sidecar that can be
regenerated or thrown away without ever risking the original.

Test of a clean separation: **drop the entire derived layer and you lose zero user data and
can rebuild it from the notes.**

This constraint doesn't simplify the design — it *forces* solving invalidation (§5).

---

## 4. Storage model: event-sourced, linear per-note chains

Notes are stored as an **append-only version chain**. Each mutating operation **at save time**
(create / update / delete — not per keystroke) creates a new **immutable node**.

- **create** → new root node
- **update** → new node parented to the prior version
- **delete** → a tombstone node (soft delete; recovery = repoint the head)

This was chosen specifically *because* the AI sidecar is the whole point. It hands us, for free:
immutability **by construction**, precise staleness, deterministic annotation migration, full
provenance, and undo. Without the AI layer this would be over-engineering; with it, it pays.

### Two graphs — do not conflate them

- **Version lineage** — per-note history. With the decisions below, this is a **linear chain**,
  not a branching DAG.
- **The knowledge graph** — links *between* notes (and later, external resources). This is the
  valuable graph and the actual product.

### Single-user / single-instance → linear chains, no merge

**Decision: single person, single instance, no sync.** Branches in a version graph only arise
from concurrent edits of the same note (multi-device offline → sync). We are explicitly *not*
doing that, which kills the only genuinely hard distributed problem (CRDT/merge conflict
resolution).

Therefore the version "graph" is a **linear chain per note**, built as one and refusing
branches by construction: **a save always parents the current head; reject the save if the
head moved underneath it.** Do not pay for merge semantics we will never use.

### Identity vs version

Two distinct ids:

- `note_id` — the **logical** note, stable across its whole lineage.
- `version_id` — the immutable node; `version_id` = **content hash** (content-addressed, so
  identical re-saves dedup for free).
- **head pointer**: `note_id → current version_id`.

Store **full content snapshot per save** (notes are small; do not prematurely delta-compress).
History grows forever — fine for years of personal notes. A compaction/squash policy can come
much later; content-addressing already dedups no-op saves.

---

## 5. Invalidation — the problem the ownership boundary forces

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

## 6. External sources & the knowledge graph

The AI annotation layer gains read access to external sources — **tickets, source repos, wikis,
email** — and **draws down explicitly linked web pages**, integrating everything into the
knowledge graph. This is added **incrementally, after the core loop works** (§7).

### Externals fit the *same* model

A fetched ticket / wiki page / email / web page is structurally **a note version**: an immutable,
point-in-time snapshot. The whole store collapses to one shape:

> immutable content nodes (some **owned** = your notes, some **mirrored** = externals)
> + a derived annotation/link layer + head pointers.

Axes on a content node: `origin: owned | mirrored`; on derived items: `source: ai | user`.

### The broken assumption: external staleness is NOT topological

For owned notes, staleness is free (head moved → you know instantly). For externals, **the true
head lives on someone else's server and changes without telling you.** Consequences:

- **Externals need a refresh policy** (TTL / on-access revalidation / webhook) — there is no
  structural staleness signal.
- **Every AI claim from an external must cite "as of `fetched_at`."** "The ticket is open" is a
  lie; "the ticket was open as of last sync, 3 days ago" is honest.
- **Retrieval uses an explicit trust gradient**, in both ranking and citation display:
  **your note > your annotation > current external snapshot > stale external snapshot >
  AI-inferred edge.** The user's own words are highest-trust; externals corroborate, they do
  not override.

### External identity — same two-id split

- `external_id` — stable logical identity: `JIRA-1234`, `repo@path@commit`, email `Message-ID`,
  normalized URL.
- `snapshot_id` — immutable fetched version (content hash).
- One canonical node per `external_id` with many edges — never five copies of a ticket linked
  from five notes. Dedup on `external_id`; version on `snapshot_id`.

### Edges: explicit vs inferred

- **Explicit** (a note cites `JIRA-1234` or pastes a URL): high confidence, user-asserted edge.
- **Inferred** (AI decides "the auth migration" *is* PR #42): a **suggestion** (`source: ai`,
  confidence-scored), **never an asserted fact**. Surface for confirmation; a user nod promotes
  it. This is where a hallucinated link would silently corrupt the graph — keep it gated.

### Draw-down rules

- **Follow explicit links one hop, then stop.** Pull the linked page, extract *its* entities,
  but do not follow that page's links outward. Recursion = unbounded web crawler, not a notes app.
- **Readability extraction + graceful failure.** Many pages (JS-rendered, paywalled, 403) return
  scaffolding to a naive GET; strip nav/ads, snapshot cleaned text (+ optional raw HTML), and on
  failure write a tombstone snapshot rather than garbage.

### Retrieval over the heterogeneous graph

Index **heads only** — for notes, the current head version; for externals, the latest snapshot,
marked with its age. Never index full history (a note edited 5× would return 5 near-duplicate
hits and cite a stale copy). History exists for audit, undo, and annotation migration — not
retrieval.

### Link-rot immunity (the payoff that justifies draw-down)

Because we **snapshot** externals instead of storing bare URLs, the knowledge graph is **immune
to link rot**: when the ticket is deleted, the wiki reorganised, the page taken down, the
mirrored snapshot — and everything the AI derived from it — survives. The opposite of bookmarks.
**Principle: always snapshot, never store a bare URL.**

### Privacy (consequence of aggregation)

Single-user does not mean low-stakes. Once this box holds embeddings of email + internal tickets
+ repo contents, it is a concentrated high-value target, and that content is shipped to an LLM
for enrichment and Q&A. Therefore:

- Be deliberate about **what text leaves the machine** to the model.
- **Redact obvious secrets (keys, tokens) before embedding** — a pasted `.env` or API key must
  not end up vectorised and retrievable.
- Care for local-at-rest storage.

---

## 7. Build sequencing

Incremental, core-first. The graph is the easy part; **connectors are the hard, rot-prone part**
(auth, rate limits, pagination, alien data models). Do not fan out into six mediocre connectors
before the core loop works.

1. **Notes + version chains + derived layer + note↔note knowledge graph + cited Q&A.**
   Externals = none. This is the whole value on its own.
2. **Then connectors, one at a time**, starting with whichever source the notes actually
   reference most (likely tickets or the repo, not email). Normalize every connector to one
   interface — `(external_id, snapshot, fetched_at, source_type, raw_payload)` — so the graph
   never learns source-specific quirks and adding source N+1 doesn't touch the core.
3. Fan out only after the single-connector loop genuinely works end-to-end.

---

## 8. Data shape (sketch)

```
notes        note_id, head_version_id, created                         # logical identity
versions     version_id(=hash), note_id, parent_version_id, body,
             op(create|update|delete), created                         # immutable, owned
externals    external_id, source_type, head_snapshot_id, created       # logical identity
snapshots    snapshot_id(=hash), external_id, body, raw_payload,
             fetched_at, status(ok|tombstone)                          # immutable, mirrored
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

---

## 9. Open decisions (deferred, not forgotten)

- **External refresh: on-access revalidation vs. scheduled background refresh.** Leaning
  **on-access with a short TTL cache** for a single instance with finite API quota — but it's
  really a per-source judgment (a closed ticket changes rarely; an active PR changes hourly).
  Decide per connector when building it.
- **History compaction / squash policy.** Not needed for years; revisit if storage matters.
- **Span-annotation fuzzy re-anchor threshold** — tune when span annotations are actually built.

---

## 10. Stack (TBD)

Not yet decided. Constraints implied by the above: a TUI front-end; local-first storage that
naturally supports content-addressed immutable nodes + a head pointer (SQLite is the obvious
default); a vector index for embeddings; an LLM for enrichment + Q&A. To be chosen when build
step 1 starts.
