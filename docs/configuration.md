# lode — Configuration & tunable knobs

Every parameter the design exposes, in one place. Three kinds, flagged in the **Kind** column:

- **runtime** — a setting the user/operator can change while running; takes effect on next use.
- **tune** — ships with a conservative default but is **meant to be tuned against the eval harness**
  ([design.md](design.md) §7) once there's a real corpus; do not hand-set pre-data.
- **build** — fixed at build time; changing it implies a rebuild/migration, so it's chosen once.

Defaults below are starting points, not measured optima.

## Paths & locations

Everything lode persists lives under **one user-controllable root**, `$LODE_HOME` (default `~/.lode`). One inspectable directory — trivial to surface, back up (`cp -r`), or relocate — rather than scattering data/state/config across separate trees. (This is deliberately *not* the [XDG Base Directory](https://specifications.freedesktop.org/basedir-spec/latest/) split of `$XDG_DATA_HOME` / `$XDG_STATE_HOME` / `$XDG_CONFIG_HOME`; a single root is simpler to reason about and matches the design's "co-locate the lock beside the DB" and "partition by rows, not by file" stance, [storage.md](storage.md#the-partition-is-by-rows-not-by-file).)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| `LODE_HOME` | runtime | `~/.lode` | Root for all on-disk state. Env-var override; one directory holds the DB, vector store, logs, lock, and config. |
| Database path | build | `$LODE_HOME/lode.db` | The SQLite file (irreplaceable rows + rebuildable cache + `jobs`). The single-instance advisory lock lives beside it as `lode.db.lock` ([storage.md](storage.md#single-user-single-instance-linear-chains-no-merge)). |
| Vector store path | build | `$LODE_HOME/lancedb/` | LanceDB passage-vector store (rebuildable cache). A subdir keeps the root readable. |
| Log directory | runtime | `$LODE_HOME/logs/` | Application logs. |
| Config file path | runtime | `$LODE_HOME/config.toml` | User-editable runtime knobs. **Optional** — if absent, every knob uses its default below; no config file is a valid, fully-working state. |

```text
$LODE_HOME/                 # default ~/.lode, overridable by env var
├── lode.db                 # SQLite (irreplaceable rows + rebuildable cache + jobs)
├── lode.db.lock            # single-instance advisory lock (PID) — beside the DB
├── lancedb/                # LanceDB vector store (rebuildable cache)
├── logs/                   # application logs
└── config.toml             # user-editable runtime knobs (optional; absent = defaults)
```

These resolved paths are what the CLI/TUI surfaces to the user (E10/E11).

## Retrieval and ranking

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Retrieval `top-k` (passages fused/fed) | tune | ~20 → trim | How many passages survive fusion into rerank, and how many reach the Q&A context. ([retrieval.md](retrieval.md)) |
| RRF constant `k` | tune | 60 | Reciprocal-Rank-Fusion smoothing constant; standard default rarely needs moving. |
| Rerank stage | runtime | on | Toggle the cross-encoder stage on/off (the *seam* is permanent; the stage is switchable). |
| Rerank model | tune | `BAAI/bge-reranker-base` | Local cross-encoder via `fastembed` (`TextCrossEncoder`), ONNX. Swappable; A/B once there's a corpus. `fastembed` does **not** ship `bge-reranker-v2-m3`, so `bge-reranker-base` is the loadable bge-family pick (verified — see [Models](#models)). |
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
| Entailment model | tune | `BAAI/bge-reranker-base` | Cross-encoder reranker **repurposed** as the entailment scorer — `fastembed` ships no dedicated NLI model, so the gate sigmoid's the cross-encoder logit. Same ONNX runtime as rerank. ([retrieval.md](retrieval.md#faithfulness-verify-citations-dont-just-require-them)) |
| Entailment loader | build | `fastembed-cross-encoder` | How the NLI model is loaded: `fastembed`'s `TextCrossEncoder` on the bundled ONNX runtime, in-process — **no** separate `optimum`/`onnxruntime` loader needed (verified — see [Models](#models)). |
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
| Embedding model | build | `nomic-ai/nomic-embed-text-v1.5` | Local ONNX via `fastembed`. A change re-keys the vector space → full re-embed + re-index. ([stack.md](stack.md)) |
| Embedding vector dimension | build | `768` | Output dimension of the embedding model. **LanceDB table creation needs this fixed**; it must match the model (`nomic-embed-text-v1.5` → 768). Re-keying it = full re-embed. |
| Enrichment LLM | runtime | Claude Haiku 4.5 | High-volume background extraction. |
| Q&A LLM | runtime | Claude Sonnet 4.6 | Default interactive synthesis model. |
| Q&A "think harder" | runtime | Opus 4.8 (toggle) | Higher-quality, higher-cost synthesis on demand. |

The **local** models — embedder, [reranker](#retrieval-and-ranking), [faithfulness NLI](#faithfulness-gate) — all run **in-process on the ONNX runtime via `fastembed`** (no model server/daemon, **not Ollama**). The **only** remote models are the enrichment + Q&A LLMs (Claude). See [stack.md](stack.md).

These local ids/dim were pinned and **verified to load** on the `fastembed` ONNX runtime in `lode-txh.6` (`fastembed 0.8.0`); the spike's standing proof is `tests/test_models_smoke.py` (opt-in, `LODE_SMOKE_MODELS=1`, since loading downloads the models). Two spike findings shaped the pins: (1) `fastembed` does **not** ship the originally-assumed `bge-reranker-v2-m3`, so the reranker is `BAAI/bge-reranker-base` (the loadable bge-family cross-encoder); (2) `fastembed` ships **no dedicated NLI model**, so the NLI/entailment leg repurposes that same cross-encoder via `TextCrossEncoder` — confirming the docs' "bge-reranker repurposed" option and removing the need for a separate `optimum`/`onnxruntime` loader. The model + threshold remain [open tuning knobs](decisions.md), revisited against the eval harness.

## Build constants (chosen once)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Content-address hash `H` | build | non-crypto 128-bit (xxh3-128) | Single-user/no-sync needs only low accidental-collision probability, not crypto resistance; length-prefixed framing. Changing `H` re-keys every node. blake2b-128 (stdlib) is the no-dep fallback. ([storage.md](storage.md#identity-vs-version)) |
| Single-instance advisory lock | build | on | Lockfile/PID beside the DB; required so async workers have a single owner. ([storage.md](storage.md#single-user-single-instance-linear-chains-no-merge)) |
