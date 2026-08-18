# lode — settings you can change

Every knob lode reads that you can change **while running it** -- an environment variable, a `config.toml` value, or a CLI flag. This is a filtered view of [configuration.md](https://github.com/bildzeitung/lode/blob/trunk/docs/configuration.md), the maintainer doc this page is generated from: that doc catalogues every tunable *and* every build-time knob together; this page keeps only the rows marked `runtime` there, since a build-time knob isn't something you can act on.

See [Paths & locations](#paths--locations) below for where `config.toml` lives and its format; a knob not listed there defaults, and there is no requirement to have a `config.toml` at all.

<a id="paths--locations"></a>
## Paths & locations

| Setting | Default | Notes |
|---|---|---|
| `LODE_HOME` | `~/.lode` | Root for all on-disk state. Env-var override; one directory holds the DB, vector store, logs, lock, and config. |
| Log directory | `$LODE_HOME/logs/` | Application logs. |
| `LODE_LOG_LEVEL` | `INFO` | lode's own root-logger level. Accepts a case-insensitive level name (`debug`, `info`, `warning`, ...); an unrecognized value raises rather than silently defaulting. Read when no level is passed explicitly. |
| `ANTHROPIC_LOG` | unset | Not a lode-specific knob — the Anthropic SDK's own wire-level debug switch. Set to `debug` or `info` and the SDK logs on the `anthropic` logger, which propagates to the root logger and is formatted/routed alongside lode's own logs. |
| `lode --debug` | off | Top-level CLI flag (`lode --debug <subcommand>`, e.g. `lode --debug tui`): forces the root-logger level to `DEBUG` for that invocation, which also flips on every DEBUG-gated diagnostic (e.g. the TUI's event-loop-lag `latency_probe`). Takes precedence over `LODE_LOG_LEVEL` when passed; omit it and `LODE_LOG_LEVEL` (default `INFO`) still applies unchanged. In the TUI this only raises verbosity in the log file — the console stays suppressed either way; for plain CLI commands it raises both stderr and file verbosity. |
| Config file path | `$LODE_HOME/config.toml` | User-editable runtime knobs. **Optional** — if absent, every knob uses its default below; no config file is a valid, fully-working state. |

<a id="retrieval-and-ranking"></a>
## Retrieval and ranking

| Setting | Default | Notes |
|---|---|---|
| Rerank stage | on | Toggle the cross-encoder stage on/off (the *seam* is permanent; the stage is switchable). |

<a id="faithfulness-gate"></a>
## Faithfulness gate

| Setting | Default | Notes |
|---|---|---|
| LLM-judge second pass | off | Optional "high-assurance" verification; costs a round-trip + $ + off-box egress. |

<a id="tui--passive-connection-surfacing-e11"></a>
## TUI — passive connection surfacing (E11)

| Setting | Default | Notes |
|---|---|---|
| Related-notes enabled | on | Master on/off switch for the passive related-notes pass. Off skips the pass entirely — no FTS5/embedder/LanceDB work runs on the input path. **This is a user preference, not a lag fix**: a lag-diagnosis spike confirmed the pass already runs off the UI thread (fastembed/ONNX releases the GIL), so turning it off does not change keystroke latency. |
| Related-notes debounce | `500ms` | Idle-typing delay in the capture screen before a passive "you wrote about this" pass runs ([design.md](design.md) §2 "Surfacing connections"); restarted on every keystroke so a burst of typing triggers at most one pass per pause. |
| Related-notes result count | `5` | Max related past notes shown per pass. |
| Related-notes minimum draft length | `20` chars | Below this (stripped) length, no pass runs at all — no DB connection opened. |

<a id="tui--ask-screen-citation-rendering"></a>
## TUI — ask screen citation rendering

| Setting | Default | Notes |
|---|---|---|
| Ask context chars (`ask_context_chars`) | `80` | Characters of a cited note/external's body shown before and after a citation's `quoted_span` when the ask screen groups citations by their cited note/external. Applies only to a citation whose identity resolved to a note/external — a citation whose target the store had nothing to resolve falls back to the flat, ungrouped rendering with no context. |

<a id="tui--theme"></a>
## TUI — theme

| Setting | Default | Notes |
|---|---|---|
| TUI theme (`[tui.theme]`) | absent | Optional, `extra="forbid"` section. Absent leaves every current default byte-identical (Textual's own `textual-dark` chrome, lode's `NOTE_BODY_SYNTAX_STYLES` note-body palette). `name` is any theme Textual registers by default; `tui.theme.colors` overrides that theme's colour variables (fixed key set: `primary`, `secondary`, `warning`, `error`, `success`, `accent`, `foreground`, `background`, `surface`, `panel`, `boost`); `tui.theme.syntax` overrides the note-body markdown palette (closed key set: `text_literal`, `punctuation_delimiter`, `heading_marker`, `heading`, `list_marker` — `_` stands in for tree-sitter's `.`, so its vocabulary never becomes config surface). Precedence: `name` → `colors` → `syntax`. Every value is a colour-only string, parsed at config load (`textual.color.Color.parse`); a bad value or unknown key is a load error naming the offending key. Run `lode theme export` to print the fully-resolved effective theme as ready-to-paste TOML rather than typing keys from memory. Design record: [decisions.md](decisions.md)'s `lode-dmbc` entry (2026-08-17 update). |

<a id="async-work-queue"></a>
## Async work queue

| Setting | Default | Notes |
|---|---|---|
| Reconciliation scan interval | periodic | How often the self-healing scan re-enqueues missing derived work. ([storage.md](storage.md#the-async-work-queue)) |
| Retry backoff + max attempts | exp backoff, capped | Transient-failure retry before dead-lettering a job. |
| Stale-running reclaim timeout | `900s` (15 min) | A job stuck in `status='running'` this long (no claim update, e.g. a worker crash) is reclaimed — same retry/dead-letter accounting as a handler failure. Excludes batch-backed enrich jobs. |
| Enrichment batch flush policy | size/time | When accumulated `enrich` jobs are submitted through the active provider's batch path. |
| Batch collect failure budget (`batch_collect_failure_budget`) | `5` | Consecutive `collect_enrich_batch()` failures (the poll call itself raising, not an individual result's errored/expired/canceled outcome) at which one `batch_handle`'s still-`running` jobs are dead-lettered — so N-1 are tolerated and the Nth is fatal. Resets to 0 on any poll that doesn't raise, so it counts *consecutive* failures, not a lifetime total. Closes the last of the three poison-pill axes `_batch_collect_enrich`'s per-handle isolation left open. |
| `work --wait` timeout | `1800s` (30 min) | Max time `lode work --wait` blocks polling for the queue to fully drain (incl. collected Batches API enrich results) before exiting non-zero and naming the still-pending/running jobs. The Batches API SLA is up to 24h, so `--wait` can legitimately time out on a large enrich load -- that's expected, not a bug; it suits embed-heavy or small-batch cases, and a big async enrich backlog may need a plain re-run of `lode work` instead. |
| Progress heartbeat interval (`progress_heartbeat_interval_s`) | `15s` | How often `lode work` logs a "still running" heartbeat line (`lode.progress.op_progress`) for a named long-running op -- a `reconcile()` step, a `drain()` batch pre-step, or the main claim/run loop -- that hasn't finished yet. Makes a stuck op visible instead of silent, even where it can't be safely aborted outright (e.g. a local ONNX model load or a SQL scan). |
| Enrich call timeout (`enrich_call_timeout_s`) | `120s` | Per-call client-side timeout passed to every **enrichment** cloud-LLM call through the `LLMProvider` seam, immediate and batch alike, under whichever provider is active: the calls reachable from `lode work` -- bounds a hung network call rather than letting it block forever. Renamed vendor-neutral from `anthropic_call_timeout_s`, then renamed again from `llm_call_timeout_s` to `enrich_call_timeout_s` once the `qa_call_timeout_s` split left the general name covering only this enrichment subset; a `config.toml` still carrying either old key is remapped by `load_settings()`. Distinct from Fetch timeout below, which governs web draw-down HTTP fetches, not LLM provider calls. Does not reach the Q&A synthesis call -- see [`qa_call_timeout_s`](#models). |
| VectorStore optimize interval (`vectorstore_optimize_interval`) | `200` | How often a `VectorStore` holding its opened LanceDB Table across many `replace_vectors()` calls runs `table.optimize()` to prune old versions. Bounds the held Table's version-history-linked memory growth, which was measured to be linear and effectively unbounded over a long `lode work --loop` process otherwise -- see [`docs/decisions.md`](decisions.md). |

<a id="externals-with-connectors"></a>
## Externals (with connectors)

| Setting | Default | Notes |
|---|---|---|
| Refresh TTL (`refresh_ttl_s`) | `3600` (1h) | How long a web external's head snapshot may go un-revalidated before `lode.reconcile`'s `refresh_stale` step re-enqueues a `refresh` job for it. Decided **scheduled TTL sweep**, not true on-access revalidation — see externals.md. A single default today (no per-source override); a closed ticket rarely changing vs. an active PR changing hourly is exactly the kind of per-source judgment a future connector may want its own TTL for. |
| Fetch timeout | `10s` | Per-fetch HTTP timeout; a timeout is a TRANSIENT failure (retried by the async queue), not a tombstone — as is a server-reported `408 Request Timeout`. |
| Fetch max redirects | 5 | 3xx redirects a single web-fetch follows before tombstoning as unresolvable. **Distinct from Draw-down hop limit above** — this caps redirects *within one fetch*; the hop limit caps crawling a fetched page's *own outbound links*. |
| URL tracking-param blocklist | `utm_*`, `fbclid`, `gclid` | Query params stripped during URL canonicalization before the `external_id` dedup key is computed. A trailing `*` matches a prefix (case-insensitive); everything else matches exactly. This same canonical form is the `lode-w0h.6` refresh policy's join key for "the same source" across refetches — and, since `lode-0as`, also strips userinfo (`user:pass@`) so credentials in a pasted URL never enter it. |
| `jira_enabled` | `false` | Feature flag for the JIRA Cloud API connector. |
| `confluence_enabled` | `false` | Feature flag for the Confluence Cloud API connector. |
| `jira_base_url` | `""` (empty) | API base override, e.g. `https://acme.atlassian.net`. Empty means infer from the pasted link at detection time. A non-empty value must be a well-formed `http(s)` URL — a malformed one fails validation at `Settings()` construction. |
| `confluence_base_url` | `""` (empty) | Same shape as `jira_base_url`, for Confluence. |
| `LODE_JIRA_TOKEN` env var / `jira_token` (config.toml fallback) | unset / `""` | JIRA Cloud API token. Resolved **env-var PRIMARY**: `LODE_JIRA_TOKEN` is checked first, then the `jira_token` key in `config.toml` as fallback. No secret is required to live in `config.toml`. **The raw value is never logged, echoed, or shown by `lode config`** — `secret=True` shows only a presence indicator in the knob table, never the value (see below). |
| `LODE_JIRA_EMAIL` env var / `jira_email` (config.toml fallback) | unset / `""` | JIRA Cloud Basic-auth account email — same env-first, config.toml-fallback resolution as the token, and the same `secret=True` presence-only treatment. |
| `LODE_CONFLUENCE_TOKEN` env var / `confluence_token` (config.toml fallback) | unset / `""` | Confluence Cloud API token — same resolution and secrecy guarantee as `jira_token`. |
| `LODE_CONFLUENCE_EMAIL` env var / `confluence_email` (config.toml fallback) | unset / `""` | Confluence Cloud Basic-auth account email — same resolution and secrecy guarantee as `jira_email`. |

<a id="privacy--egress"></a>
## Privacy & egress

| Setting | Default | Notes |
|---|---|---|
| `no_egress` (per note / source) | off | Indexed locally, never sent to the configured cloud LLM (no enrichment, excluded from cloud Q&A; cited as "withheld"). Set/cleared per note via `lode no-egress --note <note_id>` (`--clear` to unset) or the TUI browse screen's `n` toggle, and per external source via `lode no-egress <external_id>` — both write through the same `no_egress` column, just via a note-side vs. externals-side setter. ([externals.md](externals.md#privacy-consequence-of-aggregation)) |
| `no_egress_scopes` | `[]` | Declarative no_egress SCOPE rules — see below. |
| Redact-before-egress pattern set | high-precision seed | Secret patterns stripped before content is sent to the configured cloud LLM; iterate from real misses. ([decisions.md](decisions.md)) |
| Redact-before-index pattern set | high-precision seed | Secret patterns kept out of the local vector/FTS index. |

<a id="tool-augmented-ask"></a>
## Tool-augmented Ask

| Setting | Default | Notes |
|---|---|---|
| `ask_tools_enabled` | `false` | Feature flag: offer the read-only `search_jira`/`search_confluence`/`fetch` tools to the Q&A synthesis call. Off by default — `lode.tool_dispatch.build_ask_tools` returns `()` regardless of what a caller passes as `answer_question`'s own `tools_enabled` argument, so notes-only behaviour is unchanged either way. **Reachable from a real `lode ask`** — `cited_answer.ask` (the single path both the CLI and the TUI take) always passes `tools_enabled=True`, so this flag alone decides whether a real ask can call the tools. |
| `ask_tool_budget` | `6` | Per-ask tool-call budget — search and fetch share **one** counter (`lode.tool_dispatch.ToolBudget`), enforced before each dispatch; a call past the budget is refused (the model is told so, via the tool result text) rather than dispatched. Distinct from `_DEFAULT_MAX_TOOL_TURNS` above (a provider-level free-turn cap — one turn is not assumed to be one tool call). |

<a id="models"></a>
## Models

| Setting | Default | Notes |
|---|---|---|
| LLM provider (`llm_provider`) | `anthropic` | Which `LLMProvider` implementation every cloud-LLM call site resolves against — whole-app, not per-surface: setting this sets it for enrichment AND Q&A together. `"anthropic"` \| `"openai"` — `"openai"` routes to direct OpenAI or Azure OpenAI depending on `azure_openai_endpoint`; Azure-vs-direct-OpenAI is a routing detail under this one value, not a second provider value. |
| Azure OpenAI endpoint (`azure_openai_endpoint`) | `""` | The resource **root**, e.g. `https://{resource}.openai.azure.com` — do **not** append `/openai`; the openai SDK's `AzureOpenAI` client adds that segment itself, so `.../openai` doubles the path and every request 404s. Empty means direct OpenAI (or a non-`"openai"` `llm_provider`). Requires `azure_openai_api_version` to also be set (validated at `Settings` construction). |
| Azure OpenAI api-version (`azure_openai_api_version`) | `""` | e.g. `2025-04-01-preview` — sent as a query param on every request, not a header. Required when `azure_openai_endpoint` is set. |
| LLM provider credentials | env/SDK-only | **No key ever lives in `config.toml`** — unlike the Jira/Confluence credentials above, there is no config-file fallback for these. `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (unchanged) resolve via the Anthropic SDK's own credential chain under the default `llm_provider`; `OPENAI_API_KEY` (direct OpenAI) or `AZURE_OPENAI_API_KEY` (when `azure_openai_endpoint` is set) resolve under `llm_provider = "openai"`. A missing credential raises `AuthError` (Anthropic) or `LLMAuthError` (OpenAI/Azure) with a provider-appropriate message naming the exact env var to set — never a traceback. |
| Enrichment LLM (`enrichment_llm`) | Claude Haiku 4.5 (default provider) | High-volume background extraction. A `(model, reasoning_effort, max_tokens)` `ModelTier` — a bare TOML string still coerces to a `ModelTier` with `reasoning_effort=None` and `max_tokens=None`. `model` is interpreted **against the active `llm_provider`**: an Anthropic model id under the default provider, or an Azure/OpenAI deployment name under `llm_provider = "openai"`. `max_tokens`, when set, overrides `enrich.MAX_TOKENS` (2048) for both the immediate and batch enrichment calls. Persists into the DB (`annotations`/`edges`) — DB-affecting; the model (and, once non-Anthropic, the provider) is recorded per-row on `annotations.model`/`annotations.provider` and drift is detected from it, never pinned. |
| Q&A LLM (`qa_llm`) | Claude Sonnet 4.6 (default provider) | Default interactive synthesis model. A `ModelTier`, same shape as Enrichment LLM — `model` is likewise interpreted against the active `llm_provider` (an Azure/OpenAI deployment name under `llm_provider = "openai"`). `max_tokens`, when set, overrides `qa.MAX_TOKENS` (8192). Answer-time only, persists nothing — recorded default, no provenance machinery. |
| Q&A "think harder" (`qa_think_harder_llm`) | Opus 5 (toggle, default provider) | Higher-quality, higher-cost synthesis on demand. A `ModelTier`, same provider-relative interpretation as the two knobs above — "think harder" can be a deployment swap (today's Anthropic Sonnet→Opus default), a `reasoning_effort` bump, a `max_tokens` override, or any combination, on the same deployment. Answer-time only, persists nothing — recorded default, no provenance machinery. |
| Q&A call timeout (`qa_call_timeout_s`) | `300s` | Budget for the Q&A synthesis call, split off `enrich_call_timeout_s` — below. **Per-RUN, not per-call**: with `tools_enabled=False` (or `ask_tools_enabled=false`, above) a "run" is exactly one call and this is unchanged from before lode-35nu.11.6; with the Ask tools enabled, this same value bounds the whole free-tool-turns-then-forced-answer run, not each turn individually. **Derived, not a measured p95.** Does not reach `enrich.py`'s three call sites — those stay on `enrich_call_timeout_s` (120s) unchanged, and are unaffected by this ticket (still calling `structured_call` directly). The derivation, the retained SDK retry-on-timeout it was chosen alongside, and the `ModelTier.max_tokens` override that invalidates it are all in the write-up linked above, deliberately not restated here. |
| HF probe timeout (`hf_probe_timeout_s`) | `5s` | Per-call timeout passed to `huggingface_hub.model_info()` by the indexing-side revision probe (`resolve_model_revision`, below). Bounds a black-holed network to this instead of the OS TCP connect timeout, which the probe used to block for before falling back to `model_revision = NULL` anyway. Matches `httpx`'s own default rather than the Fetch timeout below (`10s`, web content fetches) — this is a small metadata GET, not a page fetch. Bounds the probe only, **not** `fastembed`'s weights download (next section). What a float timeout actually bounds in `httpx`, with the measurement: `docs/decisions.md`, the `lode-w5nr` entry. |
