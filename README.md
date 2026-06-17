# lode

An AI-first, TUI-first personal knowledge base for **things you learn during your day at work** —
meeting notes, technical instructions, decisions. Fast to capture, intelligent to retrieve.

> The mother lode: a rich vein you accumulate and mine. An append-only log of notes + a derived
> knowledge graph — the ore and the assay.

```
$ lode add
$ lode ask "what did we decide about auth?"
```

## The idea in one breath

Capture is cheap and frequent; retrieval is rare and valuable. The whole point of the AI is to
make capture worth it *because* you trust you can always **ask later** and get a cited answer
grounded in your own notes. The TUI keeps capture instant; intelligence runs async or on demand —
**never in the capture path.**

## Core principles

- **You own the notes; the AI never touches them.** Everything the AI produces — annotations,
  links, tags, embeddings — lives in a parallel **derived layer** that can be regenerated or
  thrown away without risking a single character of your content.
- **Append-only, immutable history.** Every save (create/update/delete) writes a new immutable,
  content-addressed node. Single-user, single-instance, no sync → simple linear history per note.
- **Answers, with citations.** Retrieval always points back to the source note, "as of" a known
  version. Fidelity over fluency.
- **Externals are snapshotted, never bookmarked.** Tickets, repos, wikis, email, and linked web
  pages get mirrored as immutable snapshots, so the knowledge graph is immune to link rot.

## Stack

A **split** store that follows the ownership boundary. The **irreplaceable** set — your notes and
your own corrections — lives in one **SQLite** file (backup = copy the file). The **regenerable
cache** — embeddings, AI annotations, the knowledge graph — is rebuildable from the notes, so it
optimizes for retrieval quality: **LanceDB** for vectors, **SQLite FTS5** for lexical, fused
app-side (RRF) with a local cross-encoder **reranker**, and **networkx** for in-memory graph
traversal. Accessed from **Python**
behind a thin repository interface, with a **Textual** TUI. Indexing is fully on-box — embeddings,
reranking, and citation-checking all run **locally** (fastembed/ONNX), so **content never leaves the
box for indexing or retrieval**. **Claude** does background enrichment (Haiku 4.5) and cited Q&A
(Sonnet 4.6 / Opus 4.8) — these are *explicit, logged egress*, and notes/sources marked `no_egress`
are kept local and never sent to the cloud. See [`docs/stack.md`](docs/stack.md) for the full
rationale (including why a split store over a unified Oracle/Postgres engine) and
[`docs/externals.md`](docs/externals.md#privacy-consequence-of-aggregation) for the privacy model.

## Status

**Design captured, not yet built.** See [`docs/design.md`](docs/design.md) for the overview and a
map of the design docs, with the reasoning behind every decision. Build is incremental: notes +
cited Q&A first, external connectors added one at a time afterward.

## Working on the docs

The design docs contain [Mermaid](https://mermaid.js.org/) diagrams. They're validated against the
same parser GitHub renders with, via the **`minlag/mermaid-cli`** Docker image — no Node/Chromium
toolchain needed on the host:

```bash
scripts/update-images.sh      # pull the mermaid-cli image (one-time / on update)
scripts/validate-mermaid.sh   # parse every ```mermaid block in docs/, fail on syntax errors
```

`docker` is the only host requirement.
