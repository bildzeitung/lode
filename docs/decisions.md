# lode — Open decisions (deferred, not forgotten)

*(§9)* Decisions deliberately left open, with the current leaning where there is one. Revisit each
when the build reaches the feature that forces it. The tunable parameters several of these reference
are catalogued in [configuration.md](configuration.md).

- **External refresh: on-access revalidation vs. scheduled background refresh.** Leaning
  **on-access with a short TTL cache** for a single instance with finite API quota — but it's
  really a per-source judgment (a closed ticket changes rarely; an active PR changes hourly).
  Decide per connector when building it. ([externals.md](externals.md#the-broken-assumption-external-staleness-is-not-topological))
- **History compaction / squash policy.** Not needed for years; revisit if storage matters.
  ([storage.md](storage.md#identity-vs-version))
- **Minimal / archival backup export.** v1 backup is `cp lode.db` — a superset copy that drags
  rebuildable cache (harmless). A true irreplaceable-only dump is a row-level `lode export` (owned
  tables + `source = user` rows), with restore rebuilding the cache via reconciliation + re-embed/
  re-enrich ([stack.md](stack.md#the-partition-is-by-rows-not-by-file)). Deferred — the superset
  copy is correct and free; build the export only if a minimal dump is actually wanted.
- **Cache rebuild cost is non-uniform** ([stack.md](stack.md#the-derived-layer-is-not-uniformly-disposable)).
  Embeddings / lexical / explicit edges rebuild cheaply (local, minutes); AI annotations + inferred
  edges cost real dollars + hours (Claude Batches) to regenerate from scratch. Decide whether to
  *snapshot* the LLM tier of the cache purely to skip recompute on restore — not for correctness,
  only to dodge the cost.
- **LanceDB maturity.** Younger / faster-moving than the rest of the stack; acceptable because the
  cache is disposable and lives behind the repository interface. Watch for breaking changes;
  sqlite-vec is the simpler fallback-down if it churns too hard.
- **Span-annotation fuzzy re-anchor threshold** — tune when span annotations are actually built.
  ([storage.md](storage.md#anchoring-strategy))
- **Local-LLM fallback for `no_egress` notes.** v1 marks sensitive notes/sources `no_egress`: they
  are locally retrievable but excluded from cloud enrichment + Q&A and cited as "withheld from
  synthesis" ([externals.md](externals.md#privacy-consequence-of-aggregation)). A future option is a
  **local generative model** that could enrich and synthesize over withheld notes entirely on-box, so
  they participate in answers without egress. Deferred — it needs a local LLM (quality/latency hit)
  and is a large scope addition; decide if the "withheld" gap proves too limiting in practice.
- **Redact-before-egress pattern set.** What counts as an "obvious secret" stripped before content is
  sent to Claude (keys, tokens, `.env` shapes, PII?) is a rule set that will need iterating; start
  with high-precision patterns to avoid mangling legitimate content, expand from real misses.
- **Substring/span redaction** (upgrade to the [hard delete](externals.md#hard-delete-the-deliberate-immutability-break-corrective-half)).
  v1 purges at version/note granularity; surgical "redact this string everywhere it appears, keep
  the rest of the note" is deferred as YAGNI. Revisit if coarse purge proves too lossy in practice.
- **Faithfulness entailment threshold (ships untuned, must be revisited).** v1 verifies citations
  deterministically (verbatim-span + extractive coupling) **and** runs a local NLI / cross-encoder
  **entailment check** so genuine multi-note synthesis is *answered*, not refused
  ([retrieval.md](retrieval.md#faithfulness-verify-citations-dont-just-require-them)). The stage and a
  default model ship in v1, deliberately **conservative and fail-closed**. The open knob is the
  **model choice + acceptance threshold**: too loose readmits unsupported synthesis (mode 4), too
  tight collapses to extractive-only. It cannot be set honestly without data, so tune it against the
  eval harness once there's a real corpus; treat v1 synthesis answers as capability-present,
  quality-untuned. An LLM-judge second pass remains an optional high-assurance toggle (off by
  default — round-trip + $ + off-box).
- **Chunk size + overlap.** Passages are structure-aware with a token-window fallback
  ([retrieval.md](retrieval.md#chunking-passages-are-the-retrieval-unit)); the fallback threshold
  `N` and the overlap are tuning knobs. Too small fragments context and citations; too large
  re-introduces the recall dilution chunking was meant to fix. Pick a sane default (e.g. ~256–512
  tokens) and tune against the eval harness — passages are regenerable, so re-chunking with new
  parameters is a cheap local rebuild.
- **Eval harness for retrieval + faithfulness — scheduled for build step 1.** A small held-out Q&A
  set (~20–50 questions with known-good citations) scored on retrieval recall@k, citation/faithfulness
  accuracy, and abstention correctness. It is **no longer deferred** — it ships in step 1
  ([design.md](design.md) §7) because three knobs (rerank, the entailment threshold, chunk size) all
  tune against it. Open sub-question: the exact metric weighting and how the golden set is curated.
- **Rerank model + threshold tuning.** The rerank *stage* ships in v1 ([retrieval.md](retrieval.md))
  with a default local cross-encoder behind a toggle; choosing/tuning the model and cutoffs — and
  A/B'ing rerank vs none — waits until there's a real corpus to evaluate against. Don't tune
  pre-data.
- **Landing loop — settled shape, open mechanics.** The landing loop's *architecture* is decided
  ([agents-workflow.md](agents-workflow.md#the-landing-loop--build-review-land-planned)): **all**
  landing goes through a single `/land`; producers (`/code`, solo or fan-out — no `/code-parallel`)
  build, run a **baked-in technical review** (`/code-review` + `simplify --fix`), push the branch to
  origin, and mark it `ready-for-land`; `/land`'s **first task is an autonomous semantic review** (the
  `debate`-twin) that accepts / bounces / escalates, then it batch-merges, re-gates once
  (isolate-on-red), and pushes. A bounced branch becomes a **new ticket carrying the findings, linked
  to the superseded original**. What's left open are the *mechanics*:
  - **`ready-for-land` representation.** beads has fixed built-in statuses plus configurable custom
    statuses (`bd config set status.custom`) **and** labels. Model the state as a **custom status**
    `ready-for-land` (one-per-issue, shows in `bd list --status`) or a **label** (composes with the
    `in_progress`/`blocked` lifecycle and with a `land-failed`/escalated marker). Leaning **label**.
  - **Landing-context schema.** The producer attaches remote branch + head SHA + a one-line summary
    (the lander re-reviews and re-gates, so stored gate-results would be decorative). Open: a JSON
    blob in `--notes`/`--design` parsed via `bd show --json` vs. discrete fields. Keep it parsed, not
    scraped.
  - **Single-lander lock across machines.** Multi-machine dev means two `/land` loops could run at
    once and both merge `trunk`; even on one machine, a `/loop 5m /land` tick can overlap a still-
    running land. Need a "single active lander" guarantee — a beads sentinel claim, a remote lock
    ref, a "skip if already running" guard, or "only run `/land` on one machine." This is the seam
    where the design later hands to real CI. **Treat as a correctness prerequisite of the lander, not
    a nice-to-have.**
  - **Remote-branch naming + GC.** Producers push branches to origin (durable artifact); pick a
    convention (e.g. `land/<ticket-id>` rather than the opaque worktree branch name) and a cleanup
    policy for branches left behind by bounces/escalations, so origin doesn't accumulate cruft.
