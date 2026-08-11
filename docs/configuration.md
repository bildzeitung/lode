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
| `lode --debug` | runtime | off | Top-level CLI flag (`lode --debug <subcommand>`, e.g. `lode --debug tui`): forces the root-logger level to `DEBUG` for that invocation, which also flips on every DEBUG-gated diagnostic (e.g. the TUI's event-loop-lag `latency_probe`). Takes precedence over `LODE_LOG_LEVEL` when passed; omit it and `LODE_LOG_LEVEL` (default `INFO`) still applies unchanged. In the TUI this only raises verbosity in the log file — the console stays suppressed either way (`lode-1i8.2`); for plain CLI commands it raises both stderr and file verbosity (`src/lode/cli/__init__.py::main`). |
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
catches both at its boundary (`src/lode/cli/__init__.py::_resolve_settings`) and reports
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

## TUI — ask screen citation rendering (lode-35nu.3)

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Ask context chars (`ask_context_chars`) | runtime | `80` | Characters of a cited note/external's body shown before and after a citation's `quoted_span` when the ask screen groups citations by their cited note/external. Applies only to a citation whose identity resolved to a note/external (`lode-35nu.1`) — a citation whose target the store had nothing to resolve falls back to the flat, ungrouped rendering with no context. |

## Async work queue

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Reconciliation scan interval | runtime | periodic | How often the self-healing scan re-enqueues missing derived work. ([storage.md](storage.md#the-async-work-queue)) |
| Retry backoff + max attempts | runtime | exp backoff, capped | Transient-failure retry before dead-lettering a job. |
| Stale-running reclaim timeout | runtime | `900s` (15 min) | A job stuck in `status='running'` this long (no claim update, e.g. a worker crash) is reclaimed — same retry/dead-letter accounting as a handler failure. Excludes batch-backed enrich jobs. ([storage.md](storage.md#crash-reclaim-a-job-stuck-in-running--pinned-lode-aor)) |
| Enrichment batch flush policy | runtime | size/time | When accumulated `enrich` jobs are submitted through the active provider's batch path (Anthropic's Batches API by default, or serialized sequential calls under a provider with no batch API — [LLM provider seam](stack.md#llm-provider-seam-decided-lode-568v1)). |
| Batch collect failure budget (`batch_collect_failure_budget`) | runtime | `5` | Consecutive `collect_enrich_batch()` failures (the poll call itself raising, not an individual result's errored/expired/canceled outcome) at which one `batch_handle`'s still-`running` jobs are dead-lettered — so N-1 are tolerated and the Nth is fatal. Resets to 0 on any poll that doesn't raise, so it counts *consecutive* failures, not a lifetime total. Closes the last of the three poison-pill axes `_batch_collect_enrich`'s per-handle isolation left open (`lode-u6he`; [storage.md](storage.md#transient-vs-permanent-job-failures--pinned-lode-9yy)). |
| `work --wait` timeout | runtime | `1800s` (30 min) | Max time `lode work --wait` blocks polling for the queue to fully drain (incl. collected Batches API enrich results) before exiting non-zero and naming the still-pending/running jobs. The Batches API SLA is up to 24h, so `--wait` can legitimately time out on a large enrich load -- that's expected, not a bug; it suits embed-heavy or small-batch cases, and a big async enrich backlog may need a plain re-run of `lode work` instead. |
| Progress heartbeat interval (`progress_heartbeat_interval_s`) | runtime | `15s` | How often `lode work` logs a "still running" heartbeat line (`lode.progress.op_progress`) for a named long-running op -- a `reconcile()` step, a `drain()` batch pre-step, or the main claim/run loop -- that hasn't finished yet (`lode-olmi.15`). Makes a stuck op visible instead of silent, even where it can't be safely aborted outright (e.g. a local ONNX model load or a SQL scan). |
| Enrich call timeout (`enrich_call_timeout_s`) | runtime | `120s` | Per-call client-side timeout passed to every **enrichment** cloud-LLM call through the `LLMProvider` seam (`lode-568v.2`/`.3`), immediate and batch alike, under whichever provider is active: the calls reachable from `lode work` (`enrich.py` -- the batch-path pre-steps and the immediate structured-output call a residual enrich job can take in `drain()`'s main loop) -- bounds a hung network call rather than letting it block forever (`lode-olmi.15`). Renamed vendor-neutral from `anthropic_call_timeout_s` (`lode-568v.1`/`.2`), then renamed again from `llm_call_timeout_s` to `enrich_call_timeout_s` (`lode-7y6s`) once the `qa_call_timeout_s` split left the general name covering only this enrichment subset; a `config.toml` still carrying either old key is remapped by `load_settings()`. Distinct from Fetch timeout below, which governs web draw-down HTTP fetches, not LLM provider calls. Does not reach the Q&A synthesis call (`qa.py`) -- see [`qa_call_timeout_s`](#models) (`lode-wfyx`). |
| VectorStore optimize interval (`vectorstore_optimize_interval`) | runtime | `200` | How often a `VectorStore` holding its opened LanceDB Table across many `replace_vectors()` calls (a caller sharing one instance across a drain, e.g. `lode.worker.drain`'s `store=` seam, `lode-2brb`) runs `table.optimize()` to prune old versions. Bounds the held Table's version-history-linked memory growth, which was measured to be linear and effectively unbounded over a long `lode work --loop` process otherwise -- see [`docs/decisions.md`](decisions.md) (`lode-2brb`). |

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
| `no_egress` (per note / source) | runtime | off | Indexed locally, never sent to the configured cloud LLM (no enrichment, excluded from cloud Q&A; cited as "withheld"). Set/cleared per note via `lode no-egress --note <note_id>` (`--clear` to unset) or the TUI browse screen's `n` toggle, and per external source via `lode no-egress <external_id>` (lode-82wt) — both write through the same `no_egress` column, just via a note-side vs. externals-side setter. ([externals.md](externals.md#privacy-consequence-of-aggregation)) |
| `no_egress_scopes` | runtime | `[]` | Declarative no_egress SCOPE rules (lode-35nu.11.8) — see below. |
| Redact-before-egress pattern set | runtime | high-precision seed | Secret patterns stripped before content is sent to the configured cloud LLM; iterate from real misses. ([decisions.md](decisions.md)) |
| Redact-before-index pattern set | runtime | high-precision seed | Secret patterns kept out of the local vector/FTS index. |

### `no_egress_scopes`: scope-level no_egress rules (decided, lode-35nu.11.8)

The per-row `externals.no_egress` flag (`lode no-egress <external_id>`) can only mark a resource that
already has an `externals` row — it structurally cannot cover an external a tool has not fetched yet.
`no_egress_scopes` closes that gap: a list of declarative rules, each `{source_type, match}`,
evaluated **live** against a candidate `(external_id, source_type)` pair — no row required, and
**never materialized onto a row**. Adding a rule covers every matching external immediately
(already-captured or not); removing one un-withholds immediately. Neither direction backfills or
migrates any `externals` row.

```toml
[[no_egress_scopes]]
source_type = "jira"
match = "PROJ"        # JIRA project key -- matches issue keys "PROJ-<number>"

[[no_egress_scopes]]
source_type = "web"
match = "internal.example.com"   # exact URL host, not a host+path prefix
```

- `source_type = "jira"` — `match` is a JIRA project key, matched against the project-key prefix of
  a candidate JIRA issue key (`externals.external_id` for `source_type='jira'` is the issue key
  itself, e.g. `"PROJ-123"`). The **whole** key boundary must line up: rule `PROJ` covers `PROJ-123`
  but not `PROJECT-1`. Matched **case-insensitively** — `drawdown.py`'s `_JIRA_ISSUE_RE` preserves
  whatever case the pasted URL used, so `/browse/proj-123` persists `"proj-123"`.
- `source_type = "web"` — `match` is a URL **host**, matched **exactly** (host-only, not a host+path
  prefix, and not a suffix — `example.com` covers `example.com` but neither `evil-example.com` nor
  `sub.example.com`; a path-prefix variant is a documented future option if ever needed, not built
  speculatively). The comparison is against the parsed host, so userinfo, a non-default port, host
  case, and a trailing root dot cannot slip content past a rule
  (`https://user@Internal.Example.com:8443/x` is covered by `internal.example.com`). Rule and
  candidate are both lowercased and stripped of a trailing dot, so a rule written
  `Internal.Example.com.` still works.
- `source_type = "confluence"` is **rejected at config-load time** with a clear
  `ValidationError` naming the reason. Confluence space-key scoping is structurally impossible under
  the current data model: `drawdown.py`'s `_CONFLUENCE_PAGE_RE` persists only the numeric page id
  into `externals.external_id`; the space key is discarded at detection time and stored nowhere, so a
  space-scoped rule would have no space information to ever match against — not merely unimplemented.
  Accepting such a rule as a silent no-op was explicitly rejected (human decision, `lode-35nu.11.8`):
  a rule that can never match must fail loudly at load, not match nothing with no signal. See
  [externals.md](externals.md#no-egress-scope-rules-decided-lode-35nu118) for the full write-up.

**Composition with the per-row flag:** both are evaluated, and either denying is a denial — a scope
rule never overrides an explicit per-row `--clear`, and a per-row flag never overrides a scope rule.

**Fail-closed, in both places.** A rule that could never match anything is refused at load with a
`ValidationError` — an empty `match`, an unsupported `source_type`, or `confluence` — because a
privacy rule that silently matches nothing is worse than no rule at all: the user believes they are
covered. At evaluation time, if matching a rule raises (an unparseable candidate `external_id`, say),
the candidate is treated as **scoped and withheld** rather than allowed. That never withholds the
world, because a candidate is only ever parsed once a rule of its own `source_type` exists — with the
default empty rule set there is nothing to fail.

**Not a generic seam.** `no_egress` is read by SQL `JOIN` at two call sites —
`cited_answer._resolve_targets` and `enrich._resolve_enrich_target` — so a config predicate cannot
live inside the join. `lode.no_egress_scope.is_no_egress_scoped` is the one shared predicate; each
site composes it with its own per-row flag itself, rather than reimplementing the match.

## Tool-augmented Ask (lode-8hsk / lode-35nu.11.2 / lode-8vvp)

**Status: wired into the production path.** `cited_answer.ask` — what `lode ask` and the TUI ask
service both call — passes `tools_enabled=True` unconditionally to `qa.answer_question`; whether the
tools are actually offered is gated solely by `ask_tools_enabled` below (`lode.tool_dispatch.build_ask_tools`
returns `()` while the flag is `False`, so the notes-only path stays byte-for-byte unchanged with it
off). A snapshot a `fetch` tool call persists mid-run is resolved into the faithfulness gate's bodies
map after synthesis returns (`cited_answer._resolve_tool_snapshots`) — unconditionally gate-eligible,
since `lode.tools.fetch_for_ask` already refuses to persist a `no_egress` destination at all.

| Knob | Kind | Default | Notes |
|---|---|---|---|
| `ask_tools_enabled` | runtime | `false` | Feature flag: offer the read-only `search_jira`/`search_confluence`/`fetch` tools to the Q&A synthesis call. Off by default — `lode.tool_dispatch.build_ask_tools` returns `()` regardless of what a caller passes as `answer_question`'s own `tools_enabled` argument, so notes-only behaviour is unchanged either way. **Reachable from a real `lode ask`** (`lode-8vvp`) — `cited_answer.ask` (the single path both the CLI and the TUI take) always passes `tools_enabled=True`, so this flag alone decides whether a real ask can call the tools. |
| `ask_tool_budget` | runtime | `6` | Per-ask tool-call budget — search and fetch share **one** counter (`lode.tool_dispatch.ToolBudget`), enforced before each dispatch; a call past the budget is refused (the model is told so, via the tool result text) rather than dispatched. Distinct from `_DEFAULT_MAX_TOOL_TURNS` above (a provider-level free-turn cap — one turn is not assumed to be one tool call). |

**Tool set.** `search_jira`/`search_confluence` return **identifiers and titles only** —
`lode.jira_fetch.JiraSearchHit`/`lode.confluence.ConfluenceSearchHit` each carry exactly
`external_id` + `title`; no body/snippet field exists on either dataclass, so the schema makes a leak
impossible rather than merely absent. `fetch` delegates wholly to `lode.tools.fetch_for_ask`
(`lode-35nu.11.1`) — the one path that ever persists a citable snapshot. No write verb is defined
anywhere in `build_ask_tools`: there is nothing to disable, because nothing writes to JIRA, Confluence,
or the web.

`search_jira` targets `GET /rest/api/3/search/jql` — the CHANGE-2046 replacement for the retired
`GET/POST /rest/api/3/search` (verified finding, `lode-6nwu`; see [decisions.md](decisions.md)) — with
`fields=summary` passed **explicitly** (the replacement endpoint defaults to returning `id` only) and
`jql` always a bounded `text ~ "..."` clause (the endpoint rejects an unbounded query). `search_jira`
is only offered when `jira_active(settings)` **and** `jira_base_url` is configured — a search call, unlike
a fetch of an already-drawn-down issue, has no pasted link to infer an `api_base` from.
`search_confluence` is the CQL equivalent (`type=page AND text ~ "..."`), gated the same way on
`confluence_active(settings)` + `confluence_base_url`. Both single-page (no cursor/CQL pagination) — a
tool-search call is not a full corpus traversal.

**Egress.** Each search or fetch call writes one `purpose='tool'` `egress_log` row (`lode.tools.log_tool_egress`,
shared by both legs) **before** the request goes out, with the query/arguments redacted through the
same `redact_before_egress_counting` path the fetch legs already used. A search call's row carries
`sent_targets=()` (a query has no resolved citation target yet — [externals.md](externals.md) "A query
result has no identity"). Search results are then filtered through the exact same `no_egress_denied`
predicate (`lode.tools.no_egress_denied` — per-row flag OR `no_egress_scopes` rule, above) a fetch
call already enforces pre-fetch: a denied hit is dropped **whole**, id and title together, before it
ever reaches the model.

**Q&A synthesis prompt.** `lode.qa`'s system prompt is chosen from whether the `tools` tuple
`run_tool_turns` receives is non-empty — never from a second flag — so `ask_tools_enabled=false`
(the tuple collapsing to `()`) reproduces the pre-lode-8hsk notes-only prompt byte-for-byte. The
tool-aware prompt keeps the verbatim-span rule and the never-from-model-knowledge rule intact (the
faithfulness gate downstream is unmodified) while permitting the one path the notes-only prompt
forbade: calling a tool, and citing a `snapshot_id` it returns, exactly like any other external
citation target.

## Models

| Knob | Kind | Default | Notes |
|---|---|---|---|
| Embedding model | build | `nomic-ai/nomic-embed-text-v1.5` | Local ONNX via `fastembed`. A change re-keys the vector space → full re-embed + re-index. ([stack.md](stack.md)) |
| Embedding vector dimension | build | `768` | Output dimension of the embedding model. **LanceDB table creation needs this fixed**; it must match the model (`nomic-embed-text-v1.5` → 768). Re-keying it = full re-embed. |
| LLM provider (`llm_provider`) | runtime | `anthropic` | Which `LLMProvider` implementation every cloud-LLM call site resolves against (`lode-568v.2`/`.3`) — whole-app, not per-surface: setting this sets it for enrichment AND Q&A together. `"anthropic"` \| `"openai"` — `"openai"` routes to direct OpenAI or Azure OpenAI depending on `azure_openai_endpoint`; Azure-vs-direct-OpenAI is a routing detail under this one value, not a second provider value. ([stack.md](stack.md#llm-provider-seam-decided-lode-568v1)) |
| Azure OpenAI endpoint (`azure_openai_endpoint`) | runtime | `""` | The resource **root**, e.g. `https://{resource}.openai.azure.com` (`lode-568v.3`) — do **not** append `/openai`; the openai SDK's `AzureOpenAI` client adds that segment itself, so `.../openai` doubles the path and every request 404s. Empty means direct OpenAI (or a non-`"openai"` `llm_provider`). Requires `azure_openai_api_version` to also be set (validated at `Settings` construction). |
| Azure OpenAI api-version (`azure_openai_api_version`) | runtime | `""` | e.g. `2025-04-01-preview` — sent as a query param on every request, not a header (`lode-568v.3`). Required when `azure_openai_endpoint` is set. |
| LLM provider credentials | runtime | env/SDK-only | **No key ever lives in `config.toml`** — unlike the Jira/Confluence credentials above, there is no config-file fallback for these. `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (unchanged) resolve via the Anthropic SDK's own credential chain under the default `llm_provider`; `OPENAI_API_KEY` (direct OpenAI) or `AZURE_OPENAI_API_KEY` (when `azure_openai_endpoint` is set) resolve under `llm_provider = "openai"`. A missing credential raises `AuthError` (Anthropic) or `LLMAuthError` (OpenAI/Azure) with a provider-appropriate message naming the exact env var to set — never a traceback ([stack.md](stack.md#llm-provider-seam-decided-lode-568v1)). |
| Enrichment LLM (`enrichment_llm`) | runtime | Claude Haiku 4.5 (default provider) | High-volume background extraction. A `(model, reasoning_effort, max_tokens)` `ModelTier` (`lode-568v.2`; `max_tokens` `lode-d70n`) — a bare TOML string still coerces to a `ModelTier` with `reasoning_effort=None` and `max_tokens=None`. `model` is interpreted **against the active `llm_provider`**: an Anthropic model id under the default provider, or an Azure/OpenAI deployment name under `llm_provider = "openai"`. `max_tokens`, when set, overrides [`enrich.MAX_TOKENS`](#per-tier-max_tokens-override-decided-lode-d70n) (2048) for both the immediate and batch enrichment calls. Persists into the DB (`annotations`/`edges`) — DB-affecting; the model (and, once non-Anthropic, the provider) is recorded per-row on `annotations.model`/`annotations.provider` and drift is detected from it, never pinned. ([below](#model-provenance-the-enrichment-llm-decided-lode-g2745)) |
| Q&A LLM (`qa_llm`) | runtime | Claude Sonnet 4.6 (default provider) | Default interactive synthesis model. A `ModelTier`, same shape as Enrichment LLM — `model` is likewise interpreted against the active `llm_provider` (an Azure/OpenAI deployment name under `llm_provider = "openai"`). `max_tokens`, when set, overrides [`qa.MAX_TOKENS`](#per-tier-max_tokens-override-decided-lode-d70n) (8192). Answer-time only, persists nothing — recorded default, no provenance machinery. |
| Q&A "think harder" (`qa_think_harder_llm`) | runtime | Opus 5 (toggle, default provider) | Higher-quality, higher-cost synthesis on demand. A `ModelTier`, same provider-relative interpretation as the two knobs above — "think harder" can be a deployment swap (today's Anthropic Sonnet→Opus default), a `reasoning_effort` bump, a `max_tokens` override, or any combination, on the same deployment. Answer-time only, persists nothing — recorded default, no provenance machinery. |
| Q&A call timeout (`qa_call_timeout_s`) | runtime | `300s` | Budget for the Q&A synthesis call (`qa.py`, routed through `LLMProvider.run_tool_turns` — `lode-35nu.11.6`), split off `enrich_call_timeout_s` (`lode-wfyx`) — [below](#qa-call-timeout-split-from-llm_call_timeout_s-decided-lode-wfyx). **Per-RUN, not per-call** (`lode-35nu.11.6`, [stack.md](stack.md#7-multi-turn-tool-use--llmproviderrun_tool_turns-decided-lode-35nu116)): with `tools_enabled=False` (or `ask_tools_enabled=false`, above) a "run" is exactly one call and this is unchanged from before lode-35nu.11.6; with the [Ask tools](#tool-augmented-ask-lode-8hsk--lode-35nu112--lode-8vvp) enabled, this same value bounds the whole free-tool-turns-then-forced-answer run, not each turn individually. **Derived, not a measured p95.** Does not reach `enrich.py`'s three call sites — those stay on `enrich_call_timeout_s` (120s) unchanged, and are unaffected by this ticket (still calling `structured_call` directly). The derivation, the retained SDK retry-on-timeout it was chosen alongside, and the `ModelTier.max_tokens` override that invalidates it are all in the write-up linked above, deliberately not restated here. |
| Max free tool turns (`_DEFAULT_MAX_TOOL_TURNS`) | build | `8` | Not a `Settings` knob — an internal constant (`src/lode/llm_provider.py`) bounding `LLMProvider.run_tool_turns`'s free-tool-choice phase. This is the mechanism that bounds a run's **total** output-token spend: `max_tokens` is per-**turn**, not per-run — a deliberate asymmetry with `qa_call_timeout_s` above, decided rather than left open (`lode-3dh1`, [stack.md](stack.md#7-multi-turn-tool-use--llmproviderrun_tool_turns-decided-lode-35nu116)) — so the worst case for one `run_tool_turns` call is `(max_tool_turns + 1) × max_tokens` output tokens — **9×** today's per-tier `max_tokens` default, not 8×, because this constant bounds only the *free* tool turns and the final forced-schema turn is spent on top. Lower this (or override the `max_tool_turns` parameter at a call site) to tighten that bound; a separate per-run token ceiling was considered and deferred, not built (`lode-csl2`). Why per-turn was chosen over decrementing, and the rejected alternative, are in the write-up linked above — deliberately not restated here. |
| HF probe timeout (`hf_probe_timeout_s`) | runtime | `5s` | Per-call timeout passed to `huggingface_hub.model_info()` by the indexing-side revision probe (`resolve_model_revision`, below). Bounds a black-holed network to this instead of the OS TCP connect timeout, which the probe used to block for before falling back to `model_revision = NULL` anyway (`lode-w5nr`). Matches `httpx`'s own default rather than the Fetch timeout below (`10s`, web content fetches) — this is a small metadata GET, not a page fetch. Bounds the probe only, **not** `fastembed`'s weights download (next section). What a float timeout actually bounds in `httpx`, with the measurement: `docs/decisions.md`, the `lode-w5nr` entry. |

The **local** models — embedder, [reranker](#retrieval-and-ranking), [faithfulness NLI](#faithfulness-gate) — all run **in-process on the ONNX runtime via `fastembed`** (no model server/daemon, **not Ollama**). The **only** remote models are the enrichment + Q&A LLMs — Anthropic by default, or an OpenAI/Azure deployment under `llm_provider = "openai"` ([LLM provider seam](stack.md#llm-provider-seam-decided-lode-568v1)). See [stack.md](stack.md).

All three load through `fastembed`'s model-management path and cache their weights at the [model cache directory](#paths--locations), `$LODE_HOME/models/` — never `fastembed`'s own `tempfile.gettempdir()` default (`lode-gmo`).

These local ids/dim were pinned and **verified to load** on the `fastembed` ONNX runtime in `lode-txh.6` (`fastembed 0.8.0`); the spike's standing proof is `tests/test_models_smoke.py` (opt-in, `LODE_SMOKE_MODELS=1`, since loading downloads the models). Two spike findings shaped the pins: (1) `fastembed` does **not** ship the originally-assumed `bge-reranker-v2-m3`, so the reranker is `BAAI/bge-reranker-base` (the loadable bge-family cross-encoder); (2) `fastembed` ships **no dedicated NLI model**, so the NLI/entailment leg repurposes that same cross-encoder via `TextCrossEncoder` — confirming the docs' "bge-reranker repurposed" option and removing the need for a separate `optimum`/`onnxruntime` loader. The model + threshold remain [open tuning knobs](decisions.md), revisited against the eval harness.

**First run needs the network — make it explicit with `lode models pull` (`lode-og3`, rebuilding the bounced `lode-6qh`).** Inference itself is fully local (ONNX/CPU, no text leaves the box) — but on a cold cache, the *first* call to the embedder or the reranker/NLI cross-encoder downloads ~500MB of ONNX weights from HuggingFace, right in the middle of whatever you were doing (a `lode work` or `lode ask` run). `lode models pull` forces that download deliberately, up front, warming the models named by your **resolved** settings (`$LODE_HOME/config.toml` honored — the pinned defaults above only if you haven't overridden them):

```bash
lode models pull
```

It warms all three local models (the embedder, and the reranker/NLI cross-encoder — one download when, as by default, `rerank_model` and `entailment_model` are the same pinned id) into the same durable [model cache directory](#paths--locations) production reads from, and prints where the weights landed. Run it once after install (or after wiping `$LODE_HOME/models/`). After that, **retrieval** (`lode ask`, related-notes) is fully offline: a query-only embed never resolves an HF revision. **Indexing** (`lode work`) is not — it still makes one read-only HuggingFace metadata call **per process** to stamp the vector provenance it records (`model_revision`, below), even against a fully warm cache. A warm cannot prepay it: the resolved value is per-embedder, in-memory state nothing persists to disk, so a later `lode work` process's own embedder re-probes regardless (`lode-r4r2`). *Within* a process it is paid once and no more — `lode work` holds one embedder for its whole run, across every queued job and every poll pass ([storage.md](storage.md#the-async-work-queue), `lode-j5r2`). Set `HF_HUB_OFFLINE=1` (next paragraph) to skip that call outright instead of paying it every run.

**Air-gapped run against an already-warm cache:** set `HF_HUB_OFFLINE=1` to force `fastembed`'s own `local_files_only` path (`fastembed/common/model_management.py`), so a load never attempts a network call even to check for updates — a cold cache under this flag fails loudly instead of silently trying to phone home. The same flag also skips the indexing-side revision probe above (`lode.embedding.resolve_model_revision`), which records `model_revision = NULL` for those vectors instead — so an air-gapped indexing run makes no metadata call at all. Why lode short-circuits in its own code when `huggingface_hub` already refuses the call under this flag, with the measurement: `docs/decisions.md`, the `lode-r4r2` entry. This is `fastembed`'s env var, not a lode-specific knob; a first-class offline/air-gapped *mode* is out of scope here (`lode-6qh`) — this is just the escape hatch once the cache is already warm. **With `HF_HUB_OFFLINE` *unset* and the network black-holed, the *probe* no longer needs this flag to stay bounded:** it is timed via the [HF probe timeout](#models) knob above (default `5s`, `lode-w5nr`). **That bound covers the probe only, so it does not make this flag optional for air-gapped use.** The weights *load* is still untimed: `fastembed`'s `download_model` tries the cache with `local_files_only=True` first, but swallows any failure of that attempt (`except Exception: pass`) and falls through to an untimed `model_info` + `list_repo_tree` + `snapshot_download`, retried with backoff (`fastembed/common/model_management.py`). A cold, partial, or unverifiable cache therefore still blocks for minutes on a black-holed network, and `HF_HUB_OFFLINE=1` — which flips `local_files_only` on for the load itself — remains the only thing that bounds it.

**Failure surfaces as a clear message, not a traceback (`lode-96t`).** `lode models pull`'s whole job is to make the first-run network dependency explicit, so its own most likely failure paths — no network reachable, `HF_HUB_OFFLINE=1` set against a cold cache, or HuggingFace rate-limiting/erroring — each exit non-zero with a distinct, actionable message instead of a raw `fastembed`/`huggingface_hub` stack trace. This is deliberately **not** a blanket `except Exception`, which would just as readily mask a real defect as a network hiccup; it catches only the specific exception types those two libraries are verified to raise for these cases (`src/lode/cli/models.py`'s `_warm`), and lets anything else propagate. A bad `config.toml` gives the same clean stderr message + exit 1 every other command gives, not a traceback.

### Model provenance: download-control and mismatch-behavior (decided, lode-crh8.1)

`lode-crh8` (model provenance: pin, verify, and regenerate what lode embeds and answers with) named two orthogonal open axes for the **embedder** — the only local model whose output persists into the DB, per the epic's own DB-invalidation scoping — and both are now decided; the full write-up, with the schema shape and the reasoning, lives in [storage.md](storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81):

- **Download-control: DETECT, not PIN.** lode does not pre-materialize the embedder's weights at a pinned HuggingFace revision (achievable via `huggingface_hub.snapshot_download(revision=...)` + `fastembed`'s `specific_model_path`, but at the cost of lode owning the whole download path — bootstrap, offline fallback, partial-download recovery). Instead it takes the cheaper **read-only probe** — the same `huggingface_hub.model_info(repo).sha` lookup `fastembed`'s own loader already performs — and records what it resolves to. **Deferred, not rejected:** see the open-decision entry in [decisions.md](decisions.md) for the PIN revisit trigger.
- **Mismatch-behavior: WARN, never REFUSE, recorded PER-VECTOR.** A live-cache/manifest disagreement surfaces as a `lode status` warning (never a hard block on embed/enrich — REFUSE was rejected because it can brick normal operation on an innocent cache eviction) and is corrected by a deliberate regeneration run (`lode-g274.7`), never automatically. "Per-vector" turned out to be a one-field addition (`model_revision` alongside the `model` field the `embeddings` LanceDB table already carries per passage), not a new schema.
- **No separate manifest artifact.** "The manifest" is the aggregate of that per-vector data (`DISTINCT (model, model_revision)` across live `embeddings` rows), not a new committed file or table — see storage.md for why that's correct under a DETECT (not PIN) design, where the resolved revision is a per-installation fact, not something the source tree can assert once for every user. The **friendly model id** stays the only git-tracked build constant here (the `Embedding model` row above), unchanged by this decision.

This unblocks `lode-g274.4` (embedder manifest + `lode status` check) and `lode-g274.7` (re-embed/regenerate capability) to be scoped without further design questions, per `lode-crh8.1`'s acceptance criteria.

### Model provenance: the enrichment LLM (decided, lode-g274.5)

`lode-crh8`'s DB-invalidation scoping also covers the **enrichment LLM** (`enrichment_llm`, [Models](#models) above) — its output (tags, entities, summaries, inferred edges) persists into `annotations`/`edges` the same way the embedder's output persists into `embeddings`, so a silent model change is the same class of problem for enrichment as for embedding. Whether a given Anthropic model ID has a **dated-snapshot form** to pin against varies per model and shifts as the lineup changes — check the authoritative catalog (Anthropic's `GET /v1/models/{id}`, or the `claude-api` skill's model table) rather than inferring it from a sibling model or an earlier generation. `enrichment_llm`'s own default has one: `claude-haiku-4-5-20251001`, a distinct pinnable identifier alongside the bare `claude-haiku-4-5`. So a pin target *does* exist for the one model whose output persists into the DB, and whether to use it is an open question, deliberately not settled here — see [decisions.md](decisions.md) (search "lode-sdjb") for the tracking entry. lode pins nothing today regardless — the mechanism is record + detect:

- **Recorded per-row, already.** `annotations.model` (alongside `prompt_ver`) is populated from `settings.enrichment_llm.model` (the `ModelTier`'s bare model/deployment string, `lode-568v.2`) at every enrichment write (`src/lode/enrich.py::_write_enrichment`) — this predates this decision; it is the same per-row provenance shape [storage.md](storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81) records on `annotations` rows. The inferred **edges** a run also writes carry no `model` column of their own (`src/lode/schema.sql`), but a run writes its annotations and its edges from one `EnrichmentResult` under one model, so the `annotations` scan below captures which model produced a given run's edges too. No schema change and no new write path were needed here.
- **No separate manifest.** As with the embedder, "the manifest" is an aggregate read over existing rows, not a new artifact: a `DISTINCT model` scan over `annotations WHERE source = 'ai'` answers "what did the enrichment store actually get built with," and detects a mix (e.g. after a mid-corpus `enrichment_llm` config change) with no additional bookkeeping. That mix is exactly what makes drift **detectable** — the acceptance bar this ticket is scoped to — and correcting it (a targeted re-enrich) is tracked separately, `lode-14jr` (not `lode-g274.7`, which built strictly to its own embedder-only scope — see [storage.md](storage.md#re-embedding-the-corpus-deliberately-lode-g2747)).
- **No attempt to pin beyond the bare ID.** `enrichment_llm`'s default (`claude-haiku-4-5`, [Models](#models) above) stays a plain build/runtime knob, recorded as-is.
- **The Q&A models are explicitly out of scope.** `qa_llm` / `qa_think_harder_llm` run at answer time and persist nothing — a change alters synthesis going forward, never the stored DB — so they keep a recorded default only ([Models](#models) above) with no provenance or pinning machinery attached, mirroring the embedder-vs-reranker/NLI split above.

**`lode status`'s hint upgraded from "mixed" to "stale-vs-current-config" (decided, lode-o9k3) — replaces, not supplements.** `lode-14jr`'s first cut of the `lode status` hint fired only on the bullet above's literal `COUNT(DISTINCT model) > 1` read — "the store has 2+ distinct recorded models." That check missed the primary intended workflow: deliberately bumping `enrichment_llm` on a corpus that was uniformly enriched under the OLD model leaves exactly **one** distinct stored model, so the 2+-distinct read stayed `False` while `lode reenrich` (`storage.md`'s [Re-enriching the corpus deliberately](storage.md#re-enriching-the-corpus-deliberately-targeted-lode-14jr) section) would in fact re-enqueue the entire corpus — `lode status` said "No action needed" while there was a corpus-wide backlog of it. The fix is to fire the hint on the same **live-head-scoped, stale-vs-current-config** condition `lode reenrich` itself already acts on (any live, non-`no_egress` head with an `'ai'` annotation whose `model` differs from `enrichment_llm` *right now*), not on the raw distinct-count. Once scoped identically, "stale" is a strict superset of the old "mixed" condition — any corpus with 2+ distinct recorded models still has at least one that disagrees with whatever `enrichment_llm` is currently configured to, so the new check fires everywhere the old one did, plus the uniform-disagreement case it missed. That makes this a straight **replacement**, not an additional hint alongside it: `src/lode/cli/status.py`'s `_enrichment_model_stale` now reads the identical query `lode reenrich` force-enqueues from (`_stale_enrichment_heads`), so "status says clean" and "reenrich has work" cannot disagree by construction — a separately-maintained approximation was exactly how they drifted apart the first time.

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

**The enrichment forced-tool-use branch needed no code change to avoid the
400** — it has never sent `thinking` at all (`lode-d1sr` never touched it),
so it already follows the same "never explicitly disable" rule and cannot hit
this 400. The separate risk named at the time — a `Kind.RUNTIME` override of
`enrichment_llm` to a thinking-capable model sharing its own (smaller,
unraised) `max_tokens=1024` between thinking and the forced tool-call JSON —
was then unreachable and tracked as a follow-up; it is now closed, see
[below](#enrichment_llm-max_tokens-headroom-for-a-thinking-capable-override-decided-lode-jgus).

### Q&A call timeout split from `llm_call_timeout_s` (decided, lode-wfyx)

> **Update (lode-7y6s):** `llm_call_timeout_s`, the knob this section is
> about, was subsequently renamed `enrich_call_timeout_s` — once this split
> landed, the general name covered only the enrichment call sites, and that
> mismatch is a real footgun (raising `llm_call_timeout_s` to fix a slow
> `lode ask` silently does nothing). The write-up below is left as written
> at decision time; read `llm_call_timeout_s` throughout it as
> `enrich_call_timeout_s`.

`lode-3dlt` (see [above](#thinking-on-the-qa-synthesis-call-decided-lode-3dlt)) raised
`qa.MAX_TOKENS` 4096 → 8192 and stopped disabling thinking on the Q&A
`messages.parse` branch, which lets Opus 5 (`qa_think_harder_llm` default) run
adaptive thinking it previously never did. That pushes wall-clock on the
think-harder path up in two ways at once, and `llm_call_timeout_s` (120s) —
the *actual* bound on this call, since the Anthropic SDK's own non-streaming
timeout guard is skipped outright whenever an explicit `timeout` is passed,
which the provider seam always does — was left unchanged. Generating up to
8192 tokens of thinking+answer on Opus 5 can plausibly exceed 120s, so the
realistic new failure mode on that path is `anthropic.APITimeoutError`, not
truncation.

**Three decisions (human, 2026-07-28):**

1. **No p95 measurement — declined deliberately, not skipped for lack of
   capability.** The ticket's original acceptance criteria required this
   decision be backed by a measured p95 latency of the think-harder call at
   `MAX_TOKENS=8192`. That bar is withdrawn by the human, knowingly: live API
   access was available at decision time, and running the benchmark was
   judged not worth the spend, not impossible. The value below is **derived,
   not measured** — cite it that way everywhere, and link back here for the
   derivation rather than restating the numbers.

2. **The knob is split, not raised in place.** `llm_call_timeout_s` used to
   reach four call sites — the Q&A synthesis call (`qa.py`) and all three
   enrichment call sites (`enrich.py`'s immediate call, batch submit, batch
   poll). Q&A is a foreground TUI call on Opus 5 with adaptive thinking and up
   to 8192 output tokens; enrichment is background work with a different
   latency profile entirely. Raising the shared knob to cover the first would
   silently loosen hang-detection on the second — a regression disguised as a
   fix, and correct regardless of what any p95 would have said. The Q&A
   synthesis call now reads its own `qa_call_timeout_s` ([Models](#models)
   above), wired only to `qa.py`'s `structured_call`; `llm_call_timeout_s` is
   untouched and still governs all three `enrich.py` sites exactly as before.

   `qa_call_timeout_s` defaults to **300s**, derived — not measured — from the
   pinned SDK's own model of how long this call should take.
   `_calculate_nonstreaming_timeout` (`anthropic` 0.117.1) prices generation at
   3600s per 128000 output tokens (~35.6 tok/s), which puts the SDK's own
   *expected* wall-clock for a full `MAX_TOKENS`=8192 response at **~230s** —
   already ~1.9x the old 120s. 300s is that ~230s expectation plus ~30%
   headroom, rounded.

   Two things ~230s is **not**, checked against the installed SDK: it is not a
   timeout the SDK would ever apply, and clearing it does not mean lode stops
   tripping before the SDK would consider the request unreasonable.
   `_calculate_nonstreaming_timeout` returns a **flat** `Timeout(600,
   connect=5.0)` and never a per-token value; the ~230s figure is an internal
   estimate it compares against its own 600s `default_time`, raising
   "Streaming is required for operations that may take longer than 10 minutes"
   only above that. So the SDK's actual refusal line is `max_tokens` > ~21333
   — the same ~21K figure `qa.MAX_TOKENS`'s note already carries — and at 8192
   the SDK finds the request entirely reasonable, with 600s the bound it would
   apply. 300s is therefore deliberately **tighter** than the SDK's own, not
   looser: it tracks the SDK's expected-duration model, not its refusal
   threshold. Moving it to 600s would match the SDK exactly, at the cost of
   doubling the worst case in the flagged risk below — a trade not taken here.

   **The derivation is against the *default* cap, and one legal config line
   invalidates it.** What `qa.py` actually sends is
   `ModelTier.resolve_max_tokens(MAX_TOKENS)` (`lode-d70n`) — a per-tier
   `max_tokens` override in the same [Models](#models) table above wins over
   the 8192 this 300s was derived from, and is validated only `gt=0`. Set
   `qa_think_harder_llm = {model = "claude-opus-5", max_tokens = 20000}` —
   still under the SDK's ~21333 refusal line — and the SDK's own expected
   wall-clock becomes ~562s against a 300s bound, i.e. `APITimeoutError`
   becomes the *expected* outcome of every think-harder ask rather than an
   exceptional one, three attempts deep. **Raise `qa_call_timeout_s` alongside
   any `max_tokens` override** — the two knobs are coupled and nothing
   enforces it.

3. **SDK retry-on-timeout left at the default (`max_retries=2`), not capped
   for this path.** Transient-blip self-healing was judged worth the
   worst-case wall clock.

**Flagged risk, surfaced at decision time and not overridden — recorded so
it isn't rediscovered as a surprise.** (2) and (3) together mean a single Q&A
call can retry twice at up to 300s each: **worst case ~900s (~15 minutes)**
on a foreground TUI call, with no intermediate feedback before an error
surfaces. `lode.progress.op_progress` — the heartbeat mechanism `lode work`
already uses for exactly this kind of "long, maybe-stuck" visibility
([Async work queue](#async-work-queue) above) — is **not** wired into the
Q&A call today.

**That worst case lands on *both* Q&A tiers, not just the one the derivation
argues from.** The reasoning above is entirely about `qa_think_harder_llm`
(Opus 5, adaptive thinking, 8192 tokens), but `qa_call_timeout_s` bounds the
single Q&A call site, so the default `qa_llm` tier moves with it. Sonnet 4.6
does *not* think when `thinking` is omitted — it gained nothing from
`lode-3dlt` — yet a wedged default `lode ask` now sits 300s instead of 120s
per attempt, ~900s instead of ~360s across retries. That is the same
"silently loosen hang-detection on a path that didn't need it" the split
refuses to do to enrichment, applied one level down; it is accepted here
rather than split a third time, but it is a cost of this decision and not an
edge case. If it bites, the lever is a per-tier bound, not a lower shared one.
If the 15-minute silent worst case proves unacceptable in
practice, the cheapest levers, in rough order, are: cap retries on the Q&A
path only (declined here), lower `qa_call_timeout_s`, or wire `op_progress`
around the Q&A call. None of those is done by this decision — it is left as
a known, accepted trade-off, not a follow-up ticket.

### enrichment_llm max_tokens headroom for a thinking-capable override (decided, lode-jgus)

`lode-3dlt` named a real but then-unreachable risk on the enrichment
forced-tool-use branch of `AnthropicProvider.structured_call` (`enrichment_llm`,
[Models](#models) above): that branch never sends `thinking` at all — a
property of the *default* tier (Haiku 4.5 predates thinking-on-by-default),
not of forced tool use itself, since a forced `tool_choice` on the
first-party Claude API does not preclude thinking (only Amazon Bedrock
requires pairing it with an explicit `disabled`). A `Kind.RUNTIME` override
of `enrichment_llm` to a thinking-capable model (Opus 5, Sonnet 5,
Fable-class) therefore runs adaptive thinking on this call too, sharing the
same `max_tokens` budget between thinking and the forced tool-call JSON — the
identical truncation hazard `lode-3dlt`'s Q&A fix exists to avoid, on a path
that predates thinking, so nobody had sized headroom for it: the
immediate/batch enrichment calls both sent a hardcoded `max_tokens = 1024`.

**Fixed the same way `qa.MAX_TOKENS` was** — a new named constant,
`enrich.MAX_TOKENS`, replaces the two identical inline `1024` literals in
`_call_haiku` and `_build_batch_request` (which must stay byte-for-byte equal
per `lode-568v.2`'s wire-equivalence bar), raised `1024 -> 2048`: headroom
for adaptive thinking to share the budget with the tool-call payload, not a
hard truncation guarantee.

**The two routes are bounded differently, and that matters for which failure
mode to expect.** Neither is bounded by the Anthropic SDK's non-streaming
timeout guard — that guard is skipped outright whenever an explicit `timeout`
is passed, and the provider seam always passes one. Beyond that they diverge:
the *immediate* call passes [`enrich_call_timeout_s`](#async-work-queue) (120s) —
the Q&A path no longer does, having split off onto `qa_call_timeout_s`
(`lode-wfyx`) — so a runaway thinking budget there tends to surface as a
timeout before it exhausts the cap. The *batch* call has no
equivalent bound — a `BatchRequest` carries no per-item timeout (the `timeout_s`
on `submit_batch`/`collect_batch` bounds only their own HTTP calls) and
generation runs server-side — so `enrich.MAX_TOKENS` is the only thing bounding
a batch item, and **truncation, not a timeout, is the realistic failure mode
there**. That is the route the raised ceiling has to actually be sufficient for.

The raised cap is headroom, not a guarantee, so `AnthropicProvider` also
*handles* running out of it on both routes. A response that spends its whole
budget inside thinking carries no `tool_use` block at all. On the immediate
route that previously escaped as a raw `StopIteration` from an unguarded
`next(...)` — the identical failure shape (and identical fix) as the
`messages.parse` branch's "no text block" guard `lode-3dlt` added; it now
raises `LLMProviderError`. On the batch route `collect_batch` already caught
it, degrading the one item to an `errored` `BatchResult` rather than failing
the whole collection — correct, but its message named nothing, so the same
raise that makes this reachable there also made it undiagnosable; it now
carries the model and `stop_reason` the immediate branch reports.

No config-load validation, model→capability predicate, or different
mitigation was chosen — same rationale `lode-3dlt` gave for the Q&A branch:
raising the cap is the simplest option, needs no new capability-detection
surface, and works on every model regardless of whether it thinks.

**A fourth option — make the budget itself a knob — was considered during
technical review and deferred at the time (`lode-d70n`), and is now decided;**
see [below](#per-tier-max_tokens-override-decided-lode-d70n).

### Per-tier `max_tokens` override (decided, lode-d70n)

The gap the section above named: `enrichment_llm` / `qa_llm` /
`qa_think_harder_llm` are all `Kind.RUNTIME`, so a user can point any of them
at a model whose output-budget needs differ substantially, but the budgets
were source constants (`qa.MAX_TOKENS` = 8192, `enrich.MAX_TOKENS` = 2048) —
they could change the model and had no way to change the budget to match.
Sharpest on the enrichment **batch** route, for the bounding reason the
section above establishes: that cap is the only thing bounding a batch item,
so truncation is the realistic failure mode there, and it was precisely the
route with no user-side escape hatch — a clean, well-named
`LLMProviderError`, and no way to act on it except editing source.

**Decided yes, on `ModelTier`, both tiers.** `ModelTier` gained an optional
third field, `max_tokens: int | None = None` (validated `> 0` when set) —
back-compat by construction, the same way the existing bare-TOML-string
coercion already gives every field a default: an unset `max_tokens` (every
`config.toml` written before this ticket, and every tier that doesn't name
it going forward) falls back to the call site's own source constant exactly
as before, so nothing changes for a config that doesn't opt in. `ModelTier`
was the natural home because it already pairs `(model, reasoning_effort)`
precisely because those co-vary per surface; `max_tokens` co-varies with the
same choice, so a `Kind.RUNTIME` override that changes the model can change
the budget alongside it in the same TOML table.

**Wiring, symmetric across both tiers and both enrichment routes.** The
fallback lives on `ModelTier` itself (`resolve_max_tokens(default)`), so the
Q&A call and both enrichment routes share one definition of "unset means the
call site's own constant" rather than each re-deriving it — the same
byte-for-byte-equivalence bar `enrich.MAX_TOKENS` is itself pinned by
(`lode-568v.2`), which a per-route copy of the rule would put at risk.
Neither `qa.MAX_TOKENS` nor `enrich.MAX_TOKENS` was removed — they stay the
documented default headroom value (see their own sections above); the tier
field only ever *overrides*, never replaces, them.

**No floor beyond "positive integer," and no cap.** A user who sets
`max_tokens` too low reproduces the same truncation symptom the source
constants already guard against (`LLMProviderError` naming the model and
`stop_reason`) — that is the explicit tradeoff an override makes, not a new
failure mode this ticket introduces. `lode config` and the TUI `ConfigScreen`
render it alongside `reasoning_effort` when set (e.g. `claude-opus-5
(effort=high, max_tokens=4096)`), via the same `knob_rows` `ModelTier`
rendering `reasoning_effort` already used — extended, not duplicated.

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

**Caveat: the check is on the value, not on the value/model pairing — but an
unsupported pairing now fails clean, not raw (decided, lode-90o7).** Effort is
not accepted by every model — it errors outright on Haiku 4.5 and Sonnet 4.5,
and `xhigh`/`max` do not exist on the 4.6 generation. All three tiers are
`Kind.RUNTIME` and two default to affected models (`enrichment_llm` = Haiku
4.5, `qa_llm` = Sonnet 4.6), so setting `reasoning_effort` on a tier whose
model does not support the level you ask for still reaches the API and gets
rejected. A model→capability predicate to predict this ahead of time was
deliberately rejected again as a moving target (lode-3dlt's option 1,
reaffirmed by lode-90o7) — **set `reasoning_effort` only alongside a model
that supports it** is still the operative guidance. What changed: a request
the API *rejects* now surfaces as `LLMProviderError`, status code and request
id preserved, on both providers — `AnthropicProvider` gained that wrap at its
three request-submitting call sites, and `OpenAIProvider` already had it. (A
*timeout* is not a rejected request and is not covered: `anthropic`'s
non-status errors still surface raw — see `qa.MAX_TOKENS`.) `OpenAIProvider`
also gained the pre-flight *value* check `AnthropicProvider` already had; its
legal set is `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`, derived
from the installed SDK's own `Reasoning.effort` Literal rather than
hand-typed.

**`reasoning_effort` value is also validated at config load (decided, lode-tvps).**
lode-90o7 left the effort *value* check at the provider seam only — on the
first API call, not when `config.toml` is parsed — so a plain typo
(`reasoning_effort = "LOW"`) started clean but failed at first use; on the
enrichment path that failure was then classified as *transient* by
`worker.run_one`, charging an attempt, backing off, and dead-lettering the
job after `retry_max_attempts` rather than refusing to start. A `Settings`
`@model_validator(mode="after")` now checks **every `ModelTier` knob's**
`reasoning_effort` against the legal set for the configured `llm_provider` at
construction time, naming the offending tier and that set on failure. Since
every CLI entry point and the TUI resolve settings through `load_settings()`,
that surfaces as a one-line `invalid config file …` on stderr and exit 1
before any work starts. Legality is always relative to the *configured*
provider: a value legal only under the *other* one (e.g. `minimal`,
OpenAI-only) is rejected exactly like an outright typo. The provider-seam
value checks stay in place unchanged — they remain the guard for programmatic
callers that construct a provider directly, bypassing `Settings`. Still
deliberately unpredicted: the value/model *pairing* (lode-3dlt option 1,
reaffirmed by lode-90o7) — the load-time check reads the effort value against
the provider's legal set, never against what the tier's specific `model`
supports.

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
