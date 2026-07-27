# lode — Configuration & tunable knobs

Every parameter the design exposes, in one place. Three kinds, flagged in the **Kind** column:

- **runtime** — a setting the user/operator can change while running; takes effect on next use.
- **tune** — ships with a conservative default but is **meant to be tuned against the eval harness**
  ([design.md](design.md) §7) once there's a real corpus; do not hand-set pre-data.
- **build** — fixed at build time; changing it implies a rebuild/migration, so it's chosen once.

Defaults below are starting points, not measured optima.

**Scope note:** this page is the lode *application's* knobs — what `lode` itself reads at runtime.
The `/code` skill's agent-orchestration concurrency cap (how many builder/reviewer agents Claude Code
runs at once) is dev-tooling, not application config, so it's documented instead in
[agents-workflow.md](agents-workflow.md#concurrency-cap-lode-2cf) alongside the rest of the coding
loop's mechanics.

## Paths & locations

Everything lode persists lives under **one user-controllable root**, `$LODE_HOME` (default `~/.lode`). One inspectable directory — trivial to surface, back up (`cp -r`), or relocate — rather than scattering data/state/config across separate trees. (This is deliberately *not* the [XDG Base Directory](https://specifications.freedesktop.org/basedir-spec/latest/) split of `$XDG_DATA_HOME` / `$XDG_STATE_HOME` / `$XDG_CONFIG_HOME`; a single root is simpler to reason about and matches the design's "co-locate the lock beside the DB" and "partition by rows, not by file" stance, [stack.md](stack.md#the-partition-is-by-rows-not-by-file).)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| `LODE_HOME` | runtime | `~/.lode` | Root for all on-disk state. Env-var override; one directory holds the DB, vector store, logs, lock, and config. |
| Database path | build | `$LODE_HOME/lode.db` | The SQLite file (irreplaceable rows + rebuildable cache + `jobs`). The single-instance advisory lock lives beside it as `lode.db.lock` ([storage.md](storage.md#single-user-single-instance-linear-chains-no-merge)). |
| Vector store path | build | `$LODE_HOME/lancedb/` | LanceDB passage-vector store (rebuildable cache). A subdir keeps the root readable. |
| Model cache directory | build | `$LODE_HOME/models/` | Local ONNX weights cache for every `fastembed`-loaded model (embedder, reranker, NLI/entailment cross-encoder — see [Models](#models)), passed as `cache_dir` to each loader. Without it, `fastembed` defaults to `tempfile.gettempdir()/fastembed_cache`, which WSL wipes on reboot (and `systemd-tmpfiles` clears on many distros) — silently re-downloading ~500MB from HuggingFace on a semi-regular basis instead of paying that cost once (`lode-gmo`). `fastembed` creates the directory itself on first load. |
| Log directory | runtime | `$LODE_HOME/logs/` | Application logs. |
| `LODE_LOG_LEVEL` | runtime | `INFO` | lode's own root-logger level. Accepts a case-insensitive level name (`debug`, `info`, `warning`, ...); an unrecognized value raises rather than silently defaulting. Read when no level is passed explicitly (`src/lode/logconfig.py::resolve_level`). |
| `ANTHROPIC_LOG` | runtime | unset | Not a lode-specific knob — the Anthropic SDK's own wire-level debug switch. Set to `debug` or `info` and the SDK logs on the `anthropic` logger, which propagates to the root logger and is formatted/routed alongside lode's own logs (`src/lode/logconfig.py`). |
| `lode --debug` | runtime | off | Top-level CLI flag (`lode --debug <subcommand>`, e.g. `lode --debug tui`): forces the root-logger level to `DEBUG` for that invocation, which also flips on every DEBUG-gated diagnostic (e.g. the TUI's event-loop-lag `latency_probe`). Takes precedence over `LODE_LOG_LEVEL` when passed; omit it and `LODE_LOG_LEVEL` (default `INFO`) still applies unchanged. In the TUI this only raises verbosity in the log file — the console stays suppressed either way (`lode-1i8.2`); for plain CLI commands it raises both stderr and file verbosity (`src/lode/cli.py::main`). |
| Config file path | runtime | `$LODE_HOME/config.toml` | User-editable runtime knobs. **Optional** — if absent, every knob uses its default below; no config file is a valid, fully-working state. |

```text
$LODE_HOME/                 # default ~/.lode, overridable by env var
├── lode.db                 # SQLite (irreplaceable rows + rebuildable cache + jobs)
├── lode.db.lock            # single-instance advisory lock (PID) — beside the DB
├── lancedb/                # LanceDB vector store (rebuildable cache)
├── models/                 # fastembed ONNX weights cache (embedder/reranker/NLI)
├── logs/                   # application logs
└── config.toml             # user-editable runtime knobs (optional; absent = defaults)
```

These resolved paths are what the CLI/TUI surfaces to the user (E10/E11).

**`config.toml` format and load order (lode-40g).** A flat TOML table whose keys
are `Settings` field names from `src/lode/config.py` — one per tunable knob in
the tables below (e.g. `refresh_ttl_s = 1800`) — with no `[section]` headers,
since `Settings` itself is flat. `lode.config.load_settings()` is the one
function that resolves this layering: field defaults, then `config.toml` if
present, then any explicit override the caller passes (a test fixture; there
is no per-knob CLI flag or env var today — only `LODE_HOME`/`LODE_LOG_LEVEL`
above are env-var knobs).

An override whose value is `None` is treated as **not supplied** and dropped
before the merge, so it cannot clobber a `config.toml` value (lode-n8n). This
is the contract the first per-knob CLI flag will rely on — a Typer option
defaulting to `None` can be passed straight through, and leaving the flag off
will not silently revert the user's configured value to the default. It holds
only because no knob's meaningful value is `None` today (no `Settings` field is
optional); a knob that legitimately needs `None` must revisit it. See
`load_settings()`'s docstring for the full rationale.

The file is **validated on load**, so a bad one fails immediately rather than
silently running at defaults: a TOML syntax error raises `TOMLDecodeError`, and
an unrecognized key or an out-of-range value raises `pydantic`'s
`ValidationError` (`extra="forbid"` plus each field's validators) — exactly as
an invalid keyword override would. Because the file is hand-edited, the CLI
catches both at its boundary (`src/lode/cli.py::_resolve_settings`) and reports
a typo as a one-line `invalid config file <path>: …` on stderr with exit 1,
rather than a Python traceback; library callers of `load_settings()` still get
the raised exception.

Each CLI/TUI entry point that needs settings resolves them **once** through that
helper and threads the result down, rather than re-resolving per call site:
`ask`, `work`, `recover`, `tui` (which hands the resolved `Settings` to
`LodeApp`, where every screen reads it back off `self.app.settings`), and `add`
(into *both* `Repository.save` and `_enrich_immediately`). Threading into the
`Repository` legs is load-bearing, not cosmetic: `save`/`recover` run
`redact_before_index()` off the settings they are handed, so an entry point that
passes none would silently index a secret matched only by the user's *own*
`redact_before_index_patterns` (lode-40g).

A bare `Settings()` elsewhere in the codebase is a library-internal default for
an *optional* caller-supplied override (`settings = settings or Settings()`),
not a second config-loading path.

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

## TUI — passive connection surfacing (E11)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Related-notes enabled | runtime | on | Master on/off switch for the passive related-notes pass. Off skips the pass entirely — no FTS5/embedder/LanceDB work runs on the input path. **This is a user preference, not a lag fix**: a lag-diagnosis spike confirmed the pass already runs off the UI thread (fastembed/ONNX releases the GIL), so turning it off does not change keystroke latency. |
| Related-notes debounce | runtime | `500ms` | Idle-typing delay in the capture screen before a passive "you wrote about this" pass runs ([design.md](design.md) §2 "Surfacing connections"); restarted on every keystroke so a burst of typing triggers at most one pass per pause. |
| Related-notes result count | runtime | `5` | Max related past notes shown per pass. |
| Related-notes minimum draft length | runtime | `20` chars | Below this (stripped) length, no pass runs at all — no DB connection opened. |

Runs the same read pipeline as `lode ask` ([retrieval.md](retrieval.md)) minus the cross-encoder rerank stage (skipped for latency; the seam stays wired for Q&A), off the UI thread so it never blocks capture.

## Async work queue

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Reconciliation scan interval | runtime | periodic | How often the self-healing scan re-enqueues missing derived work. ([storage.md](storage.md#the-async-work-queue)) |
| Retry backoff + max attempts | runtime | exp backoff, capped | Transient-failure retry before dead-lettering a job. |
| Stale-running reclaim timeout | runtime | `900s` (15 min) | A job stuck in `status='running'` this long (no claim update, e.g. a worker crash) is reclaimed — same retry/dead-letter accounting as a handler failure. Excludes batch-backed enrich jobs. ([storage.md](storage.md#crash-reclaim-a-job-stuck-in-running--pinned-lode-aor)) |
| Enrichment batch flush policy | runtime | size/time | When accumulated `enrich` jobs are submitted through the active provider's batch path (Anthropic's Batches API by default, or serialized sequential calls under a provider with no batch API — [LLM provider seam](stack.md#llm-provider-seam-decided-lode-568v1)). |
| `work --wait` timeout | runtime | `1800s` (30 min) | Max time `lode work --wait` blocks polling for the queue to fully drain (incl. collected Batches API enrich results) before exiting non-zero and naming the still-pending/running jobs. The Batches API SLA is up to 24h, so `--wait` can legitimately time out on a large enrich load -- that's expected, not a bug; it suits embed-heavy or small-batch cases, and a big async enrich backlog may need a plain re-run of `lode work` instead. |
| Progress heartbeat interval (`progress_heartbeat_interval_s`) | runtime | `15s` | How often `lode work` logs a "still running" heartbeat line (`lode.progress.op_progress`) for a named long-running op -- a `reconcile()` step, a `drain()` batch pre-step, or the main claim/run loop -- that hasn't finished yet (`lode-olmi.15`). Makes a stuck op visible instead of silent, even where it can't be safely aborted outright (e.g. a local ONNX model load or a SQL scan). |
| LLM call timeout (`llm_call_timeout_s`) | runtime | `120s` | Per-call client-side timeout passed to EVERY cloud-LLM call through the `LLMProvider` seam (`lode-568v.2`/`.3`), immediate and batch alike, under whichever provider is active: the enrichment calls reachable from `lode work` (`enrich.py` -- the batch-path pre-steps and the immediate structured-output call a residual enrich job can take in `drain()`'s main loop) and the Q&A synthesis call (`qa.py`) -- bounds a hung network call rather than letting it block forever (`lode-olmi.15`). Renamed vendor-neutral from `anthropic_call_timeout_s` (`lode-568v.1`/`.2`); a `config.toml` still carrying the old key is remapped by `load_settings()`. Distinct from Fetch timeout below, which governs web draw-down HTTP fetches, not LLM provider calls. |

## Externals (with connectors)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Refresh TTL (`refresh_ttl_s`) | runtime | `3600` (1h) | How long a web external's head snapshot may go un-revalidated before `lode.reconcile`'s `refresh_stale` step re-enqueues a `refresh` job for it (`lode-w0h.6`). Decided **scheduled TTL sweep**, not true on-access revalidation — see [externals.md](externals.md#refresh-policy-ttl-based-revalidation-decided-for-web-lode-w0h6). A single default today (no per-source override); a closed ticket rarely changing vs. an active PR changing hourly is exactly the kind of per-source judgment a future connector may want its own TTL for. |
| Re-enrichment materiality threshold | tune | embedding-similarity delta (1 - cosine) | Gates the paid re-enrichment of a changed external snapshot; below it, carry prior enrichment forward. Caps cloud spend on chatty sources. ([externals.md](externals.md#snapshot-churn-decouple-new-snapshot-from-re-enrich)) |
| Draw-down hop limit | build | 1 | Follow explicit links one hop, then stop. ([externals.md](externals.md#draw-down-rules)) |
| Fetch timeout | runtime | `10s` | Per-fetch HTTP timeout (`lode-w0h.1`); a timeout is a TRANSIENT failure (retried by the async queue), not a tombstone — as is a server-reported `408 Request Timeout`. |
| Fetch max redirects | runtime | 5 | 3xx redirects a single web-fetch follows before tombstoning as unresolvable (`lode-w0h.1`). **Distinct from Draw-down hop limit above** — this caps redirects *within one fetch*; the hop limit caps crawling a fetched page's *own outbound links*. |
| Fetch min-extract-chars floor | tune | 200 | Readability-extracted text shorter than this is treated as a JS-scaffold/paywall/empty page and tombstoned, even when the extractor returned non-`None` text — the length-floor half of the fetch-outcome signal ([externals.md](externals.md#draw-down-rules)). |
| URL tracking-param blocklist | runtime | `utm_*`, `fbclid`, `gclid` | Query params stripped during URL canonicalization (`lode-w0h.3`) before the `external_id` dedup key is computed. A trailing `*` matches a prefix (case-insensitive); everything else matches exactly. This same canonical form is the `lode-w0h.6` refresh policy's join key for "the same source" across refetches — and, since `lode-0as`, also strips userinfo (`user[:pass]@`) so credentials in a pasted URL never enter it. ([externals.md](externals.md#url-canonicalization-decided-lode-w0h3-userinfo-stripped-lode-0as)) |

### Atlassian connectors (JIRA + Confluence Cloud, `lode-gpzn`)

Cloud-only, Basic auth (account email + API token); JIRA REST v3 and Confluence Cloud REST. Data Center / Server is out of scope (deferred). Each product is feature-flagged independently and **defaults off** — a JIRA/Confluence link falls through to the generic web connector until its flag is on **and** credentials resolve (`src/lode/config.py::jira_active` / `confluence_active`).

| Knob | Kind | Default | Notes |
|---|---|---|---|
| `jira_enabled` | runtime | `false` | Feature flag for the JIRA Cloud API connector. |
| `confluence_enabled` | runtime | `false` | Feature flag for the Confluence Cloud API connector. |
| `jira_base_url` | runtime | `""` (empty) | API base override, e.g. `https://acme.atlassian.net`. Empty means infer from the pasted link at detection time. A non-empty value must be a well-formed `http(s)` URL — a malformed one fails validation at `Settings()` construction. |
| `confluence_base_url` | runtime | `""` (empty) | Same shape as `jira_base_url`, for Confluence. |
| `LODE_JIRA_TOKEN` env var / `jira_token` (config.toml fallback) | runtime | unset / `""` | JIRA Cloud API token. Resolved **env-var PRIMARY**: `LODE_JIRA_TOKEN` is checked first, then the `jira_token` key in `config.toml` as fallback. No secret is required to live in `config.toml`. **The raw value is never logged, echoed, or shown by `lode config`** — `secret=True` (`src/lode/config.py::_knob`) shows only a presence indicator in the knob table, never the value (see below). |
| `LODE_JIRA_EMAIL` env var / `jira_email` (config.toml fallback) | runtime | unset / `""` | JIRA Cloud Basic-auth account email — same env-first, config.toml-fallback resolution as the token, and the same `secret=True` presence-only treatment (lode-dx4r). |
| `LODE_CONFLUENCE_TOKEN` env var / `confluence_token` (config.toml fallback) | runtime | unset / `""` | Confluence Cloud API token — same resolution and secrecy guarantee as `jira_token`. |
| `LODE_CONFLUENCE_EMAIL` env var / `confluence_email` (config.toml fallback) | runtime | unset / `""` | Confluence Cloud Basic-auth account email — same resolution and secrecy guarantee as `jira_email`. |

A missing token or email — from either source — resolves to a clean **"connector inactive"** state (`resolve_jira_credentials`/`resolve_confluence_credentials` return `None`), never an exception; the link falls through to the generic web fetcher. This is a deliberately different shape from the LLM provider credential chain ([below](#models), `src/lode/auth.py`/`src/lode/llm_provider.py`, [decisions.md](decisions.md)): that chain never reads `config.toml` at all and raises `AuthError` (Anthropic) or `LLMAuthError` (OpenAI/Azure) on "nothing resolved" (there is no "connector inactive" fallback path for the LLM calls lode's own core loop depends on), whereas an Atlassian connector is opt-in per product and must degrade quietly when unconfigured.

`lode verify --jira` / `lode verify --confluence` (`lode-04lz`) is a read-only preflight that confirms these knobs resolved the way you intended — which flag/credential/base-URL source is in effect, and whether the resolved credentials actually reach the tenant — without writing anything; see [externals.md](externals.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn) for the full manual smoke-test procedure it's the fast first step of. No new knob is introduced by it.

### Atlassian credentials show a presence indicator in `lode config` / the TUI knob table (lode-dx4r)

All four credential fields — `jira_email`, `jira_token`, `confluence_email`, `confluence_token` — are `secret=True` (`src/lode/config.py::_knob`). A `secret=True` field is **not** excluded from `knob_rows()`'s output; it still gets a row, but the row's value is always one of two fixed placeholders, never the raw setting:

- **`[REDACTED]`** — the credential resolves from *either* source, the env var or the `config.toml` fallback.
- **`[unset]`** — neither source resolves.

Presence is computed from the env var / the resolver's inputs, not read back off the raw `Settings` field value — the field value alone can't tell the difference between "resolved via `config.toml`" and "resolved via env var, so this field is empty" (env vars never flow into `Settings`). This is enforced structurally in `knob_rows()`, not by each renderer (`lode config` CLI, the TUI's Ctrl+O diagnostics screen) remembering to redact two field names — both call the one shared builder, so the fix lands once. Before lode-dx4r, `jira_token`/`confluence_token` were excluded from the table outright (no row at all, even when set), while `jira_email`/`confluence_email` were plain knobs that echoed a `config.toml` value verbatim — both gaps are closed by the presence-indicator contract above.

## Privacy & egress

| Knob | Kind | Default | Notes |
|---|---|---|---|
| `no_egress` (per note / source) | runtime | off | Indexed locally, never sent to the configured cloud LLM (no enrichment, excluded from cloud Q&A; cited as "withheld"). ([externals.md](externals.md#privacy-consequence-of-aggregation)) |
| Redact-before-egress pattern set | runtime | high-precision seed | Secret patterns stripped before content is sent to the configured cloud LLM; iterate from real misses. ([decisions.md](decisions.md)) |
| Redact-before-index pattern set | runtime | high-precision seed | Secret patterns kept out of the local vector/FTS index. |

## Models

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Embedding model | build | `nomic-ai/nomic-embed-text-v1.5` | Local ONNX via `fastembed`. A change re-keys the vector space → full re-embed + re-index. ([stack.md](stack.md)) |
| Embedding vector dimension | build | `768` | Output dimension of the embedding model. **LanceDB table creation needs this fixed**; it must match the model (`nomic-embed-text-v1.5` → 768). Re-keying it = full re-embed. |
| LLM provider (`llm_provider`) | runtime | `anthropic` | Which `LLMProvider` implementation every cloud-LLM call site resolves against (`lode-568v.2`/`.3`) — whole-app, not per-surface: setting this sets it for enrichment AND Q&A together. `"anthropic"` \| `"openai"` — `"openai"` routes to direct OpenAI or Azure OpenAI depending on `azure_openai_endpoint`; Azure-vs-direct-OpenAI is a routing detail under this one value, not a second provider value. ([stack.md](stack.md#llm-provider-seam-decided-lode-568v1)) |
| Azure OpenAI endpoint (`azure_openai_endpoint`) | runtime | `""` | The resource **root**, e.g. `https://{resource}.openai.azure.com` (`lode-568v.3`) — do **not** append `/openai`; the openai SDK's `AzureOpenAI` client adds that segment itself, so `.../openai` doubles the path and every request 404s. Empty means direct OpenAI (or a non-`"openai"` `llm_provider`). Requires `azure_openai_api_version` to also be set (validated at `Settings` construction). |
| Azure OpenAI api-version (`azure_openai_api_version`) | runtime | `""` | e.g. `2025-04-01-preview` — sent as a query param on every request, not a header (`lode-568v.3`). Required when `azure_openai_endpoint` is set. |
| LLM provider credentials | runtime | env/SDK-only | **No key ever lives in `config.toml`** — unlike the Jira/Confluence credentials above, there is no config-file fallback for these. `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (unchanged) resolve via the Anthropic SDK's own credential chain under the default `llm_provider`; `OPENAI_API_KEY` (direct OpenAI) or `AZURE_OPENAI_API_KEY` (when `azure_openai_endpoint` is set) resolve under `llm_provider = "openai"`. A missing credential raises `AuthError` (Anthropic) or `LLMAuthError` (OpenAI/Azure) with a provider-appropriate message naming the exact env var to set — never a traceback ([stack.md](stack.md#llm-provider-seam-decided-lode-568v1)). |
| Enrichment LLM (`enrichment_llm`) | runtime | Claude Haiku 4.5 (default provider) | High-volume background extraction. A `(model, reasoning_effort)` `ModelTier` pair (`lode-568v.2`) — a bare TOML string still coerces to a `ModelTier` with `reasoning_effort=None`. `model` is interpreted **against the active `llm_provider`**: an Anthropic model id under the default provider, or an Azure/OpenAI deployment name under `llm_provider = "openai"`. Persists into the DB (`annotations`/`edges`) — DB-affecting; the model (and, once non-Anthropic, the provider) is recorded per-row on `annotations.model`/`annotations.provider` and drift is detected from it, never pinned. ([below](#model-provenance-the-enrichment-llm-decided-lode-g2745)) |
| Q&A LLM (`qa_llm`) | runtime | Claude Sonnet 4.6 (default provider) | Default interactive synthesis model. A `ModelTier` pair, same shape as Enrichment LLM — `model` is likewise interpreted against the active `llm_provider` (an Azure/OpenAI deployment name under `llm_provider = "openai"`). Answer-time only, persists nothing — recorded default, no provenance machinery. |
| Q&A "think harder" (`qa_think_harder_llm`) | runtime | Opus 5 (toggle, default provider) | Higher-quality, higher-cost synthesis on demand. A `ModelTier` pair, same provider-relative interpretation as the two knobs above — "think harder" can be a deployment swap (today's Anthropic Sonnet→Opus default), a `reasoning_effort` bump on the same deployment, or both. Answer-time only, persists nothing — recorded default, no provenance machinery. |

The **local** models — embedder, [reranker](#retrieval-and-ranking), [faithfulness NLI](#faithfulness-gate) — all run **in-process on the ONNX runtime via `fastembed`** (no model server/daemon, **not Ollama**). The **only** remote models are the enrichment + Q&A LLMs — Anthropic by default, or an OpenAI/Azure deployment under `llm_provider = "openai"` ([LLM provider seam](stack.md#llm-provider-seam-decided-lode-568v1)). See [stack.md](stack.md).

All three load through `fastembed`'s model-management path and cache their weights at the [model cache directory](#paths--locations), `$LODE_HOME/models/` — never `fastembed`'s own `tempfile.gettempdir()` default (`lode-gmo`).

These local ids/dim were pinned and **verified to load** on the `fastembed` ONNX runtime in `lode-txh.6` (`fastembed 0.8.0`); the spike's standing proof is `tests/test_models_smoke.py` (opt-in, `LODE_SMOKE_MODELS=1`, since loading downloads the models). Two spike findings shaped the pins: (1) `fastembed` does **not** ship the originally-assumed `bge-reranker-v2-m3`, so the reranker is `BAAI/bge-reranker-base` (the loadable bge-family cross-encoder); (2) `fastembed` ships **no dedicated NLI model**, so the NLI/entailment leg repurposes that same cross-encoder via `TextCrossEncoder` — confirming the docs' "bge-reranker repurposed" option and removing the need for a separate `optimum`/`onnxruntime` loader. The model + threshold remain [open tuning knobs](decisions.md), revisited against the eval harness.

**First run needs the network — make it explicit with `lode models pull` (`lode-og3`, rebuilding the bounced `lode-6qh`).** Inference itself is fully local (ONNX/CPU, no text leaves the box) — but on a cold cache, the *first* call to the embedder or the reranker/NLI cross-encoder downloads ~500MB of ONNX weights from HuggingFace, right in the middle of whatever you were doing (a `lode work` or `lode ask` run). `lode models pull` forces that download deliberately, up front, warming the models named by your **resolved** settings (`$LODE_HOME/config.toml` honored — the pinned defaults above only if you haven't overridden them):

```bash
lode models pull
```

It warms all three local models (the embedder, and the reranker/NLI cross-encoder — one download when, as by default, `rerank_model` and `entailment_model` are the same pinned id) into the same durable [model cache directory](#paths--locations) production reads from, and prints where the weights landed. Run it once after install (or after wiping `$LODE_HOME/models/`); every `lode work` / `lode ask` after that is offline for indexing/retrieval.

**Air-gapped run against an already-warm cache:** set `HF_HUB_OFFLINE=1` to force `fastembed`'s own `local_files_only` path (`fastembed/common/model_management.py`), so a load never attempts a network call even to check for updates — a cold cache under this flag fails loudly instead of silently trying to phone home. This is `fastembed`'s env var, not a lode-specific knob; a first-class offline/air-gapped *mode* is out of scope here (`lode-6qh`) — this is just the escape hatch once the cache is already warm.

**Failure surfaces as a clear message, not a traceback (`lode-96t`).** `lode models pull`'s whole job is to make the first-run network dependency explicit, so its own most likely failure paths — no network reachable, `HF_HUB_OFFLINE=1` set against a cold cache, or HuggingFace rate-limiting/erroring — each exit non-zero with a distinct, actionable message instead of a raw `fastembed`/`huggingface_hub` stack trace. This is deliberately **not** a blanket `except Exception`, which would just as readily mask a real defect as a network hiccup; it catches only the specific exception types those two libraries are verified to raise for these cases (`src/lode/cli.py`'s `_warm`), and lets anything else propagate. A bad `config.toml` gives the same clean stderr message + exit 1 every other command gives, not a traceback.

### Model provenance: download-control and mismatch-behavior (decided, lode-crh8.1)

`lode-crh8` (model provenance: pin, verify, and regenerate what lode embeds and answers with) named two orthogonal open axes for the **embedder** — the only local model whose output persists into the DB, per the epic's own DB-invalidation scoping — and both are now decided; the full write-up, with the schema shape and the reasoning, lives in [storage.md](storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81):

- **Download-control: DETECT, not PIN.** lode does not pre-materialize the embedder's weights at a pinned HuggingFace revision (achievable via `huggingface_hub.snapshot_download(revision=...)` + `fastembed`'s `specific_model_path`, but at the cost of lode owning the whole download path — bootstrap, offline fallback, partial-download recovery). Instead it takes the cheaper **read-only probe** — the same `huggingface_hub.model_info(repo).sha` lookup `fastembed`'s own loader already performs — and records what it resolves to. **Deferred, not rejected:** see the open-decision entry in [decisions.md](decisions.md) for the PIN revisit trigger.
- **Mismatch-behavior: WARN, never REFUSE, recorded PER-VECTOR.** A live-cache/manifest disagreement surfaces as a `lode status` warning (never a hard block on embed/enrich — REFUSE was rejected because it can brick normal operation on an innocent cache eviction) and is corrected by a deliberate regeneration run (`lode-g274.7`), never automatically. "Per-vector" turned out to be a one-field addition (`model_revision` alongside the `model` field the `embeddings` LanceDB table already carries per passage), not a new schema.
- **No separate manifest artifact.** "The manifest" is the aggregate of that per-vector data (`DISTINCT (model, model_revision)` across live `embeddings` rows), not a new committed file or table — see storage.md for why that's correct under a DETECT (not PIN) design, where the resolved revision is a per-installation fact, not something the source tree can assert once for every user. The **friendly model id** stays the only git-tracked build constant here (the `Embedding model` row above), unchanged by this decision.

This unblocks `lode-g274.4` (embedder manifest + `lode status` check) and `lode-g274.7` (re-embed/regenerate capability) to be scoped without further design questions, per `lode-crh8.1`'s acceptance criteria.

### Model provenance: the enrichment LLM (decided, lode-g274.5)

`lode-crh8`'s DB-invalidation scoping also covers the **enrichment LLM** (`enrichment_llm`, [Models](#models) above) — its output (tags, entities, summaries, inferred edges) persists into `annotations`/`edges` the same way the embedder's output persists into `embeddings`, so a silent model change is the same class of problem for enrichment as for embedding. Unlike the embedder, there is no PIN-vs-DETECT axis to decide: this model generation's Anthropic IDs (`claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-5`) have **no dated-snapshot form** — the bare/marketing ID *is* the complete stable identifier, and appending a date suffix 404s. There is nothing more specific to pin against, so the mechanism collapses to record + detect:

- **Recorded per-row, already.** `annotations.model` (alongside `prompt_ver`) is populated from `settings.enrichment_llm.model` (the `ModelTier`'s bare model/deployment string, `lode-568v.2`) at every enrichment write (`src/lode/enrich.py::_write_enrichment`) — this predates this decision; it is the same per-row provenance shape [storage.md](storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81) records on `annotations` rows. The inferred **edges** a run also writes carry no `model` column of their own (`src/lode/schema.sql`), but a run writes its annotations and its edges from one `EnrichmentResult` under one model, so the `annotations` scan below captures which model produced a given run's edges too. No schema change and no new write path were needed here.
- **No separate manifest.** As with the embedder, "the manifest" is an aggregate read over existing rows, not a new artifact: a `DISTINCT model` scan over `annotations WHERE source = 'ai'` answers "what did the enrichment store actually get built with," and detects a mix (e.g. after a mid-corpus `enrichment_llm` config change) with no additional bookkeeping. That mix is exactly what makes drift **detectable** — the acceptance bar this ticket is scoped to — and correcting it (a targeted re-enrich) is tracked separately, `lode-14jr` (not `lode-g274.7`, which built strictly to its own embedder-only scope — see [storage.md](storage.md#re-embedding-the-corpus-deliberately-lode-g2747)).
- **No attempt to pin beyond the bare ID.** `enrichment_llm`'s default (`claude-haiku-4-5`, [Models](#models) above) stays a plain build/runtime knob, recorded as-is.
- **The Q&A models are explicitly out of scope.** `qa_llm` / `qa_think_harder_llm` run at answer time and persist nothing — a change alters synthesis going forward, never the stored DB — so they keep a recorded default only ([Models](#models) above) with no provenance or pinning machinery attached, mirroring the embedder-vs-reranker/NLI split above.

**`lode status`'s hint upgraded from "mixed" to "stale-vs-current-config" (decided, lode-o9k3) — replaces, not supplements.** `lode-14jr`'s first cut of the `lode status` hint fired only on the bullet above's literal `COUNT(DISTINCT model) > 1` read — "the store has 2+ distinct recorded models." That check missed the primary intended workflow: deliberately bumping `enrichment_llm` on a corpus that was uniformly enriched under the OLD model leaves exactly **one** distinct stored model, so the 2+-distinct read stayed `False` while `lode reenrich` (`storage.md`'s [Re-enriching the corpus deliberately](storage.md#re-enriching-the-corpus-deliberately-targeted-lode-14jr) section) would in fact re-enqueue the entire corpus — `lode status` said "No action needed" while there was a corpus-wide backlog of it. The fix is to fire the hint on the same **live-head-scoped, stale-vs-current-config** condition `lode reenrich` itself already acts on (any live, non-`no_egress` head with an `'ai'` annotation whose `model` differs from `enrichment_llm` *right now*), not on the raw distinct-count. Once scoped identically, "stale" is a strict superset of the old "mixed" condition — any corpus with 2+ distinct recorded models still has at least one that disagrees with whatever `enrichment_llm` is currently configured to, so the new check fires everywhere the old one did, plus the uniform-disagreement case it missed. That makes this a straight **replacement**, not an additional hint alongside it: `src/lode/cli.py`'s `_enrichment_model_stale` now reads the identical query `lode reenrich` force-enqueues from (`_stale_enrichment_heads`), so "status says clean" and "reenrich has work" cannot disagree by construction — a separately-maintained approximation was exactly how they drifted apart the first time.

### Thinking on the Q&A synthesis call (decided, lode-3dlt)

`lode-d1sr` pinned `thinking={"type": "disabled"}` unconditionally on the Q&A
`messages.parse` call (`AnthropicProvider.structured_call`, no `tool_name`) so
that Opus-5-and-later's thinking-on-by-default couldn't share
[`qa.MAX_TOKENS`](#models) with the claims response and truncate it. That value
turned out to be **not universally accepted**: it 400s on Fable-class models
(`claude-fable-5`, `claude-mythos-5`) at any effort level, and on Opus 5 itself
when paired with effort `xhigh`/`max` — both reachable the moment
`qa_llm` / `qa_think_harder_llm` (both `Kind.RUNTIME`) are overridden to such a
model, turning a previously-working config into an unhandled
`anthropic.BadRequestError` deep in the provider.

**Fixed by never sending `disabled` at all** — `thinking` is now omitted
entirely on that branch, for every model, with no model-family predicate.
This is not just the simplest option (of the four considered — a
model→capability predicate, a config-load validation that blocks Fable-class
outright, and a catch-and-retry-without-the-param loop — see lode-3dlt's own
description for why each was passed over): disabling thinking is the
*disfavoured* setting on Opus 5 even where it's legal, so omitting it is also
the behaviorally better choice, not merely a workaround. `qa.MAX_TOKENS` was
raised `4096 -> 8192` to give the now-possible adaptive thinking room to share
the budget with the claims response — headroom, not a hard truncation
guarantee. Net effect: **Sonnet 4.6** (`qa_llm` default) is unaffected, since
it runs no thinking when the parameter is omitted (matching pre-`lode-d1sr`
behavior exactly); **Opus 5** (`qa_think_harder_llm` default) now runs adaptive
thinking instead of disabled thinking, a deliberate change; **Fable-class**
overrides now work.

**The enrichment forced-tool-use branch needed no code change** — it has never
sent `thinking` at all (`lode-d1sr` never touched it), so it already follows
the same "never explicitly disable" rule and cannot hit this 400 today. A
related but separate and currently-unreachable risk — a `Kind.RUNTIME`
override of `enrichment_llm` to a thinking-capable model would share its own
(smaller, unraised) `max_tokens=1024` between thinking and the forced
tool-call JSON — is tracked as a follow-up rather than fixed here, since it
needs its own tuning pass once someone actually wants that override.

### `reasoning_effort` wired to `output_config.effort` (decided, lode-wnz1)

A `ModelTier`'s `reasoning_effort` reaches Anthropic as `output_config.effort`
(`low`/`medium`/`high`/`xhigh`/`max` — GA since the 4.6 generation, `xhigh`
added on Opus 4.7) on every wire mechanism `AnthropicProvider` has: both
`structured_call` branches (the `messages.parse` Q&A path and the
forced-tool-use enrichment path) and `submit_batch`. Left unset — which is
every tier's default, and what a bare-string `enrichment_llm = "…"` coerces to
— the parameter is **omitted** from the request rather than sent as `null`. A
value outside the legal five raises `LLMProviderError` before any request is
sent, instead of being silently dropped.

**Caveat: the check is on the value, not on the value/model pairing.** Effort
is not accepted by every model — it errors outright on Haiku 4.5 and Sonnet
4.5, and `xhigh`/`max` do not exist on the 4.6 generation. All three tiers are
`Kind.RUNTIME` and two default to affected models (`enrichment_llm` = Haiku
4.5, `qa_llm` = Sonnet 4.6), so setting `reasoning_effort` on a tier whose
model does not support the level you ask for produces an unhandled
`anthropic.BadRequestError` — where before it was inert. **Set
`reasoning_effort` only alongside a model that supports it.** A
model→capability predicate was deliberately rejected as a moving target
(lode-3dlt's option 1); how to fail cleanly instead is tracked as lode-90o7,
which also covers the same unvalidated knob on `OpenAIProvider`.

**Interaction with the `thinking`-omission decision above: not reachable.**
Opus 5 rejects `thinking={"type": "disabled"}` paired with effort
`xhigh`/`max` (400) — the same family of incompatibility lode-3dlt exists to
dodge. Since lode-3dlt omits `thinking` entirely on every branch and this
change only adds `effort` alongside that omission, the combination cannot
occur; the `AnthropicProvider` docstring carries a standing note against
reintroducing an explicit `disabled`.

## Build constants (chosen once)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Content-address hash `H` | build | non-crypto 128-bit (xxh3-128) | Single-user/no-sync needs only low accidental-collision probability, not crypto resistance; length-prefixed framing. Changing `H` re-keys every node. blake2b-128 (stdlib) is the no-dep fallback. ([storage.md](storage.md#identity-vs-version)) |
| Single-instance advisory lock | build | on | Lockfile/PID beside the DB; required so async workers have a single owner. ([storage.md](storage.md#single-user-single-instance-linear-chains-no-merge)) |
| `ASSERTIVE_KINDS` (`lode.display`) | build | `{"action_item"}` | Annotation kinds the [stale-display policy](storage.md#stale-display-policy-decided-implemented-lode-npx4) hides (rather than shows-flagged) once not fresh. No extractor emits one yet — a forward-compatible hook for action-item extraction. |
| `_MODEL_CACHE_IDENTITY` (`lode.config`) | build | the `(sources.hf, model_file)` pair for each pinned model id above | On-disk cache identity for the pinned models — **duplicated from `fastembed`'s own `list_supported_models()`** so `lode status`'s cold-cache hint can answer "are the weights on disk?" via `huggingface_hub.try_to_load_from_cache` alone and **never `import fastembed`** (~830 modules via onnxruntime/numpy, ~740ms warm — it made a pure-DB-read command ~1.4x slower just to print "No action needed."; `lode-l38d.6`). Needed because the HF repo id can differ from the friendly model id (`BAAI/bge-small-en-v1.5` caches under `qdrant/bge-small-en-v1.5-onnx-q`). Deliberately duplicated data, so it can drift on a `fastembed` upgrade: `tests/test_model_cache_identity.py` is the drift guard, asserting the pin still matches the installed registry (**that** test may import `fastembed`; production must not). A model id outside this set — a `config.toml` override — falls back to importing the registry, which is fine: it is already off the fast path. |

## Python style: PEP 758 unparenthesized `except`

**Decided (lode-mkm):** with `requires-python = ">=3.14"` (lode-93o), lode adopts [PEP 758](https://peps.python.org/pep-0758/)'s unparenthesized multi-exception `except` clauses **tree-wide, deliberately** — `except ValueError, OSError:` rather than `except (ValueError, OSError):`. This is a 3.14-only style choice, not a workaround: no `# fmt: skip` pragma and no pinned older `[tool.ruff] target-version` — ruff infers `target-version` from `requires-python` and its formatter performs the flip automatically on every `ruff format .` / `nox -t fix`. Once flipped, the tree is stable (no further reformats on repeat runs).

**Caveat — `as`-bound clauses keep their parens.** PEP 758 only relaxes parentheses when the clause has no `as` binding. `except A, B as err:` is a `SyntaxError` under 3.14 ("multiple exception types must be parenthesized when using 'as'") — parens remain mandatory whenever a name is bound. So clauses like `src/lode/auth.py`'s `except (ValueError, KeyError, TypeError) as err:` are correctly left parenthesized by ruff; that is expected behavior, not a ruff gap or an inconsistency to fix.
