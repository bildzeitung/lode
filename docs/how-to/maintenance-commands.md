# How to fix a stale index: `reembed`, `reenrich`, `reindex-lexical`

> The full mechanism and its rationale live in
> [`storage.md`](../storage.md#re-embedding-the-corpus-deliberately-lode-g2747) and
> [`storage.md`](../storage.md#re-enriching-the-corpus-deliberately-targeted-lode-14jr). This
> guide is only *which command fixes which signal*.

Three commands force regeneration of derived index state after something outside your notes
changed — an embedder or enrichment-LLM upgrade, a cache eviction, or an index-time bug. `lode
status` is what tells you one of them is needed; this page is the depth pointer it links to. If
you're deciding *whether* to run a command, `lode status`'s hint and the command's own `--help`
should already be enough — this page exists for the fuller "what does it actually do" story.

## `lode reembed`

**`lode status` signal:** "the index is mixed" (more than one model revision is live) or "the
embedder's cached weights have moved past the revision your vectors were embedded with."

**What it does:** enqueues a fresh `embed` job for *every* live head — every note's current version
and every external's current snapshot — unconditionally. There's no
whole-corpus-vs-targeted flag, because the triggering event (a model or cache change) is itself
corpus-wide, not per-note. It rebuilds vectors in place rather than building a parallel index and
swapping it in.

**Queue vs. execute:** queues only. It enqueues the jobs and returns; running them is `lode
work`'s job (`lode work` or `lode work --wait`). It's resumable by construction — re-running `lode
work` after an interruption picks up where the queue left off, so there's no need to re-run
`reembed` itself.

**What it does NOT touch:** the enrichment (`enrich`) leg, and the lexical/FTS leg — the latter is
synchronous and model-free, so a model change can't make it stale in the first place. An
enrichment-model mismatch is a separate signal handled by `reenrich`, not this command.

## `lode reenrich`

**`lode status` signal:** "the enrichment store's AI annotations disagree with the currently
configured enrichment_llm — run `lode reenrich`."

**What it does:** enqueues a fresh `enrich` job, but only for the heads whose stored annotations
actually disagree with the currently configured `enrichment_llm`/provider — targeted, not
whole-corpus, unlike `reembed`. That's a deliberate cost tradeoff: enrichment calls a cloud LLM, so
re-running it for the whole corpus on every config change would be needlessly expensive. A head
with no annotations at all yet is left to the passive gap-filling reconcile step rather than
duplicated here.

**Queue vs. execute:** queues only, same as `reembed` — `lode work` runs the enqueued jobs, and
the process is resumable the same way.

**What it does NOT touch:** the `embed` queue (never enqueues embed jobs), and it never sweeps in
`no_egress` content — notes/externals marked `no_egress` are excluded even if their stored
annotations happen to be stale, because they're never sent to the cloud enrichment LLM at all.

## `lode reindex-lexical`

**`lode status` signal:** "N live note head(s) have no lexical (FTS5) index rows — run `lode
reindex-lexical` to make them keyword-findable now, or wait for `lode work` to heal them
automatically on its next reconcile pass."

**What it does:** rebuilds the `passages_fts` lexical index for every live *note* head from its
current body — not just the heads missing rows, so it's a repair tool as much as a backfill.
Unlike the embed/enrich staleness signals, a lexical gap self-heals on its own over time via the
normal reconcile pass; this command is purely for making it happen *now* instead of waiting.

**Queue vs. execute:** synchronous, no queue involved. Chunking and the FTS5 write are the same
model-free path the reconcile step already uses in-process; there's no `lode work` step because
there's nothing left to drain once the command returns.

**What it does NOT touch:** externals. External snapshot FTS rows are written at fetch time by the
externals ingest path, not by this command, which only walks notes/versions. Purged notes are the
one deliberate exception to "skip purged content": a hard purge already re-indexes the surviving
`[purged ...]` marker body, so this command re-indexes that marker too rather than skipping it —
which keeps it faithful to the save path it exists to reproduce. It never re-indexes the purged
content itself; that is gone.

## Summary

| Command | Triggering `lode status` signal | Scope | Queues (`lode work`) or executes now |
|---|---|---|---|
| `lode reembed` | index mixed / revision drift | every live head — notes + externals | queues `embed` jobs |
| `lode reenrich` | enrichment-LLM mismatch | only live heads with stale annotations — notes + externals, excluding `no_egress` | queues `enrich` jobs |
| `lode reindex-lexical` | missing lexical (FTS5) rows | every live note head — notes only | executes immediately, no queue |
