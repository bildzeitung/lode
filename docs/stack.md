# lode — Stack (decided)

*(§10)* Chosen at founding; rationale where non-obvious. Most choices follow the existing
job-harness ecosystem (Python + Textual + Typer + SQLite) so there's no new framework risk. This
is the storage realization of the ownership boundary and data shape in [storage.md](storage.md).

| Layer | Choice | Notes |
|---|---|---|
| Language | **Python** | Richest LLM/embedding tooling; matches the existing harness |
| TUI | **Textual** | Already a proven front-end in the sibling project |
| CLI | **Typer** | Repo convention (never argparse) |
| **Irreplaceable store** | **SQLite** (one file) | Holds everything that can't be regenerated: owned content (`notes`/`versions`/`externals`/`snapshots`) **and** user curation (`annotations`/`edges` where `source = user`), plus the **FTS5** lexical index. Tiny, durable, **backup = copy one file** |
| **Regenerable cache** | **LanceDB** (vectors) + **networkx** (graph, in-memory) | Disposable, rebuildable from the notes. LanceDB: columnar on-disk embeddings with a real ANN index and metadata filtering (its native hybrid is **unused** — lexical stays in FTS5; fusion is app-side RRF, see [retrieval.md](retrieval.md)). Graph traversal runs in-memory via networkx over the edge rows — no graph server. AI annotation/edge rows live in SQLite alongside the rest. Behind a thin **repository interface**, so the cache engine is swappable (sqlite-vec is the simpler fallback-down) |
| Embeddings | **Local, on-machine** | Open model via fastembed/ONNX (e.g. `nomic-embed-text-v1.5`, `bge-*`) — CPU-only, no torch. **Chosen specifically to honor [privacy](externals.md#privacy)**: note/email/ticket content is never sent off-box *for indexing*. The resulting vectors land in LanceDB. Accepts slightly lower retrieval quality + a bundled model file (~100–500MB) in exchange |
| Reranker | **Local cross-encoder** (e.g. `bge-reranker-v2-m3`) | First-class retrieval stage ([retrieval.md](retrieval.md)), wired in v1 behind a toggle. Runs on the **same ONNX runtime** as embeddings — no new stack, content stays on-box. Biggest single quality lever for cited Q&A; model/threshold tuning deferred until there's a corpus ([decisions.md](decisions.md)) |
| Faithfulness NLI | **Local NLI / cross-encoder** (e.g. `bge-reranker-v2-m3` repurposed, or a dedicated NLI model) | Entailment leg of the [faithfulness gate](retrieval.md#faithfulness-verify-citations-dont-just-require-them): scores whether cited spans jointly entail a synthesized claim, so multi-note synthesis is answered rather than refused. Same **ONNX runtime**, on-box. Ships in v1 **conservative and fail-closed**; the acceptance **threshold ships untuned** and is revisited against the eval harness ([decisions.md](decisions.md)) |
| Enrichment LLM | **Claude Haiku 4.5** (`claude-haiku-4-5`, $1/$5 per Mtok) | High-volume background tagging/extraction. Use **structured outputs** (`output_config.format` + Pydantic) so the derived layer gets validated JSON. Bulk re-enrichment goes through the **Batches API** (50% off, non-interactive) |
| Q&A LLM | **Claude Sonnet 4.6** default (`claude-sonnet-4-6`, $3/$15); **Opus 4.8** (`claude-opus-4-8`, $5/$25) as a "think harder" toggle | Low-volume, interactive, quality-sensitive synthesis. Returns **structured claims**, each pinned to a verbatim span of a specific version; a [faithfulness gate](retrieval.md#faithfulness-verify-citations-dont-just-require-them) verifies the evidence and abstains rather than emit an unsupported claim — citations are enforced by *verification*, not just by the response schema |

---

## Why a split store

*(decided after evaluating a unified Oracle AI Database 26ai, plus SQLite+sqlite-vec, SurrealDB,
FalkorDB, Neo4j, Postgres+pgvector+AGE)*

The ownership boundary ([storage.md](storage.md#the-ownership-boundary)) already partitions the data
by *value*, so the storage follows it. The irreplaceable set is **tiny and structurally trivial**
but must be durable and trivial to back up — SQLite is the ideal fit (one file, atomic,
restore-anywhere). The cache is **heavy but disposable**, so it optimizes for *retrieval quality and
feature fit*, not durability — which frees the pick to the best embedded tools (LanceDB + networkx)
with no server, licensing, or unpatched-security risk.

A single unified engine (the original Oracle 26ai choice) was rejected because it inverted that
match: it put the **heaviest, least-durability-critical machinery under the most sensitive data**.
Oracle Free is unsupported/unpatched (security included) on a box aggregating email + tickets + repo
contents; it makes backup a full-DB dump instead of a file copy; and it front-loads the heaviest
yak-shaving onto an MVP (build step 1, [design.md](design.md) §7) that needs none of its
differentiators (the note↔note graph fits in memory; entity extraction is Claude's job — with
provenance — not a DB black box).

---

## The derived layer is not uniformly disposable

Three rebuildable tiers, plus one non-regenerable exception that belongs with the irreplaceable set:

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
re-enrichment on restore ([decisions.md](decisions.md)) — an optimization, not a correctness need.

**Keep the cache behind a repository interface.** The [data shape](storage.md#data-shape-sketch) is
engine-agnostic; the access layer hides the cache engine so LanceDB can be swapped (sqlite-vec is
the simpler fallback-down) without touching the core.

**Embeddings reality check:** Anthropic has **no first-party embeddings API**, so embeddings was
always going to be a separate runtime decision regardless of using Claude for the LLM work. Going
local resolves it in favor of the privacy principle; LanceDB just stores the resulting vectors.

**Auth:** no hardcoded `ANTHROPIC_API_KEY` — resolve from env or an `ant auth login` profile, same
as the harness.

**Model-tier split mirrors the harness:** cheap/deterministic high-volume work on Haiku;
judgment-sensitive synthesis on Sonnet/Opus.
