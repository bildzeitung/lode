---
title: lode
description: An AI-first, TUI-first personal knowledge base for the things you learn during your day at work.
---

<!--
  Landing page / docs-site index (lode-fhql.10).

  README.md is the CANONICAL pitch -- it is what every GitHub visitor sees
  first, and it is read on GitHub whether or not the docs site exists. This
  page is a DERIVED restatement for the site, not a second, independently
  maintained pitch: the positioning line, the name story, the two-line demo,
  and "The idea in one breath" section below are reused verbatim from
  README.md, never rewritten into separate marketing copy. If either changes,
  update both in the same commit -- see the sync note in
  docs/stack.md#docs-site-generator-lode-fhql8 (the "Landing page / README
  sync" subsection) for the full contract.
-->

<p align="center">
  <img src="assets/lockup.svg" alt="lode" width="220">
</p>

<p align="center">
<strong>lode</strong> is an AI-first, TUI-first personal knowledge base for the things you learn
during your day at work. Fast to capture, intelligent to retrieve.
</p>

> The mother lode: a rich vein you accumulate and mine. An append-only log of notes + a derived
> knowledge graph -- the ore and the assay.

Meeting notes, technical instructions, decisions -- captured fast, retrieved with citations.

```
$ lode add
$ lode ask "what did we decide about auth?"
```

## Install

lode does not publish to PyPI, so `pip install lode` installs a different, unrelated project --
see [release.md's Non-goals](release.md#non-goals).

**From a release (no clone needed).** Download the wheel from the
[GitHub releases page](https://github.com/bildzeitung/lode/releases/latest) and install it
directly:

```bash
pip install lode-*.whl   # the wheel attached to the release you downloaded
```

**From source (for contributing or tracking `trunk`).**

```bash
git clone https://github.com/bildzeitung/lode.git
cd lode
./scripts/python-init.sh
. ./venv/bin/activate
```

## The idea in one breath

Capture is cheap and frequent; retrieval is rare and valuable. The whole point of the AI is to
make capture worth it *because* you trust you can always **ask later** and get a cited answer
grounded in your own notes. The TUI keeps capture instant; intelligence runs async or on demand —
**never in the capture path.**

## Read more

- [Design](design.md) -- the core problem, the primary bet, principles, and build sequencing
- [Storage](storage.md) -- the ownership boundary, event-sourced version chains, the data shape
- [Retrieval](retrieval.md) -- the hybrid retrieval pipeline: FTS5 + vectors, rerank, graph expand
- [Externals](externals.md) -- external sources, the knowledge graph, link-rot immunity, privacy
- [Brand](brand.md) -- the brand brief: positioning, palette, type, voice
- [How-to guides](how-to/README.md) -- task-oriented recipes

---

<sub>
This page mirrors [README.md](https://github.com/bildzeitung/lode/blob/trunk/README.md), the
canonical pitch read on GitHub. See the sync note in
[docs/stack.md](https://github.com/bildzeitung/lode/blob/trunk/docs/stack.md) for how the two stay
in sync.
</sub>
