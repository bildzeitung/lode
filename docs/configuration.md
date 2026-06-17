# lode — Configuration & tunable knobs

Every parameter the design exposes, in one place. Three kinds, flagged in the **Kind** column:

- **runtime** — a setting the user/operator can change while running; takes effect on next use.
- **tune** — ships with a conservative default but is **meant to be tuned against the eval harness**
  ([design.md](design.md) §7) once there's a real corpus; do not hand-set pre-data.
- **build** — fixed at build time; changing it implies a rebuild/migration, so it's chosen once.

Defaults below are starting points, not measured optima.

## Retrieval & ranking

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Retrieval `top-k` (passages fused/fed) | tune | ~20 → trim | How many passages survive fusion into rerank, and how many reach the Q&A context. ([retrieval.md](retrieval.md)) |
| RRF constant `k` | tune | 60 | Reciprocal-Rank-Fusion smoothing constant; standard default rarely needs moving. |
| Rerank stage | runtime | on | Toggle the cross-encoder stage on/off (the *seam* is permanent; the stage is switchable). |
| Rerank model | tune | `bge-reranker-v2-m3` | Local cross-encoder, ONNX. Swappable; A/B once there's a corpus. |
| Rerank keep-N / score cutoff | tune | top-N | How many reranked hits proceed to graph expansion. |

## Chunking (passages)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Chunk fallback threshold `N` (tokens) | tune | ~256–512 | Structure-aware split sub-splits any block over `N`. Too small fragments context/citations; too large re-introduces recall dilution. ([retrieval.md](retrieval.md#chunking-passages-are-the-retrieval-unit)) |
| Chunk overlap | tune | small | Overlap between fallback sub-chunks at block boundaries. |

Passages are regenerable, so re-chunking with new values is a cheap local rebuild.

## Faithfulness gate

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Entailment model | tune | local NLI / cross-encoder | Same ONNX runtime as rerank. ([retrieval.md](retrieval.md#faithfulness-verify-citations-dont-just-require-them)) |
| Entailment acceptance threshold | tune | **conservative** | The one residual-risk knob for synthesis: too loose readmits unsupported synthesis, too tight collapses to extractive-only. Ships fail-closed, untuned. |
| LLM-judge second pass | runtime | off | Optional "high-assurance" verification; costs a round-trip + $ + off-box egress. |

## Async work queue

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Reconciliation scan interval | runtime | periodic | How often the self-healing scan re-enqueues missing derived work. ([storage.md](storage.md#the-async-work-queue)) |
| Retry backoff + max attempts | runtime | exp backoff, capped | Transient-failure retry before dead-lettering a job. |
| Enrichment batch flush policy | runtime | size/time | When accumulated `enrich` jobs are submitted as a Claude Batch. |

## Externals (with connectors)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Refresh policy / TTL (per source) | runtime | per-source | On-access revalidation vs scheduled; a closed ticket rarely changes, an active PR hourly. ([decisions.md](decisions.md)) |
| Re-enrichment materiality threshold | tune | size/similarity delta | Gates the paid re-enrichment of a changed external snapshot; below it, carry prior enrichment forward. Caps cloud spend on chatty sources. ([externals.md](externals.md#snapshot-churn-decouple-new-snapshot-from-re-enrich)) |
| Draw-down hop limit | build | 1 | Follow explicit links one hop, then stop. ([externals.md](externals.md#draw-down-rules)) |

## Privacy & egress

| Knob | Kind | Default | Notes |
|---|---|---|---|
| `no_egress` (per note / source) | runtime | off | Indexed locally, never sent to Claude (no enrichment, excluded from cloud Q&A; cited as "withheld"). ([externals.md](externals.md#privacy-consequence-of-aggregation)) |
| Redact-before-egress pattern set | runtime | high-precision seed | Secret patterns stripped before content is sent to Claude; iterate from real misses. ([decisions.md](decisions.md)) |
| Redact-before-index pattern set | runtime | high-precision seed | Secret patterns kept out of the local vector/FTS index. |

## Models

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Embedding model | build | `nomic-embed-text-v1.5` / `bge-*` | Local ONNX. A change re-keys the vector space → full re-embed + re-index. ([stack.md](stack.md)) |
| Enrichment LLM | runtime | Claude Haiku 4.5 | High-volume background extraction. |
| Q&A LLM | runtime | Claude Sonnet 4.6 | Default interactive synthesis model. |
| Q&A "think harder" | runtime | Opus 4.8 (toggle) | Higher-quality, higher-cost synthesis on demand. |

## Build constants (chosen once)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Content-address hash `H` | build | non-crypto 128-bit (xxh3-128) | Single-user/no-sync needs only low accidental-collision probability, not crypto resistance; length-prefixed framing. Changing `H` re-keys every node. blake2b-128 (stdlib) is the no-dep fallback. ([storage.md](storage.md#identity-vs-version)) |
| Single-instance advisory lock | build | on | Lockfile/PID beside the DB; required so async workers have a single owner. ([storage.md](storage.md#single-user-single-instance-linear-chains-no-merge)) |
