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
   immediately; structuring happens after. **Extraction is a Claude pass** (Haiku, structured
   outputs — §10), recorded with full provenance (`model`, `prompt_ver`, source `version_id`) —
   never a storage-engine black box, so it stays auditable and re-runnable on a model upgrade. The
   same pass proposes inferred edges (§6), gated as suggestions.
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

Test of a clean separation: **drop the AI-derived cache — embeddings, AI annotations, inferred
edges — and you lose zero user data; it rebuilds from the notes.**

**One caveat the build must honor:** *user* corrections (`source: user` — a fixed tag, a confirmed
or deleted link) live in the derived layer but are **not** AI output and **not** regenerable —
they're genuine user decisions. So the real partition is not *owned vs derived* but
**irreplaceable** (owned content **+** user curation) **vs regenerable cache** (everything the AI
produced). The irreplaceable set is what must be backed up; the cache is rebuildable (§10).

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
  not end up vectorised and retrievable. (Preventive half.)
- Care for local-at-rest storage.

### Hard delete — the deliberate immutability break (corrective half)

Append-only + content-addressing means a pasted secret otherwise lives in `versions.body`
**forever**, and a normal delete only writes a tombstone — the bytes survive. Because this box
aggregates sensitive data, there must be an escape hatch that **violates immutability on purpose**:

- **`purge` operates at version or whole-note granularity** (v1 — substring/span redaction is
  deferred, §9). It overwrites the body of the targeted version(s) with a redaction marker
  (`[purged YYYY-MM-DD]`) and sets `purged_at`. Node identity, `parent_version_id`, `op`, and
  `created` are **kept**, so lineage and undo structure survive — only the sensitive bytes die.
- **It sweeps the note's whole chain**, including soft-deleted (tombstoned) notes — a secret pasted
  then edited-around persists in older versions.
- **It cascades to the cache:** drop every derived entry referencing the purged versions (LanceDB
  vectors, FTS rows, `source: ai` annotations), then re-derive cheaply/locally so nothing leaks
  through the index. `source: user` annotations stay (metadata, not content).
- **Hash consequence (accepted):** a purged body no longer hashes to its `version_id`; that id stays
  as the historical identifier, flagged `purged`, and is no longer recomputable. This is the cost of
  an explicit immutability break, taken knowingly.

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

This maps onto the split store (§10). **Irreplaceable** — `notes`, `versions`, `externals`,
`snapshots`, plus the `annotations`/`edges` rows where `source = user` — lives in **SQLite**
(one-file backup). **Regenerable cache** — `embeddings`, the `source = ai` `annotations`/`edges`,
and the lexical index — is rebuildable: vectors in **LanceDB**, lexical in **SQLite FTS5**, and the
`edges` knowledge graph traversed **in-memory with networkx** (loaded from the edge rows). The whole
shape sits behind a thin repository interface, so the cache engine is swappable without touching the
core.

---

## 9. Open decisions (deferred, not forgotten)

- **External refresh: on-access revalidation vs. scheduled background refresh.** Leaning
  **on-access with a short TTL cache** for a single instance with finite API quota — but it's
  really a per-source judgment (a closed ticket changes rarely; an active PR changes hourly).
  Decide per connector when building it.
- **History compaction / squash policy.** Not needed for years; revisit if storage matters.
- **Cache rebuild cost is non-uniform** (§10 chart). Embeddings / lexical / explicit edges rebuild
  cheaply (local, minutes); AI annotations + inferred edges cost real dollars + hours (Claude
  Batches) to regenerate from scratch. Decide whether to *snapshot* the LLM tier of the cache purely
  to skip recompute on restore — not for correctness, only to dodge the cost.
- **LanceDB maturity.** Younger / faster-moving than the rest of the stack; acceptable because the
  cache is disposable and lives behind the repository interface. Watch for breaking changes;
  sqlite-vec is the simpler fallback-down if it churns too hard.
- **Span-annotation fuzzy re-anchor threshold** — tune when span annotations are actually built.
- **Substring/span redaction** (upgrade to §6's hard delete). v1 purges at version/note
  granularity; surgical "redact this string everywhere it appears, keep the rest of the note" is
  deferred as YAGNI. Revisit if coarse purge proves too lossy in practice.

---

## 10. Stack (decided)

Chosen at founding; rationale where non-obvious. Most choices follow the existing job-harness
ecosystem (Python + Textual + Typer + SQLite) so there's no new framework risk.

| Layer | Choice | Notes |
|---|---|---|
| Language | **Python** | Richest LLM/embedding tooling; matches the existing harness |
| TUI | **Textual** | Already a proven front-end in the sibling project |
| CLI | **Typer** | Repo convention (never argparse) |
| **Irreplaceable store** | **SQLite** (one file) | Holds everything that can't be regenerated: owned content (`notes`/`versions`/`externals`/`snapshots`) **and** user curation (`annotations`/`edges` where `source = user`), plus the **FTS5** lexical index. Tiny, durable, **backup = copy one file** |
| **Regenerable cache** | **LanceDB** (vectors) + **networkx** (graph, in-memory) | Disposable, rebuildable from the notes. LanceDB: columnar on-disk embeddings with a real ANN index, metadata filtering, and **native hybrid lexical+vector fusion** (higher-quality retrieval — see §6). Graph traversal runs in-memory via networkx over the edge rows — no graph server. AI annotation/edge rows live in SQLite alongside the rest. Behind a thin **repository interface**, so the cache engine is swappable (sqlite-vec is the simpler fallback-down) |
| Embeddings | **Local, on-machine** | Open model via fastembed/ONNX (e.g. `nomic-embed-text-v1.5`, `bge-*`) — CPU-only, no torch. **Chosen specifically to honor §6**: note/email/ticket content is never sent off-box. The resulting vectors land in LanceDB. Accepts slightly lower retrieval quality + a bundled model file (~100–500MB) in exchange |
| Enrichment LLM | **Claude Haiku 4.5** (`claude-haiku-4-5`, $1/$5 per Mtok) | High-volume background tagging/extraction. Use **structured outputs** (`output_config.format` + Pydantic) so the derived layer gets validated JSON. Bulk re-enrichment goes through the **Batches API** (50% off, non-interactive) |
| Q&A LLM | **Claude Sonnet 4.6** default (`claude-sonnet-4-6`, $3/$15); **Opus 4.8** (`claude-opus-4-8`, $5/$25) as a "think harder" toggle | Low-volume, interactive, quality-sensitive synthesis. Every answer grounded in retrieved note text; citations required in the response schema |

**Why a split store (decided after evaluating a unified Oracle AI Database 26ai, plus
SQLite+sqlite-vec, SurrealDB, FalkorDB, Neo4j, Postgres+pgvector+AGE):** the ownership boundary
(§3) already partitions the data by *value*, so the storage follows it. The irreplaceable set is
**tiny and structurally trivial** but must be durable and trivial to back up — SQLite is the ideal
fit (one file, atomic, restore-anywhere). The cache is **heavy but disposable**, so it optimizes for
*retrieval quality and feature fit*, not durability — which frees the pick to the best embedded
tools (LanceDB + networkx) with no server, licensing, or unpatched-security risk.

A single unified engine (the original Oracle 26ai choice) was rejected because it inverted that
match: it put the **heaviest, least-durability-critical machinery under the most sensitive data**.
Oracle Free is unsupported/unpatched (security included) on a box aggregating email + tickets + repo
contents; it makes backup a full-DB dump instead of a file copy; and it front-loads the heaviest
yak-shaving onto an MVP (build step 1, §7) that needs none of its differentiators (the note↔note
graph fits in memory; entity extraction is Claude's job — with provenance — not a DB black box).

**The derived layer is not uniformly disposable** — three rebuildable tiers, plus one
non-regenerable exception that belongs with the irreplaceable set:

| Derived item | Rebuilt by | Regeneration cost |
|---|---|---|
| Embeddings | Local CPU model over head nodes | **Cheap** — minutes for thousands of notes, tens of minutes for ~100k. No dollars, no network. |
| Lexical (FTS5) + explicit edges | Deterministic re-parse | **Trivial** — pure computation, no model. |
| AI annotations + inferred edges | Claude Haiku via the Batches API | **Real $ + hours** — ~tens of dollars per ~10k notes, non-interactive. Not prohibitive, but not free. |
| **User curation** (`source = user`) | — (not derived from anything) | **Not regenerable.** A fixed tag, a confirmed or deleted link — genuine user decisions. Stored with the irreplaceable set in SQLite. |

So "drop the derived layer and lose nothing" holds only for the first three tiers; user curation is
irreplaceable.

**Backup stays simple:** copy the one SQLite file and you have the entire irreplaceable set (owned
content + user curation). The cache is never *required* in a backup — losing it costs a rebuild,
never data. Optionally snapshot just the LLM tier of the cache to skip the dollars + hours of
re-enrichment on restore (§9) — an optimization, not a correctness need.

**Keep the cache behind a repository interface.** The §8 shape is engine-agnostic; the access layer
hides the cache engine so LanceDB can be swapped (sqlite-vec is the simpler fallback-down) without
touching the core.

**Embeddings reality check:** Anthropic has **no first-party embeddings API**, so embeddings was
always going to be a separate runtime decision regardless of using Claude for the LLM work. Going
local resolves it in favor of the privacy principle; LanceDB just stores the resulting vectors.

**Auth:** no hardcoded `ANTHROPIC_API_KEY` — resolve from env or an `ant auth login` profile, same
as the harness.

**Model-tier split mirrors the harness:** cheap/deterministic high-volume work on Haiku;
judgment-sensitive synthesis on Sonnet/Opus.
