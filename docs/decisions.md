# lode — Open decisions (deferred, not forgotten)

*(§9)* Decisions deliberately left open, with the current leaning where there is one. Revisit each
when the build reaches the feature that forces it.

- **External refresh: on-access revalidation vs. scheduled background refresh.** Leaning
  **on-access with a short TTL cache** for a single instance with finite API quota — but it's
  really a per-source judgment (a closed ticket changes rarely; an active PR changes hourly).
  Decide per connector when building it. ([externals.md](externals.md#the-broken-assumption-external-staleness-is-not-topological))
- **History compaction / squash policy.** Not needed for years; revisit if storage matters.
  ([storage.md](storage.md#identity-vs-version))
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
- **Eval harness for retrieval + faithfulness.** Several "tune post-data" decisions (rerank, the
  entailment threshold above) presuppose a way to *measure* quality. There is no measurement plan
  yet — a small held-out Q&A set with known-good citations, scored on retrieval recall and citation
  accuracy. Without it, "A/B once there's a corpus" has nothing to A/B against. (Cross-references
  finding #9; promote to a build-step-1 deliverable when we reach it.)
- **Rerank model + threshold tuning.** The rerank *stage* ships in v1 ([retrieval.md](retrieval.md))
  with a default local cross-encoder behind a toggle; choosing/tuning the model and cutoffs — and
  A/B'ing rerank vs none — waits until there's a real corpus to evaluate against. Don't tune
  pre-data.
