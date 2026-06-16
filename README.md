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

## Status

**Design captured, not yet built.** See [`docs/design.md`](docs/design.md) for the full
architecture and the reasoning behind every decision. Build is incremental: notes + cited Q&A
first, external connectors added one at a time afterward.
