# lode — Open decisions (deferred, not forgotten)

*(§9)* Decisions deliberately left open, with the current leaning where there is one. Revisit each
when the build reaches the feature that forces it. The tunable parameters several of these reference
are catalogued in [configuration.md](configuration.md).

**This file is a dated log, not a living doc — an entry is never edited in place when later work
makes it stale.** An entry records a decision (or a finding) as it stood at the time; when something
later invalidates part of it, the correction is a **new entry, or a marker appended to the existing
one**, never a silent rewrite. Every such marker opens with one fixed lead-in —
`**Update (<id>[, <date>])`, asterisks included, the id never wrapped onto the next line — so
`grep -n '\*\*Update (' docs/decisions.md` finds every stale claim in this file. What follows the
lead-in is free prose; only the lead-in is load-bearing. Where no ticket owns the correction, the id
slot takes whatever does (a bare date, or `maintainer decision`). The rule binds markers that point
*backwards* at another entry; an entry narrating its own history in one pass ("settled X, then
re-decided Y") is prose, not a marker, and is left alone. Contrast this with an
*operational* doc like [`.claude/skills/land/SKILL.md`](../.claude/skills/land/SKILL.md), which
describes current behavior and so *is* corrected in place — there is no history to preserve there,
while erasing it here would lose the record of what was believed, and when. The marker *shape* is
gated by [`tests/test_decisions_supersession_markers.py`](../tests/test_decisions_supersession_markers.py);
what that gate cannot catch is recorded in its module docstring (lode-nlk6).

- **External refresh: on-access revalidation vs. scheduled background refresh.** Leaning
  **on-access with a short TTL cache** for a single instance with finite API quota — but it's
  really a per-source judgment (a closed ticket changes rarely; an active PR changes hourly).
  Decide per connector when building it. ([externals.md](externals.md#the-broken-assumption-external-staleness-is-not-topological))
  **Decided-for-web (`lode-w0h.6`):** a **scheduled TTL sweep**, not a true on-access hook — every
  synchronous read path in this codebase is deliberately network-free, so an on-access hook would
  have to add a blocking fetch to interactive Q&A/retrieval, which this ticket's scope (staleness
  detection + scheduling only, no second fetch path) does not take on. `lode.reconcile`'s new
  `refresh_stale` step re-enqueues a `refresh` job for any external whose head snapshot is older
  than `refresh_ttl_s` (default 1h, [configuration.md](configuration.md)), riding the reconciliation
  scan's existing periodic cadence (worker startup + every `--loop`/`--wait` tick) rather than a new
  mechanism. Tombstoned externals are excluded (mirrors `embed_gap`'s own tombstone exclusion — a
  permanently-failed source is not blindly re-fetched forever). Full write-up:
  [externals.md](externals.md#refresh-policy-ttl-based-revalidation-decided-for-web-lode-w0h6).
  **Still open** for any future non-web connector — decide per connector when building it, as
  originally noted above; nothing here presumes the same TTL-sweep answer is right for, say, a
  webhook-capable source.
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
  tune against it. **Determinism — settled (lode-5y8.1):** the scorer
  (`lode.eval.harness.score_golden_set`) is reproducible for a fixed corpus because it injects two
  seams. Retrieval is model-free in the lexical leg (FTS5/BM25) and deterministic in the dense leg
  (local embeddings), so **recall@k is corpus-deterministic** — and it is the leg that scores real
  seed prose even with a stubbed embedder. The Q&A LLM call is *not* deterministic, so the
  faithfulness/abstention legs are sourced through an injected **answerer** seam (the same mock seam
  `cited_answer.ask` / `qa.answer_question` already expose via their `client` parameter): a fixed
  answerer over a fixed corpus yields a fixed score. Tests inject deterministic stubs and never hit
  the network; production wires the real embedder + a real-client `ask`. **Command + CI wiring —
  settled (lode-5y8.2), then re-settled (Shape A, lode-5y8.5):** the original wiring shipped eval as
  a top-level `lode eval` command (`src/lode/cli.py`) that ran the scorer against a fresh ephemeral
  store and printed the three metrics. **Re-decided (Shape A, supersedes lode-5y8.2):** eval is a
  maintainer/CI **integration test in a live-like state**, not an end-user feature, so it is **no
  longer a shipped CLI command**. The `lode eval` subcommand is removed from `src/lode/cli.py` (and
  from the E10 shipped surface, lode-y42); the live-wiring entry point moves to a `tests/`
  integration test (`tests/test_eval_live.py`) that `nox -s eval` runs. Rationale: Python extras only
  gate *dependencies* — `lode[dev]` decides whether fastembed/anthropic/test deps are installed, not
  which first-party modules ship — so a `lode eval` command would land in the base wheel for every
  end user regardless of extras. Relocating the live entry point into `tests/` keeps it out of *every*
  shipped wheel while preserving its value as a real-seam integration check; the `dev` extra carries
  the deps needed to run it. It still scores against a fresh ephemeral store (in-memory SQLite + a
  throwaway LanceDB dir — never the user's notes) over the *real* seams — the local ONNX embedder
  (`FastEmbedEmbedder`) and a real-client answerer (`cited_answer.ask`) — so its Q&A leg needs
  `ANTHROPIC_API_KEY` and the network, and stays **out** of the offline test gate: the noxfile keeps
  `nox.options.sessions = ["fix", "tests"]` so a bare `nox` and `nox -s tests` stay offline + keyless,
  and the `nox -s eval` session is the explicit, credential-gated CI-style check — it `skip`s itself
  when `ANTHROPIC_API_KEY` is absent rather than failing or hitting the network. **Exclusion mechanism
  — re-settled (lode-b4w.7, 2026-07-10):** credential presence alone was, for a while, the *only* thing
  keeping the live pass out of `nox -s tests` (a bare `pytest.skip` when `ANTHROPIC_API_KEY` was
  absent). `nox -s tests` applies no marker filter by design (lode-pql, so nothing slow is ever skipped
  before trunk) and `@pytest.mark.slow` alone doesn't gate it — so whenever `ANTHROPIC_API_KEY` was
  ambient in the shell (the normal case in agent environments, not just CI), `nox -s tests` silently
  ran the live, ~273s, API-billed Q&A pass on every invocation, breaking the offline/deterministic gate
  split this entry establishes. The test's skip is now gated on an explicit opt-in env var,
  `LODE_RUN_LIVE_EVAL=1`, checked *before* the credential check; `nox -s eval` is the only session that
  sets it, so `nox -s tests` and `nox -s unit` skip the test unconditionally regardless of ambient
  credentials, and `nox -s eval` still self-skips without a key once opted in. The deterministic
  offline scorer tests (`tests/test_eval_*.py`, stubbed seams) are unchanged, and
  `lode.eval.harness.score_golden_set` stays a library function shared by both the offline stub tests
  and the live integration test. Knock-on: the Phase-A exit gate (lode-6w1 / lode-6w1.1) wording
  moves from "`lode eval` runs green" to "the `nox -s eval` integration test runs green." **Mechanical
  enforcement — added (lode-85q):** the offline/keyless split this entry establishes is no longer
  just session wiring — `tests/conftest.py`'s autouse `_block_unmocked_network_and_llm_access` fixture
  fails any test, loudly, that reaches a real `anthropic.Anthropic()`/`AsyncAnthropic()` construction
  or non-loopback socket egress, with `@pytest.mark.network` as the single explicit, greppable escape
  hatch. One residual hole is deliberate: `@pytest.mark.slow` additionally relaxes *only* the socket
  guard (never the client-construction guard), so the cold-cache `FastEmbedCrossEncoder` reranker's
  one-time HuggingFace Hub download can proceed without weakening the Anthropic-client guard, which
  still covers every `slow` test. **Pass bar,
  metric weighting, and golden-set curation — settled (lode-7lp).** The harness previously shipped
  with no quality floor (`tests/test_eval_live.py` asserted only that each metric fell in `[0, 1]`, so
  even 0% recall passed); a live baseline is now recorded and enforced. **Weighting: independent
  per-metric floors, not a combined score.** Recall@k, faithfulness, and abstention measure distinct
  failure modes (retrieval missed the note vs. the answer cited the wrong thing vs. the system
  answered/abstained wrongly) and a single blended score would let a collapse in one metric hide
  behind headroom in the other two — exactly the silent-regression risk this ticket exists to close.
  Each of the three metrics in `tests/test_eval_live.py` must independently clear its own floor for
  `nox -s eval` to pass. **Baseline (recorded 2026-07-02, two independent live runs against the
  committed golden fixture, `k=20`):** recall@20 = 1.000, faithfulness/citation accuracy = 1.000,
  abstention correctness = 1.000 — all stable across both runs. **Floors: 0.95 per metric**, a
  one-item margin below the perfect baseline (24/25 = 0.960 clears it, 23/25 = 0.920 does not, on the
  25-item answerable population; the 33-item abstention population has more headroom still) to absorb
  the live Q&A leg's run-to-run sampling variance without masking a real multi-item regression. Floors
  are recorded as named constants (`RECALL_FLOOR`, `FAITHFULNESS_FLOOR`, `ABSTENTION_FLOOR`) next to
  the assertions in `tests/test_eval_live.py`, not only here, so a future re-baseline finds them
  in-context. **Golden-set curation policy:** the set is maintainer-curated, not auto-generated or
  crowd-sourced — every item is hand-authored against the committed seed corpus (`src/lode/eval/`),
  with each citation's verbatim span mechanically checked against the cited note's body
  (`tests/test_eval_golden.py`) so a stale or fabricated quote fails loudly rather than drifting. It
  grows the same way: a new question is added only alongside the seed-corpus note(s) it targets (or as
  a new out-of-corpus item for abstention coverage), and a re-baseline (rerun both `nox -s eval` and
  this entry) follows any change that could move the recorded metrics — a new item, a seed-corpus edit,
  or a retrieval/answerer knob change. The set intentionally stays small (~20–50 items, current: 25
  answerable + 8 abstain) so it remains fully hand-auditable; it is a regression harness for the
  tuning knobs (rerank, entailment threshold, chunk size), not a statistically powered benchmark.
- **Rerank model + threshold tuning.** The rerank *stage* ships in v1 ([retrieval.md](retrieval.md))
  with a default local cross-encoder behind a toggle; choosing/tuning the model and cutoffs — and
  A/B'ing rerank vs none — waits until there's a real corpus to evaluate against. Don't tune
  pre-data.
- **`$LODE_HOME` on-disk layout (settled); migration moot — no install base.** The on-disk layout
  is a single root, `$LODE_HOME` (default `~/.lode`), holding the DB, lock, `lancedb/`, `logs/`, and
  optional `config.toml` ([configuration.md](configuration.md#paths--locations)) — replacing the old
  XDG-style `~/.local/share/lode/lode.db` (`$LODE_DB`) binding (lode-qd9). The data-migration question
  lode-qd9 raised (auto-move-if-present vs document a manual move for existing
  `~/.local/share/lode` data) is **resolved as not-applicable: there is no install base, so there is
  no on-disk data to migrate** — qd9's "`$LODE_HOME` for new installs, no auto-move" is the complete
  fix, and the discovered-from migration ticket (lode-qfp) is closed as moot. If a deployed install
  base ever predates a path change again, re-open the move-vs-document question then.
- **`lode --debug` coupled to DEBUG log level (accepted tradeoff, split deferred).** The top-level
  `lode --debug` flag (lode-1i8.3) ties log **verbosity** to diagnostic **feature-enablement**:
  passing it forces the root logger to `DEBUG` for that invocation, and every DEBUG-gated diagnostic
  (e.g. the TUI's event-loop-lag `latency_probe`, lode-0wj.2) checks that same level to decide
  whether to run — there is no separate "enable this diagnostic" switch
  ([configuration.md](configuration.md#paths--locations) `lode --debug` row). **Accepted tradeoff:**
  one flag, one concept — simplest thing that works, at the cost of always paying DEBUG-level log
  volume to get the diagnostics, and vice versa. **Split trigger:** revisit if a future debug feature
  needs enabling *without* DEBUG-level log spam (a diagnostic cheap enough to always want on, but
  DEBUG logging is too noisy to also flip on), or the reverse (DEBUG logging wanted without enabling
  every diagnostic) — then decouple verbosity from feature-enablement into two flags/knobs. Until
  one of those forces it, the coupling stands.
- **Landing loop — architecture + mechanics settled; two future upgrades noted.** The whole landing
  loop is decided in
  [agents-workflow.md](agents-workflow.md#the-landing-loop--build-review-land) — all landing
  through one `/land`, split technical/semantic review, the `ready-for-land` **label**, minimal
  landing context (head SHA + summary), `land/<ticket-id>` branches, and the v1 single-lander lock (a
  local "skip if running" guard + the one-machine convention). Deferred, *not* blocking v1: (1) a
  **distributed remote-lock ref** (`refs/locks/land`, owner + timestamp for stale-break) to replace
  the v1 guard once true concurrent multi-machine landing is wanted — the seam toward real CI; (2) a
  **stale-escalation sweep** — **surfacing** (not GC'ing) a `land-escalated` branch that has sat
  unresolved unusually long, so a long-abandoned decision is called out distinctly rather than
  blending into the routine digest. This is deliberately a refinement of surfacing, not a deletion
  mechanism: `/sweep` (lode-nps.1, [agents-workflow.md](agents-workflow.md#running-the-loop-family-unattended--epic-audit-sweep))
  already surfaces every open `land-escalated` item every pass regardless of age; a `land-escalated`
  branch is otherwise never touched by an automated sweep — only the human-driven resolution
  exits ([agents-workflow.md](agents-workflow.md#the-lander--land-drained-by-a-self-paced-loop))
  remove the label and let the branch go.
- **`bd dolt push` retry-on-reject: a backoff wrapper, not a Dolt server-mode migration (lode-83d).**
  lode-nps.3 validated that `bd dolt push` is fast-forward-only + atomically CAS-protected on the
  branch ref (a losing concurrent writer is *rejected*, never silently dropped) but surfaced two
  gaps: no call site retried a rejection, and lode's **embedded** (in-process Dolt engine) mode is
  documented by beads itself as single-writer-via-file-lock, the wrong mode for `/code`'s
  multi-producer fan-out, whose failure mode is a hard "database is locked" error with no built-in
  retry. **Decision: fix both with one mechanism — a shared backoff-and-retry wrapper
  (`scripts/bd-dolt-push.sh`), not a switch to Dolt server mode.** Every literal `bd dolt push` call
  site across the skills (`.claude/agents/coding.md`, `.claude/agents/code-reviewer.md`,
  `.claude/skills/land/SKILL.md`, `.claude/skills/epic-audit/SKILL.md`) now calls the wrapper
  instead: on a non-zero exit it runs `bd dolt pull` (folds in the winner's commit so a rejected push
  has a shot at fast-forwarding on retry) and retries with exponential backoff + jitter (default 5
  attempts, ~2s/4s/8s/16s base delays, `BD_DOLT_PUSH_MAX_ATTEMPTS` / `BD_DOLT_PUSH_BASE_DELAY`
  override the defaults), surfacing the final failure's exit code if every attempt is exhausted.
  **Follow-up (lode-bpl): that enumeration was itself prefix-blind** — it greped
  `"rtk bd dolt push"`, missing any call site written without the `rtk` prefix. A prefix-agnostic
  re-audit found and wrapped two more unattended-loop call sites (`land/SKILL.md`'s exit-(a)
  re-entry step, `sweep/SKILL.md`'s publish step) and confirmed three deliberate exemptions —
  `challenge/SKILL.md` (human-invoked/interactive, a failed push is observed), `.beads/README.md` and
  `AGENTS.md` (generic beads-generated quick-reference prose, not automated call sites). See the
  "Concurrent `bd dolt push` under fan-out" section in [agents-workflow.md](agents-workflow.md) for
  the full inventory.
  **Why not switch to Dolt server mode:** it's the operationally heavier fix — every contributor
  machine would need a running `dolt sql-server` process, port/credential config, and a lifecycle
  story (start on session begin, survive across worktrees, restart on crash) before any producer
  could write bd state at all; a single-repo, single-machine, short-lived-lock workload doesn't
  warrant that infrastructure. Embedded mode's lock window is one bd operation (milliseconds to low
  seconds), well inside the wrapper's backoff schedule — a few seconds of retry absorbs contention
  from `/code`'s N-producer fan-out without a new daemon to run, monitor, or fail. **Revisit if:**
  lock contention or push rejections become a *frequent* rather than occasional event (i.e. the
  wrapper's default 5-attempt budget starts exhausting under normal fan-out width, not just an
  unlucky race), or lode's contributor base grows to where a shared always-on Dolt server earns its
  keep for reasons beyond this ticket's concurrency concern.
- **`/land` bounce-lineage cap — deferred, not built (lode-nps).** A `land-review` bounce supersedes
  the original ticket into a fresh rebuild; if that rebuild is bounced again for the same reason,
  nothing today stops an unbounded chain of rebuild tickets — a real internal livelock needing no
  external churn. The mechanism sketched to close it is sound and cheap (a `bounce_depth` metadata
  counter carried across each supersede, escalating to `land-escalated` past a cap), but no real
  bounce chain has ever been *observed* — and `/sweep`
  ([agents-workflow.md](agents-workflow.md#running-the-loop-family-unattended--epic-audit-sweep)) is
  already the detector for one: it would surface a stuck lineage the moment it escalates, with no cap
  needed to make that visible. **Revisit if:** a real bounce chain is actually observed running past
  one or two rebuilds without landing.
- **`/code` rebase-attempt cap — deferred (YAGNI).** A parallel safeguard considered for
  `needs-rebase` starvation under perpetual `trunk` churn (a `rebase_attempts` counter, escalating
  after N attempts). The failure mode is churn-only — a finite backlog of rebases quiesces on its
  own — and a genuine rebase *conflict* already escapes to `land-escalated` today, so there is no
  observed gap this cap would close. **Revisit if:** perpetual-churn starvation (a ticket rebasing
  repeatedly without ever landing, absent any real conflict) is actually seen in practice.
- **Epic auto-close + confirming re-audit — rejected, not merely deferred (lode-nps).** `/epic-audit`
  never closes an epic itself and, after filing gap children, does not re-arm itself — closing an
  `epic-audited` + all-children-closed epic stays a manual act (`/epic-audit <id>` to re-verify, or a
  direct `bd close`). A `/challenge` pass considered auto-closing an epic once every filed gap child had
  landed and a confirming re-audit came back clean. **Rejected:** epic closure is a human
  **capability judgment** — "did the delivered set actually satisfy what this epic promised" — not a
  mechanical check a re-audit can safely stand in for. Every gap child already passes code-review +
  land-review + the land gate on its own merits, so an automatic confirming re-audit would only redo
  judgment a human should own, to save one rare click; the downside of a false-positive auto-close
  (an epic quietly marked done when it wasn't) outweighs that saving. `/sweep` now surfaces a
  closable epic (`epic-ready-to-close`) so the human is prompted rather than left to notice on their
  own — that is the whole fix; manual `/epic-audit <id>` remains available to re-verify on demand.
- **Loop poll / quiescence cost — deferred.** `/code`, `/land`, `/epic-audit`, and `/sweep` are all
  designed to poll forever on a fixed interval (`/loop 5m /land`, `/loop 30m /sweep`, …); a no-op tick
  still spends a model turn even when every queue is empty. Fixed-interval polling is accepted as-is
  for now. **Revisit if:** no-op poll cost is actually *observed* to matter — then consider adaptive
  backoff or a quiescence stop ("N consecutive empty passes → stop the loop") — rather than
  pre-optimizing against a cost that hasn't been shown to bite.
- **Loop topology — landing-side loops are a one-machine invariant, stated explicitly (lode-nps).**
  `/land`, `/epic-audit`, and `/sweep` are all expected to run on **one** machine. This was previously
  an implicit convention riding on `/land`'s single-lander lock
  ([agents-workflow.md](agents-workflow.md#mechanics-decided)); with `/epic-audit` and `/sweep` now
  also writing bd state as their own loop legs, the same one-machine expectation has to cover all
  three explicitly — the lock itself only ever guarded overlapping `/land` ticks, and says nothing
  about where the other two run. **`/code` producers are the one leg that MAY fan out across
  machines**, because they write disjoint issue rows and push their own branches rather than touching
  any landing-side shared state — see the concurrent-`bd dolt push` validation above. Distributed
  cross-machine landing (the `refs/locks/land` ref, above) stays separately deferred; this invariant
  does not un-defer it, it just states plainly what was always assumed.
- **`/code` invocation topology — concurrent invocations documented as unsupported, not locked
  (lode-pzr).** Surfaced by the lode-t83 technical reviewer: `/code`'s step-0 `needs-rebase` sweep and
  step-1 stranded-`ready-for-code-review` sweep (lode-t83) both select a ticket by **label**, and that
  label is only cleared at the very *end* of the agent the sweep dispatches — so a *second*, concurrent
  `/code` invocation's sweep can select the same ticket while the first invocation's agent is still
  live, and dispatch a second agent onto the same builder worktree via `git -C`. This is distinct from
  producer-level fan-out (the previous entry): **within** one invocation each producer/reviewer works a
  ticket that invocation itself resolved, so they never collide; the race is specifically two *separate*
  `/code` invocations each running their own start-of-run sweep. Today's consequence is benign (the
  loser's push non-fast-forward-rejects; clean-tree assertions guard the worktree), which is why
  lode-t83 didn't treat it as a regression to fix inline — but "benign today" is an observation about
  current code paths, not an invariant. **Decision: document it as unsupported (option (a) from
  lode-pzr's design), not build a claim mechanism (option (b)).** Rationale: `/code` fan-out already
  parallelizes within one invocation across the whole ready frontier, so a second concurrent invocation
  buys negligible extra parallelism for the cost of a claim-before-dispatch mechanism on *both* sweeps.
  Mirrors how the entry above states `/land`/`/epic-audit`/`/sweep`'s one-machine invariant by
  documentation rather than a distributed lock — same shape of tradeoff, cheaper fix for a race with no
  observed harmful failure. Recorded in
  [agents-workflow.md](agents-workflow.md#the-coding-loop--code--coding--code-reviewer) and
  [`.claude/skills/code/SKILL.md`](../.claude/skills/code/SKILL.md): run only **one** `/code` invocation
  at a time against a given repo; get more parallelism by passing more IDs (or bare `/code`) to that
  same invocation. **Revisit if:** concurrent `/code` invocations become an actual desired mode (e.g.
  two humans/agents each wanting to drive their own fan-out simultaneously) — then a per-ticket claim
  stamp (label swap or metadata, applied by the sweep *before* dispatching, on both step 0 and step 1)
  is the right mechanism, at per-ticket granularity — explicitly **not** a lockfile like `/land`'s,
  since the contended resource here is a per-ticket worktree, not a single shared `trunk` write path.
- **Review architecture — the reviewer checks the branch out into its own worktree; the `git -C
  <builder-worktree>` architecture is retired (lode-k5e, lode-8k3).** Both `code-reviewer` (its
  technical review) and `coding`'s rebase-pickup cycle used to stay in their own launch worktree and
  drive the *builder's* existing worktree in place via `git -C <path>`, reasoning that `EnterWorktree`
  into a path-entered worktree was refused for a worktree-isolated subagent. **That premise was
  falsified by a direct probe (2026-07-09):** `EnterWorktree(path=…)` reports success, but a separate
  isolation guard still hard-pins `Bash`/`Edit`/`Write` to the agent's own launch worktree regardless —
  so `git -C` was never a workaround for a nonexistent constraint, the constraint (no writing outside
  the launch worktree) is real, and it was never possible to `EnterWorktree` around it either way.
  Worse, `git -C` alone can only *read* the builder's worktree; every `code-reviewer` fix had to go
  through a `bash` single-match-replacement workaround (`Edit`/`Write` can't reach `$WT`), and a launch
  worktree freshly branched off `trunk` HEAD has an *empty* diff against the builder's actual branch —
  so `/code-review`/`/simplify` (both cwd-relative, no working-directory argument — they always review
  the current tree and cannot be pointed at another worktree's directory — even though a base/target
  rev-range or file/branch IS accepted, which is exactly why the explicit `trunk...HEAD` below works)
  silently reviewed **nothing**
  (lode-k5e), a false-green that six of six fan-out reviewers missed on one observed day. Separately,
  `coding`'s rebase-pickup cycle had *no* mechanism at all for writing a conflict resolution once it
  hit the same guard, so it escalated every conflict to a human — including trivially mechanical ones
  (lode-8k3) — undermining `/code`'s "no manual nudge needed" claim.

  **Decision: fetch `origin/land/<id>` and check it out into the agent's *own* launch worktree**
  (originally `git fetch origin land/<id> trunk && git checkout -B land/<id> FETCH_HEAD`, or `--detach`
  if that branch name happened to be checked out elsewhere — see the lode-em6v update below for why the
  bare name and the detach fallback were retired), instead of reaching into the builder's worktree at
  all. Builders themselves never contend for the name: they work on `worktree-agent-<hash>` branches and
  only *push* `land/<id>` as a remote ref. Once checked out locally, `Edit`/`Write`/`nox` all work
  natively — no guard to work around — and `/code-review high --fix trunk...HEAD` / `/simplify` see the
  real diff (the explicit `trunk...HEAD` base matters: `checkout -B` leaves no upstream, and
  `/code-review`'s own fallback base is `main...HEAD`, the wrong default branch for this repo).
  `coding`'s rebase pickup gets the identical treatment and, as a consequence, gains a real capability: a
  **mechanical** conflict (both sides add independent, non-overlapping content) is now resolved directly
  with `Edit` and the rebase continues; a **genuine disagreement** (the two sides changed the same
  content incompatibly) still aborts and escalates — that remains a deliberate judgment boundary, not a
  tool-guard consequence, and it should stay that way even though the tooling limitation that used to
  force *every* conflict down that path is gone.

  **Update (lode-em6v): the "no `land/<id>` branch is ever checked out elsewhere" assumption above held
  only for a single isolated cycle, not in steady state.** Neither `code-reviewer` nor `coding`'s rebase
  pickup ever removed its own launch worktree when it finished, so a *second* review/rebase cycle on the
  same ticket (or one that ran later, after the first cycle's worktree was simply left on disk) found
  `land/<id>` already checked out and fell back to `git checkout --detach FETCH_HEAD`. A detached
  worktree owns no branch ref, so back when `/land`'s worktree GC was still branch-name-keyed (walking
  `git worktree list --porcelain`'s `branch refs/heads/...` lines, or enumerating branch refs directly)
  it structurally could not see it — at the time, the only net that ever caught it was a separate
  by-SHA/by-detached-state sweep, added in lode-mxeu specifically because the name-keyed sweep couldn't
  see a worktree with no branch. (lode-jiyk has since **unified** those *two* worktree sweeps — and only
  those two — into a single loop keyed on HEAD-sha ancestry, which is why both are described here in the
  past tense.) Worse, the leak was self-compounding: every leaked worktree was exactly the "already
  checked out elsewhere" state that forced the *next* cycle onto the same detaching path.

  The actual fix is on the agent side, not `/land`'s: `code-reviewer` and `coding`'s rebase pickup now
  check `land/<id>` out under a local name suffixed with their own launch worktree's directory name
  (e.g. `land/<id>--agent-<hash>`), which is unique by construction, so the
  collision — and with it the detaching fallback — can no longer arise. The suffixed name still starts
  with `land/`, but `/land`'s worktree GC (lode-jiyk) doesn't match on that prefix, or on any branch
  name at all, any more: it reclaims any worktree under `.claude/worktrees/` that is **unlocked** and
  whose **HEAD commit** is already an ancestor of `trunk` (`git merge-base --is-ancestor`), so this
  worktree is reclaimed exactly as it always was, once merged into trunk. That name-independence is
  scoped to the worktree loop only — `/land`'s dangling-**ref** backstops still match `land/*` and
  `worktree-agent-*` by name (they must: `refs/heads/*` is shared with human branches, so a name-blind
  "delete any merged local ref" would eat them too). One `/land` sweep did have to follow the rename,
  though: the dangling-**ref** sweep over `land/*` keys on an **exact** name match against `git
  ls-remote`'s listing to decide "remote gone ⇒ stale", and a suffixed `land/<id>--agent-<hash>` can
  never equal origin's `land/<id>` — left alone, its keep-the-in-flight-ref arm becomes dead code and
  the sweep silently degrades into "delete every `land/*` ref not currently checked out", taking an
  in-flight ticket's unpushed commits with it the moment its worktree goes away by any route. It now
  strips the suffix (`${BR%%--*}`, safe because a bd id never contains `--`) before comparing, restoring
  the original semantics for both the suffixed and the bare shape. A detached worktree is still caught,
  by that same HEAD-sha-ancestry test — as defense against a crash mid-cycle, not steady-state
  operation, since the rename means the detach fallback no longer fires at all. The mechanism of record
  for all of the above is [`.claude/skills/land/SKILL.md`](../.claude/skills/land/SKILL.md#4-land-the-survivors)
  §4 — check this prose against it, not the other way round.

  **Accepted costs:** (1) the reviewer's launch worktree has no venv, so `./scripts/python-init.sh`
  rebuilds one every review — a few extra seconds per review, not a correctness issue. (2)
  `metadata.review_worktree` is now vestigial for the reviewer and the rebase pickup — neither opens it
  — but it is **not** removed from the hand-off: `/land`'s worktree GC still keys off it to reclaim the
  builder's local worktree after a clean land, so the builder keeps recording it. **Update (lode-h1vn)
  — superseded, below: that GC loop is deleted, so `review_worktree` is now vestigial outright —
  recorded by the builder, read by nobody. The builder's worktree is still reclaimed after a clean
  land, but by the backstop sweep, which discovers it from `git worktree list` instead.** `/code`'s
  step-1 stranded-review guard is re-keyed onto `metadata.review_head` instead (the field the reviewer
  actually consumes). (3) Uncommitted work left in the builder's worktree is now structurally invisible
  to the reviewer (it never opens that worktree at all) — accepted because the builder's hand-off
  contract already requires a clean tree before recording `review_head` (lode-tpt); if that contract is
  ever violated, this architecture can no longer even detect it as a "dirty builder worktree" the way
  the old `git -C` architecture nominally could (though in practice the old detection existed only in
  prose, not in a proven catch). (4) **Which `/code-review` resolves inside a subagent is now recorded
  and confirmed (2026-07-09), closing lode-k5e's acceptance criterion that was never written down**:
  it is the built-in first-party skill, not the marketplace `claude-plugins-official/code-review`
  plugin of the same name. The evidence is behavioral, not just nominal — `/code-review high --fix
  trunk...HEAD` wrote a fix directly to the reviewer's own working tree, which the marketplace plugin
  cannot do under any invocation: its `commands/code-review.md` scopes `allowed-tools` entirely to `gh`
  subcommands (`gh pr view`, `gh pr diff`, `gh pr comment`, …) and its whole flow is "review a GitHub
  PR, then comment back on it via `gh pr comment`" — there is no path from that command to a local
  working-tree edit, and a `land/<id>` branch has no PR to comment on in the first place. If the
  marketplace command ever shadows the built-in in some environment, this review step would silently
  no-op again in a new way (the same failure shape as the `git -C` false-green above).

  **Update (lode-axyq, 2026-07-20) — (4) is not wrong, it is STALE: an upstream dependency changed
  underneath it.** `/code-review` is now **USER-GATED** — a human keystroke can invoke it anywhere, but
  *no model context, main session or subagent, can invoke it at all*, regardless of cwd or which
  `/code-review` would resolve. Two independent confirmations: a direct keystroke test by the
  maintainer (it runs when typed), and its absence from the model-invocable skill listing handed to
  both subagents and the main session (so there is no `Skill`-tool handle and nothing resolves).

  **This does not impugn (4)'s 2026-07-09 behavioral test.** Both records are literally true, because
  the capability was removed between them, deliberately, by upstream. The Claude Code 2.1.215
  changelog entry reads verbatim:

  > Claude no longer runs the `/verify` and `/code-review` skills on its own; invoke them with
  > `/verify` or `/code-review` when you want them

  The installed version is exactly 2.1.215 (`claude --version`), and 2.1.215 is the newest published
  release, so nothing has restored it since. On 2026-07-09 the model *could* invoke `/code-review` and
  (4) recorded that faithfully; the upgrade to 2.1.215 removed it, which is why the breakage surfaced
  abruptly rather than having always been there. So the pipeline was never built on a false premise —
  this is dependency drift, not a design defect, and the earlier claim that step 1 "has been inert for
  every branch this pipeline has reviewed" is retracted: the inert window is only branches reviewed
  after the 2.1.215 upgrade.

  **This claim is therefore version-bound, and that is the one thing a future reader must not lose.**
  Every other file states it flatly ("unreachable from any model context") and points here; *here* is
  where the version pin lives. **WATCH ITEM [RETIRED 2026-07-21 — see the maintainer-decision note at
  the end of this thread; do NOT re-file a watch ticket for this]:** because this was a deliberate
  upstream product decision rather than a stable constraint, a later release could revise it or add an
  opt-in — re-check the changelog for an entry restoring model invocation before assuming this still
  holds. If it is restored, the correct fix collapses to simply un-breaking the old step 1 rather than
  keeping the hand-reasoned pass. **Tracked as lode-ebhk** so this does not live only as prose here — a
  watch item with no owner goes stale exactly the way (4) above did. Likely rationale for the removal (LIKELY — not stated in the changelog):
  `/code-review` and `/verify` are both expensive multi-agent skills and the `ultra` variant is
  separately billed, so gating them behind a human keystroke stops an autonomous agent firing them in
  a loop at unbounded cost.

  **Update (lode-ebhk, 2026-07-20) — re-checked; NOT restored, watch item re-affirmed.** Installed
  version is now **2.1.216** (`claude --version`), one release past the 2.1.215 regression recorded
  above, and 2.1.216 is the newest published release — confirmed against the local changelog cache
  (`~/.claude/cache/changelog.md`), whose newest heading is `## 2.1.216` with nothing newer beneath it.
  The 2.1.216 entry contains exactly one line touching `/code-review`: "Improved `/code-review ultra`
  empty-diff message to name the exact base ref and suggest passing an explicit base" — a cosmetic
  error-message improvement, not a restoration of model invocation. No entry in 2.1.216, nor anywhere
  else in the changelog, reverses or qualifies the 2.1.215 removal quoted above. **`code-reviewer.md`
  step 4's hand-reasoned correctness pass is therefore still the correct design, unchanged** — the
  acceptance (a) approach from lode-axyq stands, and nothing collapses back to the old direct-invocation
  step 1. Re-filed as **lode-cbc0** for the next check, per this entry's own instruction that a watch
  item with no owner goes stale as prose alone.

  **Update (lode-cbc0, 2026-07-21) — re-checked; NOT restored, watch item re-affirmed again.**
  Installed version is still **2.1.216** (`claude --version`) — no release has shipped since the
  lode-ebhk check the day before — and the local changelog cache
  (`~/.claude/cache/changelog.md`) confirms `## 2.1.216` is still the newest heading, with nothing
  newer beneath it. The 2.1.216 entry's sole `/code-review`-touching line is unchanged: "Improved
  `/code-review ultra` empty-diff message to name the exact base ref and suggest passing an explicit
  base" — still cosmetic, still not a restoration of model invocation. Nothing in the changelog
  reverses or qualifies the 2.1.215 removal quoted above. **`code-reviewer.md` step 4's
  hand-reasoned correctness pass stands, unchanged.** Re-filed as **lode-01tv** for the next check.

  **Update (lode-01tv, 2026-07-21) — re-checked; NOT restored, watch item re-affirmed again.**
  Installed version has moved to **2.1.217** (`claude --version`) — one release past the lode-cbc0
  check earlier the same day — and the local changelog cache (`~/.claude/cache/changelog.md`)
  confirms `## 2.1.217` is the newest heading, with nothing newer beneath it. The 2.1.217 entry
  contains **no `/code-review`-touching line at all** (`grep -ni "code-review"` against the full
  changelog cache finds nothing under the `## 2.1.217` heading; the nearest hit above it is still
  2.1.216's cosmetic empty-diff-message line, unchanged from the prior check). Nothing in the
  changelog reverses or qualifies the 2.1.215 removal quoted above. **`code-reviewer.md` step 4's
  hand-reasoned correctness pass stands, unchanged.** Re-filed as **lode-fr6z** for the next check.

  **Update (maintainer decision, 2026-07-21) — watch item RETIRED; the re-file loop is struck.** By
  maintainer decision, the recurring re-check-and-re-file cycle (lode-axyq→ebhk→cbc0→01tv→fr6z) ends
  here: **lode-fr6z is closed and will not be re-filed, and no successor watch ticket is to be stood up
  for this.** This supersedes the standing WATCH ITEM directive above — treat the upstream user-gating
  of `/code-review` as settled and not expected to revert. `code-reviewer.md` step 4's hand-reasoned
  correctness pass is the standing design regardless of what upstream does, so nothing here depends on
  continued monitoring. If upstream ever does restore model invocation of `/code-review`, re-open this
  thread **on explicit request only** — do not re-create a self-perpetuating watch ticket to poll for
  it. (The version pin above stays as the historical record of when and why the capability was removed.)

  Either way, one thing stays rejected regardless of what upstream does: do **not** hand-roll a local
  copy of the bundled skill into a project-scope `.claude/skills/code-review/SKILL.md` to make it
  nominally invocable. That forks a prompt whose source we cannot see, drifts silently as the real
  skill gains features, and reads as official while being a local hand-roll — precisely how this class
  of bug regenerates under a new name. `code-reviewer.md` instead runs its own reasoned correctness
  pass (see `.claude/agents/code-reviewer.md` step 4 and `docs/agents-workflow.md`'s landing-loop
  section).

  **Update (lode-905v, 2026-07-20) — the multi-agent bug hunt is rebuilt as a Workflow, invoked by the
  `/code` ORCHESTRATOR, not by the reviewer.** lode-axyq made the definitions honest about the loss
  above; this ticket restores the CAPABILITY without touching the two rejected paths (extraction from
  the Claude Code binary, a hand-rolled skill impersonating `/code-review` — both stay rejected).

  The first build attempt (superseded — see below) wired the workflow *inside* `code-reviewer.md` step
  4, on the assumption the reviewer's own dispatched context could invoke the `Workflow` tool. That
  assumption was checked and falsified: **two direct probes, both invocation-gated (a `ToolSearch`
  attempt, not just a tool-list inspection), on both a `coding` producer and a `code-reviewer`,
  dispatched the same way this pipeline dispatches every producer and reviewer (Agent tool, `isolation:
  "worktree"`), found `Workflow` absent from the tool set in both cases.** The `/code` orchestrator's
  own (main) session is the only context confirmed to reach it. So the boundary is main-session-only
  vs. every dispatched subagent, not a coding-vs-reviewer difference — and the agent registry's own
  "Tools: All tools" label for both subagent types is not a reliable guide to their actual runtime tool
  surface.

  **Update (lode-rlyx, 2026-07-27) — superseded: `/code` no longer invokes the workflow at all.
  Everything from here down to the lode-rlyx update at the end of this thread is the record of a
  design that was shipped and then removed; read that update before relying on any of it.**

  **Rescoped design (shipped): the orchestrator runs the workflow, the reviewer consumes its output as
  input.** `.claude/workflows/correctness-review.js` (FIND — one agent per correctness dimension:
  logic/edge cases, error handling, concurrency/ordering, API/contract misuse, test adequacy, sensitive
  data exposure — → VERIFY, a refute-biased skeptic per finding, default-refuted when uncertain → REPORT,
  ranked survivors; `pipeline()` over the dimensions so one dimension's findings verify while another is
  still finding, no barrier without a cross-item reason) is unchanged from the first attempt — its
  defect was *where it was invoked from*, not what it does, so it is salvaged rather than rewritten.
  What moved is the call site: `.claude/skills/code/SKILL.md` Phase 2 now runs
  `Workflow({ scriptPath: ".claude/workflows/correctness-review.js", args: { refRange: "trunk...land/<id>" } })`
  itself, right after verifying each builder's hand-off and right before dispatching that ticket's
  `code-reviewer`, and folds the surviving findings into the reviewer's dispatch prompt as
  **orchestrator-supplied, pre-computed candidates** — the reviewer still independently confirms each
  one against the real diff before acting on it (`.claude/agents/code-reviewer.md` step 4), and still
  runs its own hand-reasoned pass regardless of whether the workflow ran, errored, or returned nothing.
  This preserves the property that mattered from the start: the main session did not write the code
  under review, so an orchestrator-run correctness pass is still not the author's own review.

  **Concurrency (lode-2cf): one workflow call at a time, chosen over capping its internal fan-out.**
  The workflow fans one agent out per correctness dimension (six today) inside a *single* call; running
  two calls concurrently for two different tickets in a fan-out batch would silently double that
  fan-out on top of whatever builders/reviewers are already in flight, against the same memory budget
  `CODE_MAX_CONCURRENT_AGENTS` protects. `/code` Phase 2 runs each ticket's workflow call to completion
  before starting the next ticket's — the *reviewer dispatches themselves* still run concurrently as
  before, since each reviewer receives its own findings already computed and needs nothing further from
  the workflow once dispatched. Chosen over capping the workflow's own internal concurrency because it
  needs no new per-workflow knob and the added serialization is bounded (six agents' worth of one
  correctness pass per ticket, not a whole review pipeline).

  **The benchmark acceptance criteria remain open — both comparisons need a `Workflow`-capable session,
  which no producer dispatch is.** The ticket requires (a) a live head-to-head against a
  keystroke-invoked `/code-review high trunk...HEAD` on a real branch, and (b) a retrospective run
  against a genuine pre-upgrade review commit, with the exact verified ref ranges for five candidates
  already recorded on the ticket (`bd show lode-905v`) — best first candidate `b70be43`/lode-gpzn.13
  (`77960d8...22e4341`), which alone contains two distinct genuine correctness catches spanning two
  different dimensions. Neither comparison is buildable from a `coding` producer's dispatched context
  (this build's own re-probe reconfirmed `Workflow` absent, matching the first attempt) — (a) never was,
  since no model context can type a keystroke, and (b), even though the rescoped design makes the
  *production* wiring run from a `Workflow`-capable session, still needs a human (or the maintainer's own
  interactive session) to actually invoke it and record the result; a `coding` producer building this
  ticket is not that session. Do not declare parity or close this ticket's comparison criteria until both
  have actually been run and recorded here.

  **Update (2026-07-23) — both comparisons have now been run; the acceptance bar is reframed from
  parity to additive-value; the gate lands (lode-905v).** The retrospective (b) came back 2/2 and
  green (best candidate `b70be43`/lode-gpzn.13, `77960d8...22e4341`). The live head-to-head (a) was
  run by the maintainer's interactive session (runbook `specs/11-correctness-review-live-benchmark.md`):
  a keystroke `/code-review high trunk...HEAD` on `land/lode-568v.2 @ fe31ecf` (the
  "behavior-preserving LLMProvider seam" refactor) as the baseline, then the workflow 3× on the
  identical range, scored finding-by-finding by a fresh session (full table on `bd show lode-905v`).
  Result: **zero false positives across all three runs** — the refute stage killed every non-bug —
  and the workflow recalled the `lode config` ModelTier display regression (2/3 runs) and the
  batch-collect failure-isolation narrowing (1/3). It **missed the baseline's dominant finding in all
  three runs** (the Q&A synthesis timeout silently cut 600s → 120s), and worse, every run's finder
  *inverted* it — asserting trunk was unbounded when the Anthropic SDK's default client timeout is in
  fact 600s. That is a **systematic FIND blind spot** — finders trusting a diff's own
  "behavior-preserving" self-description instead of independently establishing prior behaviour
  (including implicit library defaults) — filed as **lode-eohb**, and distinct from the *stochastic*
  single-pass recall caveat **lode-p5gf**.

  **The decision: parity with `/code-review` is NO LONGER the bar — this supersedes the paragraph
  above.** **Update (lode-rlyx) — superseded by the lode-rlyx update at the end of this thread: the
  wiring described in this paragraph no longer exists; "supersedes the paragraph above" was not
  terminal.**
  correctness-review is wired as an **additive backstop**: Phase 2 folds its survivors into
  the reviewer's dispatch as pre-computed candidates, and the `code-reviewer`'s own hand-reasoned pass
  runs regardless of whether the workflow ran, errored, or returned nothing. A recall miss therefore
  *degrades the backstop on that run; it can never suppress the reviewer's own review*. So the honest
  bar for landing is **"adds real findings at an acceptable false-positive rate," not finding-count
  parity** — and on that bar the live evidence clears cleanly (0 FP, two genuine catches that
  `/simplify` alone would not have made). lode-905v lands on this bar. The two residual FIND-quality
  gaps — lode-p5gf (stochastic recall; a K-round-union mitigation is built but **unvalidated**, so it
  does not land with this ticket) and lode-eohb (the systematic blind spot) — are tracked as
  follow-ups and validated together via `specs/12-correctness-review-recall-validation.md`; neither
  blocks landing an additive backstop, because the failure mode of both is "sometimes adds less,"
  never "removes the reviewer's reasoning."

  **Update (lode-eohb) — the FIND-prompt fix for the "behavior-preserving" blind spot is built; the
  dev-loop validation is NOT, for the same structural reason lode-905v's own benchmarks needed a
  human.** `.claude/workflows/correctness-review.js`'s Find prompt (shared verbatim across all six
  dimensions — logic, errors, concurrency, contracts, tests, exposure — so the fix is general, not a
  timeout special-case) now instructs: treat a diff's own "behavior-preserving" / "no-op" / "pure
  refactor" self-description as a claim to **disprove**, not a fact; independently establish the
  PRIOR (base-of-range) behavior for every changed call — reading the base commit directly, not
  inferring it from the diff's framing — **including implicit library/SDK defaults** (timeouts,
  retries, pagination, …) that the diff may silently tighten. A new `reviewBase` (the left side of
  the range, when the caller passes a two-sided one) is threaded into the prompt alongside the
  existing `reviewTip`, so the instruction can point agents at a concrete `git show <reviewBase>:
  <path>` rather than leaving "check the prior behavior" unanchored. Verified end-to-end (script
  syntax + prompt rendering, both branches of the `reviewBase` ternary) against a stub Workflow
  harness (`bun`, the only JS runtime available in this producer's dispatched context — `node` is
  not installed) since the real `Workflow` tool is unreachable from here, same finding as every prior
  ticket in this thread.

  **Why this ships escalated, not `ready-for-code-review`:** the ticket's own acceptance bar requires
  a **dev-loop re-run against the saved fe31ecf baseline** (does the fixed prompt now recall the B1
  Q&A-timeout finding, still 0 false positives, B2/B3 not regressed) — that requires actually
  invoking `Workflow`, which no dispatched `coding`/`code-reviewer` context can do (confirmed once
  more this build: `ToolSearch` for `Workflow` and `select:Workflow` both return nothing). Sending
  this to `code-reviewer` first would hit the identical wall the reviewer also cannot cross, spending
  an Opus review cycle on a gap that isn't about code quality — so this escalates directly, mirroring
  exactly how lode-905v's own two required benchmarks were handled.

  **A second, compounding gap found this build**: the raw fixture files lode-905v's hand-off said
  were "preserved" and would be "committed onto the FIND-quality follow-up branch" —
  `specs/905v-live-results/` (the `/code-review` baseline text + 3 saved workflow-run JSONs) — **do
  not exist anywhere on this machine.** Searched recursively across the main checkout and every
  worktree under `.claude/worktrees/`: nothing. Only the *scored*, finding-by-finding table survived,
  pasted verbatim into `bd show lode-905v`'s notes. The extended `specs/12-...md` runbook (Part B)
  reproduces that scored table so a human running the validation isn't blocked on the missing raw
  files — re-running the workflow against the *code* at the verified-reproducible range
  `51dc7c2...fe31ecf` (confirmed via `git diff --stat` to touch exactly the files B1–B5 cite) doesn't
  need the old raw JSON, only the scored baseline to compare against — but the original runs can no
  longer be diffed byte-for-byte against new ones, which is worth knowing before anyone goes looking
  for them.

  **Explicitly out of scope**, filed as a follow-up (lode-3ci): whether the builder still needs to
  *keep* its worktree at all now that neither the reviewer nor a rebase pickup opens it, and whether
  `/land`'s worktree GC should change as a result. **Resolved below — kept as-is.**

  **Update (lode-p5gf) — FIND-recall reliability: K-round union + semantic dedup, mitigation shipped,
  validation still pending.** lode-905v's own retrospective already showed FIND recall is stochastic
  run-to-run — the tombstone bug above was found by 0 of 6 finders in one run and 4 of 6 in another,
  identical code and prompts — filed separately as lode-p5gf per maintainer decision to land lode-905v
  and treat reliability separately. Chosen mitigation, implemented in
  `.claude/workflows/correctness-review.js`: run each dimension's FIND prompt `FIND_ROUNDS` times (2,
  a starting default) independently and union the results via a shared near-duplicate merge, rather
  than loop-until-dry (rejected: no natural cost ceiling for a dimension that stays flaky every round,
  where a fixed K has a hard, predictable bound) or more finders/a completeness-critic pass (rejected:
  adds a whole extra reviewing role rather than reusing the redundancy the stochasticity itself calls
  for). **Cost tradeoff, explicit since this runs inside every `/code` pass:** Find calls multiply by
  `FIND_ROUNDS` (12 finder calls instead of 6, at the K=2 default); Verify calls do **not** multiply by
  `FIND_ROUNDS` — a dimension's own cross-round duplicates are merged *before* Verify runs, so a bug
  found in every round of the same dimension still costs exactly one Verify call.

  **Semantic dedup, not just line-proximity.** The original REPORT-stage dedup collapsed survivors only
  on an *exact* file:line match — deliberately, since a raw proximity window risked collapsing two
  genuinely distinct bugs. That left the residual gap this ticket names directly: the tombstone was
  cited at `:319` by some finders and `:322` by others, the *same* bug, missed by exact-location dedup.
  The fix (`mergeNearDuplicates` in the script) requires **both** a location match (same file, line
  within a small window) **and** title-token-Jaccard similarity above a threshold before two findings
  are treated as the same bug — closing the residual gap without reintroducing the risk the original
  exact-match rule was guarding against. The same function also unions a single dimension's own
  `FIND_ROUNDS`, so the K-round mitigation does not reopen the duplicate-findings problem lode-905v's
  REPORT dedup was built to close.

  **What is NOT yet true: `FIND_ROUNDS = 2` is a starting default, not a validated optimum, and the
  distribution-level acceptance bar (run the gpzn.13 retrospective, `77960d8...22e4341`, N≥5 times and
  report per-run recall) has NOT been executed.** That validation needs a `Workflow`-capable session —
  the same constraint as lode-905v's own benchmark above: no `coding` producer or `code-reviewer`
  dispatch reaches the `Workflow` tool, only the `/code` orchestrator's own main session does. A
  producer building this ticket is not that session, so it cannot run or record that validation itself.
  `specs/12-correctness-review-recall-validation.md` is the runbook (mirroring `specs/11`'s precedent
  for lode-905v's own live-benchmark step) for a human or the maintainer's own interactive session to
  run the retrospective N≥5 times against both the pre-mitigation and post-mitigation script and record
  per-run recall on lode-p5gf. **Do not declare this ticket's reliability bar met until that has
  actually been run and recorded there** — the code above is built and reviewable now, but "solved" is
  a claim only the runbook's result can support.

  **Reconciliation (2026-07-24) — the lode-eohb "ships escalated" note above is now historical, not
  current.** The `specs/12-correctness-review-recall-validation.md` Part-B validation this thread
  called for was run from a `Workflow`-capable session: B1 (the qa.py `600s`→`120s` timeout finding)
  went from missed-and-inverted 0/3 to surfaced-and-correctly-characterized 3/3, at 0 false positives.
  The B2/B3 recall dip observed under eohb-alone was single-round stochastic variance, not a
  regression from the FIND-prompt fix — it recovered to ~pre-fix rates once measured under the
  combined eohb+p5gf production config (the K-round union + semantic dedup this ticket describes
  above, which is what actually ships once both land). lode-eohb subsequently landed on `trunk` via
  `/land` (merge `3e4f3c4`), after lode-p5gf. The paragraphs above are left as-is as the accurate
  record of what was true at build time.

  **Update (lode-wtwb, 2026-07-24) — a crashed VERIFY agent was defaulting its finding to
  `refuted`, silently discarding real bugs; fixed to a third `unverified` state.** During the
  `/code` run of 2026-07-24, the workflow was invoked over `trunk...b760b3d` (lode-ns3r) and hit an
  API session limit: 14 of 22 agents errored, most of them VERIFY skeptics. The workflow returned
  `findings: []` — an apparently clean review — but every one of the 10 entries in `refuted[]`
  carried the reason `'verifier produced no verdict — defaulted to refuted'`. Zero of the
  refutations were real; a High-severity `set -o pipefail` + `grep -q` SIGPIPE finding in
  `scripts/release-bump.sh` (mechanically confirmed true, see lode-ns3r's own comment thread) was
  discarded this way, and the orchestrator only caught it by hand-inspecting refutation-reason
  strings. The failure mode is fail-**open** on the wrong side, and gets *worse* under load — the
  more agents in flight, the more likely a verifier dies, exactly when review matters most.

  **Fix, in `.claude/workflows/correctness-review.js`:** a verifier that produces no verdict is now
  a genuine third state — neither confirmed nor refuted — returned in its own `unverified` array
  (never folded into `refuted`), each entry carrying an `unverifiedReason` instead of a
  `refutationReason`. The same near-duplicate merge `survivors` already used (lode-905v's
  `mergeNearDuplicates`) applies to `unverified` too — within that array only, so the several copies
  one infra fault leaves unverified across dimensions collapse to one. It deliberately does NOT
  collapse across the three arrays: they partition findings by verification *state*, so a bug one
  dimension confirmed and another left unverified legitimately appears in both, each label true of
  its own copy. Cross-pool merging is the thing to avoid — dropping an unverified copy because a
  similar-titled entry sits in `refuted` would re-create this very bug. The top-level
  result also gains **`degraded`** (true the moment any Find round, Verify agent, or whole dimension
  in the run failed to produce output) plus `stats.{findRoundsFailed,verifyAgentsFailed,
  dimensionsFailed,unverifiedCount}` — a partially-failed run is now distinguishable from a clean
  one by the caller checking one boolean, never by parsing reason strings by hand.

  **Wiring — the fix alone doesn't help unless the two consumers actually read the new fields.**
  `.claude/skills/code/SKILL.md` Phase 2 now folds `result.unverified` (labeled plainly as
  "unverified, not refuted") and `result.degraded` into the reviewer's dispatch prompt, alongside
  the existing survivors, and step 5's user-facing report calls out a degraded run explicitly.
  `.claude/agents/code-reviewer.md` step 4 now says an `unverified` finding gets at least as much
  scrutiny as a confirmed survivor (its skeptic never weighed in at all, unlike a `refuted` one that
  was actively checked and rejected), and that a degraded run's silence on a failed dimension is not
  evidence that dimension is clean.

  **Acceptance criterion 3 (regression replay of the lode-ns3r run, `resumeFromRunId
  wf_9b60ff50-0c6`) is NOT executed by this fix — same structural reason lode-p5gf's and lode-eohb's
  own validations needed a human.** `Workflow` is reachable only from the `/code` orchestrator's own
  main session (verified empirically, lode-905v); a `coding` producer building this ticket is not
  that session, so it cannot invoke `Workflow` (with or without `resumeFromRunId`) to confirm the
  SIGPIPE finding now survives to the reviewer as `unverified` rather than vanishing. The runbook for
  a human (or the maintainer's own interactive session) to execute this replay and record the result
  is `specs/13-correctness-review-verify-crash-regression.md`, following the `specs/11`/`specs/12`
  precedent. **Do not treat lode-wtwb's acceptance bar as fully met until that replay has actually
  been run and its result recorded on this ticket** — the code above is built and reviewable now,
  but the regression-replay criterion specifically needs the runbook's result.

  **Update (lode-rlyx, 2026-07-27) — REVERSED: the `correctness-review` Workflow is removed from the
  `/code` path entirely. Everything above about how `/code` *invokes* it is now historical.** The
  script (`.claude/workflows/correctness-review.js`) is deliberately **kept on disk** and remains
  manually invocable from a Workflow-capable session — `specs/11`–`specs/13` still reference it, and a
  one-off review of a genuinely hairy diff is a reasonable use. What changed is that `/code` Phase 2 no
  longer runs it before dispatching a reviewer; the `code-reviewer`'s own reasoned pass **is** the
  correctness review, not a backstop to one.

  **Why — measured, not asserted.** *Provenance of the figures below: the harness's per-agent token
  accounting as reported in that invocation's task notifications, read by the orchestrating session as
  the batch ran. They are not reproducible from anything in this repo and no artifact of them was
  persisted — treat them as a faithful contemporaneous record, not as a citation a later reader can
  independently check. The orders of magnitude are what the decision rests on, not the third digit.*
  The 2026-07-26 fan-out (16 tickets dispatched) ran four workflow
  passes. They consumed **~8.1M subagent tokens, roughly 80% of the entire invocation's spend**, against
  ~1.6M for eleven builders and ~0.4M for five reviewers *combined*. That exhausted the operator's
  session limit in about twenty minutes, killing four in-flight reviewers mid-work and stranding more
  work than the batch landed (one ticket merged; two reached `ready-for-land`; ten sat built-but-
  unreviewed). The workflow was additionally single-flight by design — its per-dimension fan-out draws
  on the same memory budget `CODE_MAX_CONCURRENT_AGENTS` protects (lode-2cf) — so it serialized every
  reviewer dispatch behind it and was the batch's throughput bottleneck as well as its cost centre.

  **What the spend bought did not justify it, on that same run's evidence:**
  - Reviewers **overturned** workflow findings repeatedly, on facts the workflow had not checked.
    `lode-wtwb`'s reviewer refuted a four-finding cluster by reading the persisted artifacts of the very
    crash that motivated the ticket (10 failed verifiers ↔ 10 no-verdict entries, a 1:1 match proving
    `agent()` resolves falsy rather than rejecting), and rejected a `falsePositiveRate` finding outright.
    `lode-hwbm`'s reviewer falsified its ticket's entire premise with two `bd list` calls.
  - The two branches that received **no** workflow at all (`lode-cs5u.1`, `lode-k9ef`) produced findings
    at least as good as the workflow-backed ones — `cs5u.1`'s reviewer caught a wrong cross-reference
    that pointed readers at exactly the stale record the ticket existed to retire.
  - Near-duplicate survivors (nine entries for four distinct bugs on `lode-wtwb`; seventeen for two
    regressions plus a test gap on `lode-3dlt`) meant the reviewer spent effort de-duplicating a list
    before it could reason about the diff. That is lode-lgvv, which is real — but fixing it would have
    made an unjustified cost merely cheaper, not justified.
  - One verify agent attempted `rm -rf /land-state`, constructing an absolute path from a relative one.
    It failed harmlessly (no such path exists), but a review mechanism that can attempt destructive
    filesystem operations on a bad path inference is carrying risk the reviewer's read-and-reason pass
    does not.

  The honest read is that the signal was coming from the Opus reviewer's own reasoning throughout, and
  the fan-out was buying volume rather than accuracy. **Do not reintroduce it to the `/code` path
  without new evidence that it beats the reviewer's own pass per token** — the bar is comparative, not
  "does it ever find something real." It does find real things; so does the reviewer, for ~5% of the
  cost. **What would actually meet that bar:** the head-to-head runbooks already written for exactly
  this question — `specs/11-correctness-review-live-benchmark.md` (workflow vs. a real review on the
  same diff) and `specs/12-correctness-review-recall-validation.md` (recall against known-seeded bugs)
  — re-run with per-side token cost recorded alongside the findings, showing findings-per-token in the
  workflow's favour on diffs the reviewer alone had already passed. Absent that, an argument that it
  "would have caught X" is not evidence: the reviewer has to have *missed* X first.

  **Enforcement is instruction-only, deliberately.** By the lode-kt6g rule (below), an
  irreversible-and-public act earns a mechanical fence and a local-and-recoverable one earns
  instruction — a stray workflow run costs only tokens. More decisively, a `PreToolUse` hook *could not
  work here*: the forbidden call (inside `/code` Phase 2) and the deliberately retained one (a manual
  one-off, or `specs/11`–`specs/13`) are the same tool, the same `scriptPath`, from the same main
  session, so any deny broad enough to stop the first would destroy the second. Don't build one.

  **Consequences for dependent tickets.** `lode-arx1` (gate the workflow on diff content) was built and
  pushed before this decision and is now moot — a gate on a call that no longer happens. `lode-lgvv`
  (mergeNearDuplicates under-merges), `lode-m73d` (per-run telemetry), `lode-eltr` (no empty-range
  guard) and `lode-dwtp` (make the unverified path demonstrable) all remain *correct* descriptions of
  real defects in a script that is now off the hot path; all five are **deferred rather than closed**, so
  nothing is discarded if the workflow is ever revived for manual use at scale.

  **`lode-arx1`'s built branch is deliberately left on origin, and its stale `ready-for-code-review`
  label was removed (lode-rlyx's technical review).** `origin/land/lode-arx1` (`157e44b`) edits the very
  `/code` Phase 2 block this ticket deletes, so it can never merge cleanly again and must not be landed
  as-is; if the workflow is ever revived, that branch is a design reference, not a mergeable change.
  Deferring the ticket keeps it off `/land` (which queues on the `ready-for-land` label) and off
  `/code`'s stranded-review sweep (which filters `--status in_progress`), but the ticket was still
  carrying `ready-for-code-review` — inert only because of that status filter, and armed the moment
  anyone moved it back to `in_progress` to un-defer it, at which point a reviewer would have been
  dispatched at a moot branch and pushed it to `ready-for-land`. The label is gone; the deferred status
  is now the single thing holding it. Two residues are accepted, not bugs: `/land` §1a enumerates
  **every** `origin/land/*` ref for its stacked-branch graph, so a parked branch adds pairwise
  merge-base work to every pass forever (bounded, and the graph is only consulted for branches actually
  in the queue), and `/sweep` will list the five deferred tickets report-only each pass, which is the
  intended visibility.

  **Update (lode-vs7g): eliminating the collision (lode-em6v, above) closed the *invisible*-worktree
  half of the leak, but not the *proactive-cleanup* half.** lode-em6v's own acceptance criterion 1 —
  "a clean code-reviewer run and a clean rebase-pickup run leave NO worktree behind" — was satisfied
  only in the sense that the worktree is now always branch-attached and hence *reachable* by `/land`'s
  backstop 1; it was never actually **removed** on a clean run, only left for that backstop to sweep up
  later, once the branch **merges into `trunk`**. Two gaps followed directly from that: (1) a ticket
  reviewed or rebase-picked-up N times across N cycles left N such worktrees standing simultaneously,
  all waiting on the same eventual land; (2) an **escalated** ticket's branch never merges into `trunk`
  at all, so backstop 1 structurally cannot reach it — that worktree leaked **indefinitely**, until a
  human resolved the escalation and the branch eventually landed.

  **Fix: `/code`'s own orchestrating session reclaims the worktree, right after the subagent that used
  it returns — on *either* outcome (`ready-for-land` or `land-escalated`) — and *derives* which worktree
  that was, rather than being told.** Neither `code-reviewer` nor a rebase pickup can `git worktree
  remove` the worktree it is currently standing in, so `/code` (never itself worktree-isolated — it runs
  from the repo root, the same place `/land`'s own GC already runs its `git worktree remove --force`
  from) does the removal immediately after collecting that agent's result, per ticket, not batched to
  the end of a fan-out.

  The derivation is the load-bearing choice, and it falls straight out of lode-em6v: the agent's branch
  is always `land/<id>--<its-own-worktree-dir>`, so the **ticket id alone** recovers both the worktree
  path and the branch name from `git worktree list --porcelain`. An earlier draft had each agent
  *report* its path and branch in its final message and had `/code` act on that string; deriving instead
  is strictly better on the cases that actually leak. It needs no cooperation from the agent, so it
  still fires when the agent **crashed**, **escalated**, or returned a garbled path — whereas a reported
  string is exactly what a crashed agent never sends, leaving the very case this ticket exists to close
  (an escalated branch, which never merges into `trunk`, so backstop 1 can never reach it) uncovered a
  second time. It also reclaims **every** worktree a ticket accumulated across N review/pickup cycles,
  not just the last one, and it removes the trust boundary (and the path-validation guard that boundary
  would otherwise need). It cannot touch the **builder's** worktree: that is branch-named
  `worktree-agent-*`, never `land/<id>--*`, so the filter skips it by construction and `/land`'s
  `review_worktree` GC still finds it. **Update (lode-h1vn) — superseded, below: `/land` reclaims
  the builder's worktree via its backstop sweep now — the `review_worktree`-keyed loop is deleted.
  The guarantee is unchanged; only the mechanism is.**

  Two `git` behaviours this depends on, both verified live: `rtk` reformats `worktree list --porcelain`
  and breaks the field parse, so the reclaim uses **plain `git`** (same hazard as lode-9j7); and the
  agent harness **locks** a launch worktree while its agent runs (`locked claude agent <name> (pid …)`)
  and unlocks it on exit, so a **single** `--force` removes a finished agent's worktree but *refuses* a
  still-locked one — it fails safe. `-f -f` must not be used: it would override the lock and rip a
  worktree out from under a live agent.

  Safe on both outcomes, for the same reason the fetch-and-checkout architecture is: by the time either
  agent returns, its worktree holds nothing `origin/land/<id>` doesn't already have — a clean pass
  pushes first, and an escalation's aborted merge (rebase pickup) or reverted-to-green commit (reviewer)
  leaves the checkout an exact mirror of what is already on origin. `/land`'s backstops 1-4 are untouched
  and remain a *partial* net — they still only reach a worktree whose branch eventually merges into
  `trunk`, which is precisely why the reclaim above must not depend on the agent saying anything.
  Scope: `.claude/skills/code/SKILL.md` (one reclaim block, defined at step 0 and referenced by step 1
  and Phase 2), `.claude/agents/code-reviewer.md` and `.claude/agents/coding.md`'s rebase-pickup section
  (both now say plainly that they neither remove nor report their own launch worktree). Docs-only
  change, no code/tests affected — same shape as lode-em6v.

- **Builder worktree retention — kept as-is; the builder keeps its worktree through the whole
  build → review → land lifecycle, and `/land`'s GC still reclaims it only on a clean land (lode-3ci,
  a follow-up to lode-k5e/lode-8k3 above).** After the reviewer/rebase-pickup architecture change,
  nothing ever *reads* the builder's original worktree again after the push — the reviewer and the
  rebase pickup both fetch `origin/land/<id>` into their **own** fresh worktree instead. So the
  builder's worktree's only remaining function, for the rest of the lifecycle, is to sit on disk as a
  path for `/land`'s GC to `git worktree remove` once the ticket lands. That raised the obvious
  question: could the builder (or `/land`, earlier) reclaim it right after hand-off instead of waiting
  for land?

  **Decision: no change.** Three reasons. (1) **No proven problem.** A live check (2026-07-09, mid a
  heavy `/code` fan-out) found 20 worktrees on disk; every one not this session's own was either an
  active reviewer/rebase-pickup worktree with `land/<id>` checked out, or a builder worktree for a
  ticket still genuinely `in_progress` (`ready-for-code-review` or `needs-rebase`) — none belonged to
  an already-`closed` ticket. `/land`'s land-time GC is doing its job; there is no observed leak to fix
  by moving the reclaim point earlier, only a hypothetical reduction in *peak* worktree count that
  scales with fan-out width and review/land latency, not with which pipeline stage does the reclaiming.
  (2) **Real cost to change it.** Reclaiming right after hand-off would need `coding.md`'s hand-off step
  to stop recording a worktree the GC can still find (or `/land`'s `git worktree list` guard to accept
  "already gone" as the normal case rather than a machine-mismatch signal), plus edits to the repeated
  "I must NOT remove my worktree" invariant across `coding.md`, and to `land/SKILL.md`'s GC section and
  its "best-effort... on a clean land" framing — a wide blast radius for an unproven benefit, and it
  touches `/land`'s mechanics directly (the reason this was split out of lode-k5e to begin with). (3)
  **An existing mechanic depends on the worktree surviving past the build step**: `/land`'s bounce path
  explicitly keeps the worktree because "the rebuild ticket may still want the tree" — an early-reclaim
  policy would have to special-case that, not just the clean-land path.

  **Revisit trigger:** a *demonstrated* leak, not mere in-flight count — e.g., a worktree found rooted
  at an already-`closed` or long-abandoned ticket (GC actually missing one), or a concrete disk-pressure
  incident tied to worktree accumulation. If that happens, the two candidate fixes are (a) the builder
  reclaims its own worktree right after a clean hand-off (`ready-for-code-review`, gates green, pushed),
  accepting that `/land`'s GC then always no-ops for tickets built after the change, or (b) `/land`
  reclaims it one stage earlier, at the review→`ready-for-land` swap, instead of waiting for the land
  itself. Either requires updating `coding.md`, `code-reviewer.md`, and `land/SKILL.md`'s GC section
  together so the hand-off contract and the GC contract don't drift apart.

- **The revisit trigger above fired: `/land`'s GC backstop was blind to `land/<id>`-branched
  worktrees, and to dangling local `land/<id>` refs (lode-r78, decided 2026-07-10).** The
  lode-k5e/lode-8k3 architecture change (above) means the reviewer and a rebase pickup each check
  `land/<id>` out into their **own** fresh worktree, not the builder's — exactly the new worktree shape
  the "no `land/<id>` branch is ever checked out in any worktree" note (above, describing the state
  *before* that decision) stopped being true of. `/land`'s per-ticket GC net only knows one worktree per
  ticket (`metadata.review_worktree`), and the lode-9j7 backstop sweep matched only
  `branch ~ /^worktree-agent-/` — so a ticket reviewed across multiple cycles left *extra*
  `land/<id>`-branched worktrees neither net could see, and they accumulated indefinitely (5 observed
  live on one pass, plus older ones rooted at already-`closed` tickets going back weeks — precisely the
  "worktree found rooted at an already-closed ticket (GC actually missing one)" trigger condition
  above). Local `land/<id>` branch refs had the same gap for a narrower reason: the per-ticket removal
  only runs `git branch -D` when it also finds a matching worktree, so a ref that lost its worktree by
  any other path lingered even after `origin/land/<id>` was deleted.

  **Fix (minimal, no architecture change):** extend the backstop sweep's branch match from
  `^worktree-agent-` to also match `^land/`, under the *same* `locked`+`merged-into-trunk` guard already
  used for `worktree-agent-*` (an in-flight `land/<id>` worktree is excluded because its branch hasn't
  merged into `trunk` yet, or because the worktree is locked mid-build/-review — never both false for
  live work). Add a second, independent backstop step that deletes any local `land/<id>` branch ref
  whose `origin/land/<id>` counterpart is gone: an in-flight ticket's remote branch always exists (only
  `/land` itself deletes it, only after landing/bounce/drop), so "remote absent" is sufficient signal on
  its own — no extra locked/merged check needed, since `git branch -D` already refuses harmlessly if the
  ref is still checked out in some worktree. See
  [`.claude/skills/land/SKILL.md`](../.claude/skills/land/SKILL.md#4-land-the-survivors).

  **Update (lode-jiyk): the branch-name-match half of the fix above was superseded.** lode-jiyk
  unified this backstop's worktree-sweep branch-name match (`^worktree-agent-`/`^land/`) with
  lode-mxeu's separate by-SHA/detached-worktree sweep into a single loop keyed on **HEAD-sha
  ancestry** (`git merge-base --is-ancestor <HEAD-sha> trunk`) plus `unlocked` — the worktree sweep
  matches no branch-name pattern any more. The **second backstop** decided here — deleting a dangling
  local `land/<id>` ref whose `origin/land/<id>` counterpart is gone — is a separate, bare-**ref**
  sweep, unaffected by that unification: it still matches `refs/heads/land/*` by name, and must, since
  `refs/heads/*` is shared with human branches. It was, however, amended by **lode-em6v**: it keys on
  an *exact* name match against origin's listing, so it now strips the worktree suffix
  (`${BR%%--*}`) before comparing — the "remote gone ⇒ stale" semantics decided here are unchanged.
  The lode-em6v update above has both stories in full (the unification, and why skipping that strip
  would turn this backstop into a ref shredder). Mechanism of record:
  [`.claude/skills/land/SKILL.md`](../.claude/skills/land/SKILL.md#4-land-the-survivors) §4 — verify
  any new claim against it, not against this entry.

- **Dead-lettered `refresh` jobs tombstone their external: a `worker.py` terminal-transition hook, not
  a reconcile sweep (lode-at8, decided 2026-07-09).** The gap: a `refresh` job that exhausts its
  retries and reaches `dead` left no record against the external at all — `head_snapshot_id` stayed
  `NULL`, indistinguishable from a draw-down still in flight. [externals.md](externals.md#draw-down-rules)'s
  "Fetch-outcome taxonomy" already documented "on `dead`, the caller writes a tombstone snapshot" —
  nothing had ever implemented that caller. **Chosen mechanism: (a) a `worker.py` dead-letter hook**
  (`register_dead_letter`, `src/lode/worker.py`), invoked once, in its own transaction, immediately
  after a job's status commits to `'dead'` — from *both* dead-letter gates (`run_one`'s max-attempts
  gate and `_reclaim_stale_running`'s crash-reclaim gate). `refresh` registers
  `_refresh_dead_letter_hook`, which calls `lode.externals.ingest_snapshot` with
  `status='tombstone'` and a body carrying the job's `last_error`, under the exact same convention a
  PERMANENT (non-retrying) fetch failure already uses — no schema change. **Rejected: (b) a
  `reconcile.py` sweep** for dead `refresh` jobs with no tombstone — cheaper (no worker change) but
  introduces a lag (a dead-lettered URL stays indistinguishable from "in flight" until the next
  reconcile pass) and a second module that has to know about `externals`/`snapshots` shape, on top of
  `lode.drawdown`/`lode.externals` already owning that. **Generalization deferred, not built:** the
  hook registry is per-job-type (mirrors the existing `HandlerFn`/`_REGISTRY` run-handler pattern), so
  `embed`/`enrich` could register their own dead-letter hooks later, but neither needs one today —
  `lode-bvg` (the sibling "`failed` vs `dead` under-observed" ticket) resolved by fixing a *read*
  predicate (`enrichment_view._enrichment_state`), not by adding a write-side dead-letter effect.
  **Accepted gap:** the hook's tombstone write is a *separate* transaction from the status-to-`dead`
  UPDATE (never nested — mirrors this codebase's existing "sequential, not nested" composition of
  standalone-transactional functions, e.g. `lode.drawdown.refresh_external`'s own
  ingest-then-repoint sequence); a crash between the two commits leaves a job `'dead'` with no
  tombstone yet. Narrow and accepted — the job row's own `last_error` already carries the diagnostic,
  and nothing sweeps this specific gap today. **Also decided: no "leave prior content alone" carve-out.**
  If an external already has an `ok` head snapshot and a *later* refresh (`lode-w0h.6`'s staleness
  policy, not the paste-triggered first draw-down) exhausts retries, the hook still moves the head to
  a tombstone — `docs/externals.md`'s TRANSIENT-failure row commits to writing a tombstone on `dead`
  unconditionally, and the alternative (silently keeping stale "known-good" content live while its
  own refresh machinery has given up on it) is a worse failure mode to ship silently. Revisit if this
  proves too aggressive once `lode-w0h.6` ships and staleness re-fetches are common.

- **Dead-letter recovery ownership — settled: two mechanisms, split by job type, no overlap
  (lode-621, cross-referencing lode-at8).** Both tickets are instances of the same defect shape — a
  job reaching the terminal `dead` status (max retries exhausted) with nothing observing or acting on
  it — but for two different job types, and the right recovery action differs per type:
  - **`embed` jobs (lode-621) → owned by `reconcile._embed_gap_step`'s periodic sweep.** A dead embed
    job means only that the *async* attempt to vectorize a still-valid body failed; the body itself
    (a note version or an external snapshot) is untouched and still embeddable. A blind periodic
    re-enqueue is a safe, cheap, idempotent recovery (`ON CONFLICT DO NOTHING` against
    `idx_jobs_live`) — no per-job-type hook is needed. lode-621 extended this existing sweep (already
    the mechanism for notes) with a snapshot arm, so a dead embed job on an external's current
    snapshot is now re-enqueued exactly like a note's version — closing the gap that made a
    lode-w0h.8-mirrored snapshot silently vector-less forever once its embed job died.
  - **`refresh` jobs (lode-at8) → owned by a worker terminal-transition hook.** A dead `refresh` job
    means the URL is *permanently* unfetchable (retries already exhausted the backoff chain); blindly
    re-enqueueing the same fetch forever would not converge, so instead the terminal transition writes
    a durable **tombstone snapshot** recording the failure, distinguishing "permanently dead" from "draw-down
    still in flight." That needs a hook fired at the exact moment a job goes `dead` (a periodic sweep
    would only add unbounded discovery lag to a failure that is already final) — lode-at8's `worker.py`
    dead-status-transition hook, registered per job type (mirroring the existing job-handler `_REGISTRY`
    shape), fired sequential-not-nested immediately after the `dead` status commit.

  **Why this doesn't collide:** the two mechanisms watch disjoint job-type sets (`embed` vs.
  `refresh`) and take disjoint actions (retry-by-re-enqueue vs. record-permanent-failure). Nothing
  implements the same recovery twice. If a *third* job type's dead-letter needs recovery, the fork is
  this: "is the underlying content still valid and cheap to retry?" → sweep; "is retrying pointless and
  the interesting fact is that it's permanently dead?" → terminal-transition hook.

- **`/land`'s worktree backstop 1 predicate widened from "merged into trunk" to "merged into trunk OR
  captured on origin" (lode-amif, a residual gap surfaced by lode-vs7g's own review).** lode-vs7g made
  `/code`'s orchestrating session eagerly reclaim a reviewer's / rebase-pickup's launch worktree right
  after the subagent returns, on either outcome (`ready-for-land` or `land-escalated`) — closing the
  steady-state leak. What it explicitly could not close: backstop 1's own predicate
  (`.claude/skills/land/SKILL.md` Section 4), `unlocked AND HEAD-sha is-ancestor-of trunk`, is never
  satisfied by an **escalated** branch (it never merges into `trunk` by definition — that's what
  "escalated, held for a human" means). So if the `/code` session itself dies **before** it can run its
  own eager reclaim (a crash mid-fan-out), the worktree leaks **indefinitely** — the same failure
  lode-vs7g exists to prevent, one level up, and structurally unreachable by the trunk-only backstop.

  **Decision: widen the predicate's real invariant from "merged into trunk" to "already captured
  elsewhere," and test it via a second arm.** "Merged into trunk" was always a stand-in for "this
  worktree's content is safely captured elsewhere, so removing it loses nothing" — never the goal
  itself. A reviewer/rebase-pickup worktree satisfies that real invariant by construction the moment it
  has pushed to `origin/land/<id>` (lode-k5e/lode-8k3), regardless of whether the branch ever lands.
  Backstop 1's loop now reclaims a candidate if `git merge-base --is-ancestor "$SHA" trunk` **or** — for
  a branch-attached worktree — `git merge-base --is-ancestor "$SHA" "origin/${BR%%--*}"` (the `${BR%%--*}`
  strip maps the lode-em6v worktree-uniqueness suffix `land/<id>--<worktree-dir>` back to the bare
  `origin/land/<id>` ref, the same mapping backstop 2 already applies). A detached worktree (no branch
  name to resolve) and a builder's own `worktree-agent-*` worktree (never pushed to origin — no origin
  counterpart exists, so the ancestor test simply fails) are unaffected by the new arm; it is `false`
  for both, leaving their behavior exactly as before.

  **Accepted, not newly introduced — and benign on this arm: the zero-divergence residual (lode-9hgu)
  cannot reach a *live* reviewer/rebase-pickup worktree, because the harness locks it.** A
  reviewer/rebase-pickup worktree, freshly checked out at `origin/land/<id>`'s current tip, is trivially
  "an ancestor of" that same tip from checkout until its first local commit — during that window the new
  arm's ancestry test reads true even though nothing new has been pushed. What stops the sweep there is
  the loop's existing `locked` filter: **the Claude Code harness locks every `isolation: worktree` launch
  worktree for the lifetime of the agent standing in it** (lock reason `claude agent <name> (pid <n>
  start <n>)`, released when the agent exits). Neither `.claude/agents/code-reviewer.md` nor `coding`'s
  rebase pickup calls `git worktree lock` itself — that is why an earlier draft of this entry recorded
  them as "unlocked" — but both *run inside* such a worktree, so a live one is `locked` in practice and
  the awk filter drops it before the predicate is ever evaluated. (`coding.md` additionally takes an
  *explicit* lock on its producer build worktree, per lode-oqr, because it unlocks again at its first
  commit — earlier than the harness would.) Verified against a running reviewer during this ticket's own
  technical review: `git worktree list --porcelain` reported the reviewer's worktree `locked`, with the
  harness's pid-keyed reason, though the reviewer never locked anything itself.

  So the widened arm can only reach an **exited** agent's worktree, which at zero divergence holds
  nothing but uncommitted, ungated scratch from a run that never finished — while the authoritative
  content sits on `origin/land/<id>` and the ticket is re-reviewed from there. That is *precisely* the
  worktree this widening exists to reclaim, so the residual is benign here, unlike on the trunk arm
  (where the same proxy destroyed two live builds before lode-oqr added the explicit lock). **lode-9hgu
  has since landed (6591ba9)**. It did not *replace* the "merged"/"captured" proxy — both ancestry arms
  still run, and still decide candidacy — it added a real dirty-tree guard (`git -C "$WT" status
  --porcelain`) directly below them in the loop, gating **both**: a worktree that is captured yet dirty
  is KEPT, not reclaimed. See the entry below and the loop itself in
  `.claude/skills/land/SKILL.md`. lode-9hgu's build correctly relied on the harness lock rather than
  duplicating it: "code-reviewer / land-review worktrees do not lock" is true only of the *agent docs*,
  not of the *running system*, so lode-9hgu did not add a doc-driven `git worktree lock` to
  `code-reviewer.md` — that would have duplicated the harness lock and, having no pid to key staleness
  on, would not have been released if the agent died, reopening the very indefinite leak lode-amif (this
  entry) closes.

  Scope: `.claude/skills/land/SKILL.md` Section 4 (backstop 1's loop and its surrounding prose) plus
  this entry. Docs/prompt-only — no Python code changed.

- **`/land`'s worktree-reclaim backstop now guards on the ACTUAL invariant (dirty tree), not just the
  "merged into trunk" proxy that reads TRUE at zero divergence (lode-9hgu, decided/built 2026-07-13,
  cross-referencing lode-oqr/lode-jiyk/lode-amif).** lode-jiyk's unified backstop (`.claude/skills/land/SKILL.md`
  Section 4) reclaims any unlocked worktree under `.claude/worktrees/` whose HEAD-sha is an ancestor of
  `trunk`. That predicate is a *proxy* for the real safety question ("is this work captured
  elsewhere"), and the proxy is exactly wrong at zero divergence: a worktree freshly branched off
  `trunk` HEAD is trivially "merged" before a single commit exists, so its live, uncommitted working
  tree reads as safe to `--force`-remove. lode-oqr closed this gap only for the `coding` producer
  (which locks its worktree before writing and unlocks after its first commit). The system has exactly
  **two** lock sources, and between them they leave three worktree classes holding no lock at all by the
  time the sweep sees them: the harness locks a *live* `isolation: worktree` agent's worktree for that
  agent's lifetime and *releases it on exit*, and `coding.md` locks the pre-first-commit window — so an
  interactive `EnterWorktree` session, a human's hand-made worktree (which `CLAUDE.md` *mandates* for
  all work), and an **exited** agent's leftover scratch are all unlocked. Each of those, sitting at
  trunk HEAD with uncommitted edits, was a live candidate for the lode-oqr failure mode (which
  destroyed two builds' uncommitted work) every time `/land` ticked (it self-paces on a 5-minute loop).

  **Considered:** (a) add a dirty-tree guard testing the actual invariant directly; (b) narrow the
  path guard to a harness-owned directory convention (e.g. `.claude/worktrees/agent-*`) — rejected,
  re-introduces a name dependency lode-jiyk exists to eliminate, and does not protect an *interactive*
  `agent-*` worktree, the likelier victim; (c) require every worktree-creating path to raise
  `git worktree lock` (spread lode-oqr's protocol beyond `coding.md`) — rejected, most places to keep
  in sync and cannot cover a human's manual `git worktree add`; (d) accept and document only —
  rejected, leaves a P1 that destroys uncommitted work. **Chose (a).**

  **Fix:** the generalized backstop loop now checks `git -C "$WT" status --porcelain` immediately
  after the existing `merge-base --is-ancestor` check, and skips (keeps) the worktree unless that
  command both succeeds AND prints nothing. Scoped to *that* loop only — the per-ticket removal loop
  earlier in the same section reaches a much narrower candidate set (its `--force` is keyed to
  `metadata.review_worktree` on a ticket that *just landed this pass*, and nothing ever writes an
  interactive or hand-made worktree's path into a ticket's metadata, so it cannot reach one), and the
  P1 was deliberately not made to carry a rider. **That exemption is narrower than it first reads, and
  is tracked in lode-h1vn:** "the content is provably on trunk" is a claim about the *branch tip* that
  merged, not about the *working-tree state of that directory* at GC time — and the per-ticket loop has
  neither a `locked` check nor a dirty check, so it force-removes unconditionally. Same primitive, same
  risk class, only one loop fails safe. The fix there is not simply "add the same guard" (a dirty-guard
  could silently no-op the per-ticket cleanup entirely, re-opening the very leak Section 4 exists to
  close), which is exactly why it is its own ticket. **Resolved by lode-h1vn (below): the per-ticket
  loop was deleted outright rather than guarded — see that entry for the measurement that ruled out the
  silent-no-op risk and the reasoning for why deletion, not guarding, was the simpler correct fix.**

  **The guard is coupled to `.gitignore`, and that coupling is load-bearing.** `status --porcelain`
  reports *untracked* files too, and a finished builder worktree is full of them: `venv/` (every
  producer runs `scripts/python-init.sh` in its own worktree), plus `.nox/`, `__pycache__/`,
  `.pytest_cache/`, `.ruff_cache/` and setuptools-scm's generated `src/lode/_version.py`. All of those
  are gitignored today, so a real, finished builder worktree reads *clean* and is still reclaimed —
  verified against every live worktree on the build machine when this landed. But if a build artifact
  ever stops being ignored, **every** worktree reads dirty, and this backstop silently stops reclaiming
  anything at all (it fails safe, so it leaks worktrees rather than destroying work — the failure is
  quiet, not dangerous). If worktrees ever start accumulating with no explanation, suspect `.gitignore`
  before suspecting this loop.

  **Fail-safe, not fail-open — the same class of bug this decision exists to fix, one level down.**
  `git -C "$WT" status --porcelain` prints nothing both when the tree is clean and when the command
  itself errors (missing directory, corrupt worktree admin entry, unreadable `.git` file, …). A naive
  emptiness test alone would therefore fail *open* on error and reclaim anyway — exactly the "the
  proxy reads the wrong way at the edge" mistake this decision exists to close. The guard captures the
  command's own exit status separately from the emptiness test
  (`STATUS=$(git -C "$WT" status --porcelain 2>&1) && [ -z "$STATUS" ] || continue` — a command
  substitution assignment inherits the command's exit code), so an error is treated identically to
  "dirty": skip, keep the worktree.

  **Accepted residual, unchanged from before this fix:** a *clean* worktree at trunk HEAD that raises
  no lock — a human's hand-made worktree they happen to be sitting in, or an exited agent's clean
  leftovers — is still reclaimed by the ancestry+clean predicate; nothing is destroyed (the tree is
  clean), the directory simply vanishes out from under whoever is standing in it. This does *not*
  extend to a **live** harness agent's worktree, which the harness locks for the agent's lifetime (see
  the paragraph below) and which the backstop's `!locked` filter therefore drops before the predicate
  is ever evaluated. The failure direction this decision moves the whole backstop to is "remove an
  empty checkout, never destroy uncommitted work" — that trade is intentional and not being chased
  further here.

  **Open, not settled by this ticket [Likely, not Certain]:** whether a hard crash of the Claude Code
  host process leaves stale pid-keyed `git worktree lock`s behind. If it does, the harness's own lock
  (held for the lifetime of an `isolation: worktree` agent, released on exit — verified live against a
  reviewer's own worktree during lode-amif's review) would cause backstop 1 to skip those worktrees
  entirely, and the crash-mid-fan-out leak lode-amif targets would still not be reached by this
  backstop (though the dirty-tree guard above would still hold if the crashed worktree also happens to
  be dirty, which is the common case for a build that crashed mid-edit). Confirm empirically if this
  ever needs chasing further; not a blocker for this decision.

- **`/land`'s per-ticket worktree GC loop was DELETED, not guarded — the generalized backstop now owns
  all local worktree/branch reclaim (lode-h1vn, decided/built 2026-07-14; folds in lode-ux1n's
  acceptance criteria as a duplicate).** lode-9hgu (above) added a dirty-tree guard to the *generalized*
  backstop but deliberately left `/land`'s separate **per-ticket** GC loop (`.claude/skills/land/SKILL.md`
  Section 4, ~line 539 before this change) untouched — it read `metadata.review_worktree`/`review_branch`
  off each just-landed ticket and ran `git worktree remove --force` unconditionally, with neither a
  `locked` check nor a dirty check, sharing the exact destructive primitive and risk class the
  generalized backstop had just been guarded against. lode-9hgu's own justification for the exemption
  ("the content is provably on trunk already") answered a different question than the one that
  matters: that claim is about the *branch tip* that merged, not the *current working-tree state* of
  that same directory at GC time.

  **The ticket named three options:** (1) add lock+dirty guards to the per-ticket loop; (2) delete the
  loop outright and let the generalized backstop own all worktree GC (flagged by the ticket itself as
  "probably right," since the backstop's candidate set already subsumes the per-ticket loop's — same
  `.claude/worktrees/` path prefix, and a landed ticket's worktree HEAD is by construction an ancestor
  of trunk the moment this pass's `--no-ff` merge lands); (3) accept and document.

  **MEASUREMENT BEFORE THE DECISION (lode-ux1n's AC1, binding):** the standing worry — stated explicitly
  in lode-9hgu's own decision record — was that a dirty-tree guard here could *silently no-op the entire
  per-ticket cleanup*, because this repo's beads pre-commit hook re-exports and re-stages
  `.beads/issues.jsonl` on every commit, and a producer's/reviewer's final `bd update` (labels, claim)
  happens *after* their last commit. If that write routinely left the working tree dirty, a naive
  `status --porcelain` guard would match *every* worktree and convert a cleanup into a permanent leak
  while looking like it worked — a guard that leaks everything is not a guard. This was checked against
  real state rather than reasoned out: `git -C "$WT" status --porcelain` was run against the **real**,
  live builder worktree for lode-amif (`metadata.review_worktree`, post-build, at the time still
  `needs-rebase`) and the **real** reviewer worktree for the same ticket (`land/lode-amif--<dir>`, one
  commit ahead — the reviewer's own fix already applied). **Both came back empty (clean).** Scanning all
  8 worktrees live on the build machine at measurement time, the *only* dirty one was a currently
  **locked**, live, mid-merge-conflict worktree — already excluded by the `locked` check before a dirty
  check would ever run. Root cause of why the feared jsonl-drift scenario doesn't materialize in
  practice: a `bd update` writes to the Dolt DB only; `.beads/issues.jsonl` on disk is regenerated,
  staged, and committed *only* by the pre-commit hook, which fires at commit time, not at bd-write time
  — so a bd write with no subsequent commit leaves the tracked file exactly as it was at the last
  commit (stale relative to the DB, but byte-identical to what git already has), and `git status` reads
  clean. This measurement is what makes option (1)'s guard — and by extension option (2)'s deletion,
  which relies on the *same* already-guarded backstop — safe rather than a leak waiting to happen.

  **RE-VERIFIED AT REVIEW, because this claim is the linchpin and the repo contradicts itself about it.**
  The builder's measurement observed that finished worktrees *are* clean; it did not, on its own, prove
  *why* — and "the file matches HEAD" is equally consistent with a competing explanation (that `bd` does
  churn the export, but something scrubs it). Those two were separated directly: inside an agent worktree,
  a `bd` **read** and then a real `bd` **write** were each followed immediately by `git status --porcelain`
  **in the same shell invocation** — so no `Stop` hook could intervene — and the tree was **empty both
  times**, while the write itself was confirmed to have landed in Dolt. The stated root cause therefore
  **holds**: a bd write goes to Dolt and does not touch the tracked `.beads/issues.jsonl`.

  **But the claim is CONTESTED elsewhere in this repo, and that is now a live coupling rather than
  trivia.** `/land`'s own SKILL.md Section 3 (~line 354) asserts the opposite — *"any `bd` read
  regenerates the passive `.beads/issues.jsonl` export and leaves it **staged**"* — and defensively runs
  `git restore --staged --worktree .beads/issues.jsonl` before every merge; `.claude/skills/release/SKILL.md`
  has a whole section for a recurring lone `M .beads/issues.jsonl`; and `.claude/settings.json` carries a
  `Stop` hook whose entire job is *"Discarding passive bd export churn."* Either those are **stale** (bd's
  behavior changed) or the churn is version/config/path dependent. It matters because the clean-tree gate
  is the **only** thing standing between a finished builder worktree and reclaim, and — since this ticket
  deletes the per-ticket loop — there is no second net and no alarm: if `bd` ever *does* leave a staged
  export in a builder worktree, **every** worktree reads dirty, the sweep reclaims **nothing**, and it
  does so silently. Not resolved here (the deletion is safe under measured behavior); tracked by
  **lode-bns3**, whose deeper fix dissolves the coupling rather than documenting it — exclude the passive
  export from the gate outright, since by invariant (`import.auto: false`, lode-6ra) it is *never* work,
  and every other clean-tree check in the repo already excludes it.

  **RESOLVED by lode-bns3.** Reconciled, not just patched: the generalized backstop's clean-tree gate
  (`SKILL.md` Section 4) now **excludes** `.beads/issues.jsonl` / `.beads/interactions.jsonl` from the
  cleanliness judgment outright (`:(exclude)` pathspecs on the `status --porcelain` guard), so a staged
  or modified export, from whatever cause, present or future, can never zero out the sweep on its own;
  the coupling is *dissolved*, not merely documented. It **excludes** rather than restoring (the build's
  first cut mirrored Section 3's restore; changed at review): Section 3 must genuinely *clean* the index
  or its `git merge --no-ff` refuses to run, whereas the GC loop only needs to *judge*. Restoring would
  have made a read-only judgment **write into candidate worktrees — including the dirty ones it then
  decides to KEEP**, silently discarding their export churn as a side effect of merely looking at them;
  exclusion has zero blast radius, which is the right posture for a loop that ends in `--force`. It also
  sidesteps a trap the restore form walked into: `git restore` aborts *wholesale* on an unmatched pathspec
  and would restore *neither* file (silently, under `|| true`) for a candidate sitting at a commit that
  predates one of these paths — a stale leftover worktree being exactly what this backstop reclaims.
  Separately, Section 3's own claim ("any `bd` read regenerates … and leaves it
  staged") turns out to be the overstatement that produced the apparent contradiction in the first place:
  measured a third time, independently (a bare `bd show`/`bd ready` **and** a real `bd update` write),
  a worktree still reads clean — bd writes go to Dolt, and the tracked jsonl is regenerated+staged by
  the **pre-commit hook at commit time**, not at `bd`-call time. Section 3's text is corrected
  accordingly, and its restore is **kept**, not deleted.

  **The positive cause remains UNESTABLISHED, and the record says so deliberately.** The obvious next
  move — name `bd dolt pull` as "the real cause" and move on — was tried and **rejected at review**:
  bd-sync discipline names `bd dolt pull` only as an explicit *defensive assumption* ("on the assumption
  it may be staged even when `git diff` says otherwise"), never as a measurement, and a direct attempt to
  reproduce it staged nothing. Replacing one confidently-wrong causal story with another — in prose about
  a `--force`-wielding loop — is the very defect this entry exists to close, one step removed (the
  lode-9i2p pattern: inventing a plausible machine-level cause is worse than an admitted gap). Crucially,
  **nothing depends on the answer**: the export is by invariant never work (`import.auto: false`,
  lode-6ra), so restoring it unconditionally is correct *whatever* the trigger is — which is precisely
  why the restore is the right fix for an unestablished cause, rather than a reason to keep hunting one.

  Verified end-to-end by **executing the loop** against a synthetic five-worktree fixture set (not by
  reading it): a worktree whose only dirt is a staged `.beads/issues.jsonl` is now correctly reclaimed
  (previously silently skipped — the exact regression this entry flagged); one with genuine uncommitted
  content is still correctly KEPT (exclusion narrows what counts as dirt, it does not mask real dirt);
  not-merged and locked worktrees are still kept; and the buckets partition the candidates exactly.

  The sweep also now emits one summary line per pass (`worktree GC: reclaimed X of Y candidate(s) …
  (skipped: locked=.., not-merged=.., dirty=..; failed=..)`), so a regression that zeroes out GC reads as
  visibly different from "nothing to do," closing the observability gap this entry also flagged. `failed`
  is a bucket of its own because the count is taken from `git worktree remove`'s **actual exit status**:
  incrementing `reclaimed` merely for *attempting* the removal (the build's first cut) would let the
  summary report "reclaimed N" when every removal had failed and nothing was reclaimed at all — the
  observability feature lying in precisely the direction it exists to expose.

  **Also caught at review — a `--force` hole the observability change itself opened.** Counting the
  `locked` skips meant moving the `locked` test out of `awk` (where it had been a filter) into the shell
  loop, reading four tab-separated fields. Tab is IFS *whitespace*, so `read` collapses adjacent tabs and
  does **not** preserve an empty *middle* field — and `branch` is empty for a **detached** worktree (a case
  the loop explicitly supports). A detached worktree's line therefore shifted every field left: `$BR`
  swallowed the locked flag, `$LOCKED` read empty, `[ "$LOCKED" = "1" ]` was false, and a **locked, live
  agent's worktree would have sailed past the locked gate into `git worktree remove --force`** — the exact
  rip-it-out-from-under-a-running-agent harm of the pre-lode-oqr disaster, reopened by a change whose only
  goal was observability. Fixed by ordering the fields so the one possibly-empty field (`branch`) is
  **last**, where an empty value is a harmless trailing delimiter. Recorded because the lesson generalizes:
  a purely "additive" observability change reached into a destructive gate's control flow, and the bug was
  invisible to reading — it took executing the actual `read` against a synthetic detached-worktree line.

  **Chose (2), not (1).** Once the measurement ruled out the silent-no-op risk, (1) and (2) are safe in
  the same way, but (2) is strictly simpler: no new guard to write, test, or keep in sync with the one
  the generalized backstop already carries, and no per-ticket `review_worktree`/`review_branch` metadata
  trust (which can drift) where the backstop already does better — it discovers the worktree and its
  branch directly from `git worktree list --porcelain`, live, every pass. The `git push origin --delete
  "land/$id"` remote-branch cleanup that lived in the same per-ticket loop was **kept**, unchanged and
  still per-ticket: deleting a remote ref carries none of the destroy-uncommitted-work risk this ticket
  is about, so it never needed a guard and isn't the generalized backstop's job (it doesn't touch remote
  refs at all).

  **Verification that the cleanup is not neutered (lode-ux1n AC3):** since no ticket happened to land
  during this build to observe an actual reclaim, the exact backstop predicate (`locked` → skip;
  `merge-base --is-ancestor $SHA trunk` → keep if false; `status --porcelain` → skip if non-empty or
  erroring; else reclaim) was evaluated read-only against all 8 live worktrees. It correctly kept every
  not-yet-merged and every locked worktree, and the lode-amif builder worktree — clean, unlocked, and
  blocked from reclaim *only* by not yet being merged (it was mid-`needs-rebase`, not landed) — would
  satisfy every remaining condition (unlocked, clean) the instant its ancestry condition is met by
  landing. No live-fire test of an actual `--force` removal was performed, deliberately: destructively
  reclaiming another concurrent agent's real, in-use worktree to manufacture a positive case was not an
  acceptable price for this verification, and none was needed — lode-9hgu's own landing already
  regression-tested the backstop loop itself (mutation-tested against every acceptance case); this
  ticket only had to confirm the *candidate set* now includes what the per-ticket loop used to cover,
  which the dry-run against real worktrees does.

  **"Subsumes" is true of the CANDIDATE set, NOT the RECLAIMED set — the cleanup is strictly WEAKER
  than before, and that must not be glossed.** The backstop *considers* every worktree the per-ticket
  loop did (confirmed at review: a landed ticket's builder worktree is always under
  `.claude/worktrees/`, is unlocked by then, and its HEAD — the recorded `review_head` — is by
  construction an ancestor of `trunk` once the pass merges the branch; verified against four really
  landed tickets, lode-9hgu/nggm/vs7g/9i2p, all YES). But the backstop then applies three gates the old
  loop did not — `locked`, clean-tree, and HEAD-ancestry — so what it actually *removes* is a strict
  **subset** of what the old loop removed. Worktrees **can now accumulate in cases where they
  previously did not**. Three cases, all failing in the safe direction (leak a directory rather than
  destroy uncommitted work):
  1. **DIRTY** — a landed builder worktree with uncommitted state is now **kept**; the old loop
     force-removed it. This is a **permanent** leak, not a deferred one: the tree stays dirty, so every
     later pass skips it too and nothing else ever reclaims it — it needs a human to inspect and remove.
     Measured to be rare (see the measurement above), and keeping it is the *correct* call: a dirty tree
     holds work captured nowhere else. This is the trade lode-9hgu already made; it is only *new* here
     in that the per-ticket loop used to be exempt from it.
  2. **LOCKED** — a landed ticket's worktree that is `locked` at GC time is no longer force-reclaimed;
     see the dedicated paragraph below.
  3. **NOT-ANCESTOR** — a landed ticket's builder worktree whose HEAD carries commits that never
     reached `origin/land/<id>` is now kept. Strictly speaking this one is a **bug fix wearing the
     costume of a regression**: the old loop removed such a worktree *and* `git branch -D`'d its branch,
     destroying those unpushed commits outright. It is listed here only because it is a case where GC no
     longer reclaims — nobody should "restore" it.

  Note the weakening is intrinsic to the **guards**, not to the deletion: option (1) — add lock+dirty
  guards to the per-ticket loop — produces the identical weakening. Only option (3), accept+document,
  preserved the old reclaim behavior, and it does so by keeping a P2 that destroys uncommitted work.

  **This RAISES the stakes of the `.gitignore` coupling lode-9hgu recorded.** The clean-tree gate reads
  clean on a finished builder worktree *only* because its untracked build junk (`venv/`, `.nox/`,
  `__pycache__/`) is ignored. Un-ignore any one of them and every worktree reads dirty, so the backstop
  reclaims **nothing** — and there is no longer a per-ticket loop as an unguarded second net. A
  `.gitignore` regression now silently disables *all* local worktree GC at once, where before it only
  degraded the backstop.

  **`review_worktree`/`review_branch` were VESTIGIAL at this point — written by `coding.md`, read by
  nobody.** The deleted loop was their only GC consumer; the backstop discovers worktrees live off
  `git worktree list --porcelain`, and `/code`'s own reclaim *derives* its target from the ticket id
  rather than trusting a recorded path (lode-vs7g). The stale cross-references that claimed `/land`'s
  GC "keys off `review_worktree`" were corrected at review across `.claude/skills/code/SKILL.md`,
  `.claude/agents/coding.md`, `.claude/agents/code-reviewer.md`, and `docs/agents-workflow.md` — the
  *guarantee* those docs protect (the builder must not remove its own worktree; `/land` reclaims it on
  a clean land) is unchanged; only the mechanism is. Whether to stop writing them was deferred to
  **lode-2m89** — resolved below, at the end of this file: the fields are retired outright.

  **The `locked`-worktree question, answered (lode-ux1n AC5):** yes, deleting the per-ticket loop means
  a landed ticket's worktree that happens to be `locked` at GC time is no longer force-reclaimed — this
  *is* a real, deliberate behavior change (the old per-ticket loop ignored lock state entirely). Is it
  reachable in practice? **Believed unreachable under the current architecture, not proven impossible:**
  `coding.md` unlocks a builder's worktree immediately after its first commit and never re-locks it, and
  the harness's own lock is scoped to a *live* `isolation: worktree` agent — the producer that built a
  ticket has already exited (its task ended) by the time that ticket reaches `ready-for-code-review`,
  let alone `ready-for-land`. The only way a landed ticket's *builder* worktree could be locked at GC
  time is if some other agent were later re-entered into that exact worktree, which nothing in the
  current design does (every task gets its own fresh worktree). If this residual is ever hit in
  practice, the correct behavior is the one this change produces anyway: leave a locked, in-use worktree
  alone rather than force-remove it out from under whoever holds the lock — per the ticket's own
  framing, "arguably the correct behavior."

  **Why the per-ticket loop and the generalized backstop deliberately used different predicates before
  this change — so a future reader does not "unify" them back into a silent leak by re-adding a
  metadata-trusting fast path:** the per-ticket loop's predicate (`review_worktree` metadata lookup,
  unconditional force) was never a *safety* predicate at all — it was a *convenience* shortcut trusting
  bookkeeping the backstop doesn't need. It looked different from the backstop's guarded predicate
  because it was solving a narrower, seemingly-safer-by-construction problem ("this exact ticket just
  landed, so its exact recorded worktree must be safe") that turned out to share the *same* underlying
  risk (destroy uncommitted work) the moment that assumption's premise — "nothing ever re-dirties a
  just-landed worktree between build and GC" — went unstated and unverified. There is no longer a
  second predicate to keep in sync: this is now the fix, not a design tension to preserve.

- **`review_worktree`/`review_branch` metadata — RETIRED, not merely deprecated (lode-2m89, follow-up
  to lode-h1vn above).** lode-h1vn deleted `/land`'s per-ticket worktree-GC loop, the last consumer of
  these two bd metadata fields; every remaining reader was already gone by construction — the backstop
  sweep discovers worktrees live off `git worktree list --porcelain`, `/code`'s own reclaim derives its
  target from the ticket id rather than trusting a recorded path (lode-vs7g), and the code-reviewer
  reads only `review_head` (live — it's what it actually checks out and diffs for drift). Confirmed by
  grep at the time this ticket was built: zero readers of either field anywhere in `.claude/` or
  `docs/`.

  **Decision: (a) stop writing them**, not (b) keep them as documented forensic bookkeeping. Two fields
  maintained forever for a dead consumer is exactly the kind of orphaned machinery the "simplest thing
  that works" principle argues against, and the risk the ticket named — a future reader "restoring" a
  phantom consumer because the fields are still there, looking load-bearing — is real precisely because
  they *were* load-bearing once, under the deleted per-ticket loop. Retiring the write is the more
  legible signal: no field, no expectation. `review_head` stays exactly as-is — it is live and this
  ticket does not touch it. Its two real readers, named precisely so this entry does not repeat one
  field over the very mistake it records: the **code-reviewer** (it is what the reviewer checks out and
  compares against for drift, `code-reviewer.md`) and **`/code`'s step-1 stranded-review guard**, which
  refuses to dispatch a reviewer at a ticket whose `metadata.review_head` is empty
  (`.claude/skills/code/SKILL.md`, lode-k5e/lode-t83). **`/land` is *not* a reader** — its 2a drift
  precheck reads `land_head` (written by the code-reviewer and refreshed by a rebase pickup);
  `.claude/skills/land/SKILL.md` never mentions `review_head` at all.

  **What changed:** `coding.md`'s hand-off (step 9, both the green path and the build-time-escalation
  path) no longer calls `--set-metadata review_worktree=…` / `--set-metadata review_branch=…` — only
  `review_head` is written. `code-reviewer.md`, `.claude/skills/code/SKILL.md`, and
  `.claude/skills/land/SKILL.md` had their cross-references updated: passages describing the fields as
  "still recorded" or "vestigial-but-written" now say they no longer exist; passages describing the
  *historical* per-ticket loop that used to key off them are left as history (they are still accurate
  descriptions of what the old, deleted loop did). The worktree-must-not-be-removed-by-its-builder
  invariant, and `/land`'s backstop-sweep-reclaims-it-on-a-clean-land invariant, are both **unchanged**
  — this ticket only removes a write nobody was reading, not any part of the reclaim mechanism itself.

- **`jq` is a hard prerequisite, and both `PreToolUse(Bash)` jq-shelling guards now FAIL CLOSED
  when it is missing, rather than silently falling through (lode-oii9).** `jq` was never documented
  as a dependency anywhere — not `docs/onboarding.md`, not `CLAUDE.md`'s new-machine-setup steps, not
  `scripts/python-init.sh` — yet the two committed `PreToolUse(Bash)` guards in
  [`.claude/settings.json`](../.claude/settings.json) both shell out to it: the `bd create --deps
  blocks:` inversion guard (`lode-ij24`) and the external-tracker write guard (`lode-o29m`). Each has
  the shape `CMD=$(jq -r '.tool_input.command // empty'); ...`. With `jq` absent, that command
  substitution silently yields empty output, nothing matches the guard's regex, and the hook exits 0
  — the guard falls through with **no signal at all**. Verified live during `lode-o29m`'s own land
  review: with `PATH=/nonexistent`, `gh issue create --title x` was **not** denied. Both guards' own
  test suites (`tests/test_bd_deps_guard.py`, `tests/test_gh_write_guard.py`) `skipif jq is None`, so
  on a jq-less machine the tests do not fail either — they silently skip. Nothing anywhere goes red.

  **Decision: FAIL CLOSED, for both guards, consistently.** When `jq` is unreachable on `PATH`, each
  hook now denies the Bash call outright — before it ever tries to parse `tool_input.command` — with
  a `permissionDecisionReason` naming `jq` as the missing prerequisite and pointing at
  `docs/onboarding.md`. This is a broader denial than "just the guarded pattern": with `jq` missing,
  the hook cannot classify the command *at all*, so there is no narrower-but-still-safe deny to fall
  back to; the choice is between denying every Bash call or falling back to today's silent no-op.

  Options considered, per the ticket's own framing:
  - **(a) Leave as-is** (prereq documented, hook stays a silent-fallthrough backstop) — rejected. The
    entire point of these two guards is to catch an agent that is *not* going to stop itself — an
    obedient agent following a ticket that says "ask upstream" (`lode-s1uz`) or emitting an inverted
    `bd create --deps blocks:` (`lode-ij24`). A guard whose failure mode is silence is invisible
    precisely in the unsupervised case it exists to cover; nothing short of an actual deny reaches an
    agent that would not otherwise notice.
  - **(c) Loud warning, but allow** — rejected. A warning written to hook stdout/stderr is not
    guaranteed to reach a human in an unsupervised producer/reviewer pass (the whole scenario `lode-
    o29m` was filed to close), and an agent has no standing instruction to *stop and read hook output*
    the way it does for an actual `deny`. A warning that nothing downstream reads is functionally the
    same as silence.
  - **(b) Fail closed** — **chosen.** The named downside — "a missing prereq then blocks EVERY Bash
    call" — is real but bounded and self-correcting: `jq` is now a one-line, documented prerequisite
    (`docs/onboarding.md` §Prerequisites, `CLAUDE.md` §New machine setup step 0), install-and-retry
    fixes it permanently for that machine, and the failure is **loud and immediate** — the very first
    Bash call after a fresh, broken-onboarding clone fails with a message naming the exact missing
    tool and the doc to read, rather than the guard quietly doing nothing for the life of the session.

    **The remedy is only performable from outside Claude Code, and the deny reason says so.** Fail
    closed means *every* Bash call is denied while `jq` is missing — including `apt-get install jq`
    itself. A deny reason that said only "install jq and retry" would therefore walk an agent into an
    infinite loop: it would attempt the install through `Bash`, be denied with that same message, and
    retry. Both hooks' reasons instead name the install commands, state explicitly that the guard
    denies every Bash call *including the install*, and instruct an agent to surface the problem to
    the human and stop. This is the one sharp edge fail-closed genuinely has, and it is a wording
    obligation on the deny path, not an argument against the policy — a human installs `jq` in their
    own terminal in one command.
    That is a strictly better failure mode than a security-relevant check disappearing without a
    trace. Because `jq` is genuinely trivial to install (`apt-get install jq` / `brew install jq` /
    `choco install jq`, no compilation, no config), the "worse dev experience" risk named in the
    ticket's own design text is judged not to materialize in practice: a correctly-onboarded machine
    never reaches this branch at all — it is dead code on every machine that followed the onboarding
    doc, and an alarm exactly once on any machine that did not.

  **Consistency across both hooks, deliberately (AC3).** They share the identical `jq` dependency and
  the identical risk shape (a security-relevant guard silently defeated), so they get the identical
  policy — divergence here (one fails closed, one stays silent) would just relocate the same
  unnoticed gap to whichever guard chose to stay soft. Implementation: `tests/test_bd_deps_guard.py`
  and `tests/test_gh_write_guard.py` each gained `test_fails_closed_when_jq_is_missing` and
  `test_jq_missing_deny_reason_names_jq_and_points_at_the_fix`, run with `PATH=/nonexistent` against
  the hook exactly as shipped — the same `PATH=/nonexistent` reproduction used during `lode-o29m`'s
  land review to first discover the gap.

  **Not touched: the guards' matching regex / deny-vs-allow table for commands where `jq` IS
  present.** This decision is scoped to the `jq`-availability question only, per the ticket's own
  design text ("Do not change lode-o29m's regex or its guard's matching logic") — the settled
  `lode-o29m` deny/allow surface (tracker-write verbs, the implicit-POST fields, the read-only
  exemptions) is unchanged. **Update (lode-9mbt) — superseded for the matching *shape* only, not for
  the `jq` question.** The `lode-9mbt` entry below inverts that surface from a denylist to an
  allowlist.

- **The `bd create --deps blocks:` guard collapses backslash-continuations, and deliberately
  OVER-matches on `;`/`&`/`|` (lode-m6px, portability fix lode-9gm2).** The guard's regex is
  evaluated per-line by `grep`, and `.` never crosses a newline, so it required `bd create` and
  `--deps ...blocks:` on the same *physical* line. A backslash-continued invocation — the normal
  shape for any real filing with a `--title`/`--description`, i.e. essentially every ticket an
  agent files — puts them on different lines and was silently missed. That is not hypothetical: it
  reached the live DB on 2026-07-17.

  **Fix, as shipped:** collapse literal backslash-newline sequences to a space before matching,
  using **only POSIX sh constructs** — capture the command via `$(jq -r ...)`, then pipe it through
  the classic "join backslash-continued lines" sed idiom: `CMD=$(printf '%s' "$CMD" | sed -e :a -e
  '/\\$/N; s/\\\n/ /; ta')`. This is safe precisely because it merges only what a continuation
  *is by definition* — one logical line — so a real, non-continued newline still separates
  statements and the anchor's per-statement intent survives untouched.

  **The first attempt at this fix (lode-m6px) shipped bash-only syntax and was bounced before
  landing (lode-9gm2).** It used `CMD="${CMD//$'\n'/ }"` — bash's `${var//pat/repl}` pattern
  substitution combined with `$'...'` ANSI-C quoting. Both are bash-only; the Claude Code harness
  runs PreToolUse hook commands under `/bin/sh`, which on Linux is **dash**, and dash rejects that
  line with a hard `Bad substitution` error. Verified live: the instant `lode-m6px`'s merge commit
  entered the working tree, the next Bash tool call errored, and the hook then errored on *every*
  subsequent Bash call — bricking the tool entirely (a hook that errors is not a clean deny; it is
  strictly worse than the fail-open the fix set out to close, and fails the ticket's own AC4 in
  spirit). The build's own `nox` gates and land-review both missed it because their test harness
  drove the hook through `bash -c`, under which the bash-isms work fine — the portability defect
  only surfaces under the harness's actual interpreter. `tests/test_bd_deps_guard.py` now drives
  every case through `/bin/sh -c` (dash) instead, and carries a dedicated sabotage test that splices
  the byte-exact original bash-only line back in and confirms it fails under dash while the shipped
  line does not — closing the "verified only under the wrong shell" gap that let this ship broken
  once already.

  **Rejected: also narrowing the interior `.*` to `[^;&|]*`.** The intent was to stop a match
  crossing a `;`/`&&`/`|` into an unrelated later `--deps blocks:` (a false deny). It was tried and
  **reverted — it introduced a worse, real fail-open.** A regex cannot parse shell quoting, so it
  cannot distinguish a `;` separating two statements from one sitting inside a quoted
  `--description` — and prose containing a semicolon is the *norm* here (this repo's own ticket text
  is full of them). Measured against the hook as shipped: `bd create --title="Fix A; also B" --deps
  blocks:lode-1` and five sibling shapes — including the exact prose-heavy filing the ticket was
  written about — went from **denied** to **falling through**. The narrowing thus re-opened the very
  hole it shipped alongside, on the common shape, to close a contrived one that has never been
  observed.

  The two goals are provably unsatisfiable together with a regex, so the tiebreak is decided by
  `lode-oii9` above, for this same hook: **when the guard cannot evaluate, it denies.** Over-matching
  costs a confusing deny whose own message states the remedy (`bd dep add <new-id> <id> --type
  blocks`) and is recoverable in seconds; under-matching silently corrupts the DB and is caught only
  by luck. The accepted over-matches are pinned as tests (`ACCEPTED_FALSE_DENIES` in
  `tests/test_bd_deps_guard.py`) rather than left as folklore: narrowing the pattern to "fix" them
  turns the prose-with-`;` cases in `DENIED` red, so the tradeoff cannot be silently re-traded. If
  the false denies ever do bite in practice, the answer is a real shell parser (or matching on
  `tool_input` structurally), **not** a narrower regex.

- **The `gh`-write guard (`lode-o29m`) is inverted from a write-verb DENYLIST to a read-only
  ALLOWLIST, default-deny (`lode-9mbt`).** The original guard enumerated write verbs and denied only
  those — a **list of verbs, not a category**. `lode-9l3d`'s technical review demonstrated this rots
  empirically, not just theoretically: every `gh` release can add a write verb, and probed live
  against the shipped hook, `gh codespace create|delete`, `gh repo rename`, `gh repo archive`, and `gh
  repo deploy-key add` all fell through even after `lode-9l3d` widened the alternation. The same
  inconsistency predates `lode-9l3d` — `gh repo edit` was denied, `gh repo rename` (same noun, same
  identity, comparable destructiveness) was not — purely because of which verbs someone thought to
  enumerate. `lode-9rim` was filed to widen the alternation again; that ticket is the treadmill this
  decision gets off, and is now **closed as superseded**, not built (reopen it only if this inversion
  is ever abandoned — the gaps it names are real and this is the only thing currently closing them).

  **The asymmetry that settles the direction.** A **false allow** is a public write to GitHub under
  the user's identity — the exact harm the guard exists to prevent, and unrecoverable in the sense
  that matters (the notification already went out; see `lode-o29m` above and `CLAUDE.md` General
  Directive 8). A **false deny** blocks a read; the agent reports it and a human unblocks it in
  seconds. Default-deny puts the cheap failure on the common path — that is the whole argument, and it
  generalizes past `gh` to any external-tracker CLI this repo might add a guard for later.

  **Why this was cheaper than a rewrite:** the guard already maintained an `ALLOWED` table of
  read-only forms as a regression pin against over-matching. Inverting the guard largely meant
  *promoting that table to be the decision*, instead of keeping it as a check against a denylist —
  not inventing a new mechanism from scratch.

  **The hard part: the `api` subcommand**, the one form that is read-or-write depending on flags, and
  therefore cannot be allowed merely by matching a verb. Per `CLAUDE.md` General Directive 8, `gh
  api`'s default with `-f`/`-F`/`--field`/`--raw-field`/`--input` and no `-X` at all is an **implicit
  POST** (`gh api --help`: "adding request parameters will automatically switch the request method to
  POST"). The allowlist therefore allows `api` only on a **positive** read test — an explicit `-X
  GET`/`--method GET` (regardless of whether fields are also present, since fields on an explicit GET
  are gh's documented way to send a query string), **or** no field flag and no explicit non-GET method
  at all (the plain, bodyless form defaults to GET). Any other shape — an explicit non-GET method, or
  field flags with no explicit GET — is denied; getting this backwards (allowing on the mere *absence*
  of a known write verb, which is what a denylist does) would invert the guard's meaning for the
  single most dangerous subcommand it has to classify. Two details of that test are load-bearing, and
  both were tightened during this ticket's technical review:

  - **The method arm enumerates no HTTP verbs.** It denies on the *presence* of any explicit method
    that is not GET, rather than on a `POST|PUT|PATCH|DELETE` list — otherwise the guard would have
    smuggled a denylist back in through the one subcommand that most needed not to have one.
  - **The field arm matches every spelling `gh` actually parses.** `gh` is cobra/pflag, so a shorthand
    flag's value may be *attached* with no separator: `-ftitle=x` **is** `--raw-field title=x` (check
    it against the binary — `gh issue list -L0` fails with `invalid limit: 0`, i.e. the value parsed,
    while `-Z0` fails with `unknown shorthand flag`). The first cut of the allowlist required a space
    or `=` *after* `-f`, and so allowed `gh api repos/o/r/issues -ftitle=x -fbody=y` — a real,
    ordinary, issue-filing implicit POST under the user's identity. The old denylist allowed it too;
    it had simply never been probed. This is the whole ticket's central claim in miniature: the
    dangerous spelling is not the exotic one, it is the *documented* one nobody tested.

  **The escape hatch is the human, out of band — deliberately no bypass token, env var, or "just this
  once" flag an agent can invoke.** That would hand the whole guard back the moment an agent decided
  it needed it. If a legitimate read is denied, the correct behavior is: the agent surfaces it and
  stops; a human edits the allowlist. Same wording pattern as `lode-oii9`'s fail-closed deny reason
  above — name the remedy, tell the agent to surface and stop, never tell it to retry.

  **The accepted cost: some false denies early, by design, not a defect.** The read surface is small,
  stable, and enumerable (`view`/`list`/`status`/`checks`/`diff`); the write surface is none of those
  things. A `gh` read form nobody has used yet against this repo (e.g. `gh gist list`, `gh repo
  deploy-key list`) is denied until a human widens the allowlist — that is the design working exactly
  as intended, the mirror image of the old denylist's failure mode (a write form nobody had *listed*
  falling through unnoticed). The allowlist was seeded from the `ALLOWED` table that already existed
  in `tests/test_gh_write_guard.py`, plus a sweep of `gh`'s read subcommands, and is meant to widen
  from real denials rather than from speculative completeness up front.

  **What this does NOT close — honest residuals, unaffected by the inversion.** Composes with, and
  does not touch, `lode-oii9`'s fail-closed-without-`jq` probe (both guards keep failing closed when
  `jq` is missing, per the entry above). Three structural gaps remain, the same character as the
  `blocks:` guard's own residuals: **quoted indirection** (`sh -c "gh issue create …"`, or the command
  held in a shell variable — closing this would mean treating a quote as a command boundary, which
  would false-deny this repo's own prose about the rule); **non-`gh` routes** (a raw `curl` against
  a tracker's REST API, a different CLI, a non-GitHub tracker's own tool); and — the one it is easiest
  to overstate away — **`gh` reached from a command position the matcher does not recognize.** The
  inversion is default-deny on the *subcommand*, but the prior question of whether a line is examined
  at all still rests on an *enumeration*: a fixed wrapper list (`env`, `sudo`, `command`, `xargs`,
  `time`, `nohup`, `if`/`then`, `rtk`), a leading `VAR=x`, a path, and gh's global `-R`/`--repo`/
  `--hostname`. `timeout 5 gh issue create`, `nice gh …`, `exec gh …`, `\gh …` and `'gh' …` therefore
  still fall through — verified against the shipped hook, and **identical on the pre-inversion guard**,
  so this is inherited from `lode-o29m`, neither introduced nor widened here. It is left as a residual
  rather than patched, because the only non-enumerating generalization ("allow any leading tokens")
  false-denies the prose cases the `ALLOWED` table pins on purpose; moving that fence is a real trade,
  **resolved in `lode-bxow` (entry below) as a permanent residual**, not a regex tweak to slip into a
  security guard during review. None of the three is a route an *obedient* agent walks — that is the
  line the fence is drawn on. Neither a wider denylist nor a narrower allowlist can see through any of
  them; the inversion does not claim to close them, and saying otherwise would overstate what a
  command-string guard can ever do.

  **Regression and mutation testing (verified, not assumed).** `tests/test_gh_write_guard.py` pins
  every command the *old* denylist denied as still denied under the new allowlist (no dropped deny),
  adds the previously-falling-through verbs (`gh codespace create|delete`, `gh repo
  rename|archive|deploy-key add`) as newly-denied cases, and adds mutation tests that assert reverting
  the inversion turns those specific new denies **red** — not merely that the suite stays green.
  Executed live: reverting to the pre-`lode-9mbt` hook made exactly the expected 7 cases fail (the 5
  unenumerated-verb denies, the reason-text markers naming the new mechanism, and the
  mechanism-discriminating mutation test itself), confirming the tests exercise the allowlist's actual
  behavior rather than a copy of its regex.

- **The `gh` guard's command-position residual (`lode-bxow`) is accepted as a PERMANENT residual —
  the same standing as quoted indirection and non-`gh` routes, not a temporarily-tracked gap awaiting
  a fix.** `lode-9mbt`'s inversion (above) is default-deny on the *subcommand*; the separate, prior
  question of whether a line is examined at all still rests on an enumeration of command-position
  wrappers (`env`, `sudo`, `command`, `xargs`, `time`, `nohup`, `if`/`then`/`else`/`do`, `rtk`, a
  leading `VAR=x`, a path, `gh`'s global `-R`/`--repo`/`--hostname`). Probed against the shipped hook,
  an unrecognized wrapper (`timeout 5 gh …`, `nice gh …`, `stdbuf -o0 gh …`, `exec gh …`) and a
  shell-escaped or quoted binary name (`\gh …`, `'gh' …`) fall through unseen. This predates both
  `lode-o29m` and `lode-9mbt` — neither introduced nor widened it.

  **Why permanent, not a widened enumeration.** Adding `timeout`/`nice`/`stdbuf`/`exec` to the wrapper
  list is the exact treadmill `lode-9mbt` exists to get off — the next release, or the next wrapper
  nobody thought to name, reopens the identical gap. The only non-enumerating generalization —
  matching `gh` at *any* leading command position, dropping the wrapper list entirely — was
  considered and **rejected**: it false-denies the prose cases the allowlist's `ALLOWED` table exists
  to protect on purpose (a `rtk grep "gh issue create" docs/`, a commit message quoting the verb), the
  same trade `lode-9mbt` already declined for quoted indirection. The other considered alternative —
  match `gh` at a command position whenever no quote character precedes it on the segment — trades
  this false-deny class for a different one (paths such as `/home/gh`), with no clear net improvement
  in coverage against an evasion class that risk analysis (below) judges not worth the new false
  denies it would buy.

  **The asymmetry that decides it.** None of these shapes is a route an *obedient* agent walks — a
  builder following its ticket writes the plain form, or the plain form behind the `rtk` prefix, both
  of which are already denied. `timeout 5 gh …`, a backslash-escaped binary name, and the rest are
  deliberate-evasion shapes, the same class `lode-o29m` already accepted as structural residuals for
  quoted indirection (`sh -c "gh issue create …"`) and non-`gh` routes (`curl` against a tracker's
  REST API). Risk is judged **LOW** on that basis, and the fix's downside (new false denies on
  ordinary paths and prose) is judged to cost more than the residual it would close.

  **No code change.** `.claude/settings.json`'s guard is unmodified by this decision; the wrapper
  enumeration in `docs/agents-workflow.md`'s guard section is reworded to state permanence rather than
  imply a pending fix (`lode-bxow` acceptance criterion 2), and the prose `ALLOWED` cases it protects
  (`tests/test_gh_write_guard.py`) are untouched and still pass. If a future `gh` release, or a new
  observed evasion pattern, changes this risk calculus, reopen this entry rather than silently
  widening the wrapper list.
- **`bd-dolt-push.sh` suspicious-DB guard: a backstop, not a fix for a mechanism nobody could
  reproduce (lode-fzau).** A code-reviewer/coding launch worktree was observed, live, with a STRAY
  worktree-local bd DB — bootstrap-hydrated from that branch's committed, passively-lagging
  `.beads/issues.jsonl` (245 issues) instead of resolving to the ONE shared main-checkout DB (404
  issues, authoritative). Because the reviewed ticket happened to postdate the stale jsonl snapshot,
  the write against it failed *loudly* ("no issue found matching …") — the only reason the incident
  was caught. Had the ticket predated the snapshot (the common case), the write would have succeeded
  *silently* against the stray DB, and the reviewer's own next step, `bd-dolt-push.sh`, would have
  published that stale 245-issue DB over `refs/dolt/data`, reverting ~159 issues of real cross-machine
  state.

  A diagnostic dispatched specifically to pin down the mechanism **could not reproduce it**: 10 probes
  (9 live worktrees + 1 deliberate re-run of the ticket's own repro, `GIT_TRACE=1`-confirmed hook
  firing included) all resolved to the ONE shared DB, with `import.auto: false` correctly read from
  the main checkout's `config.yaml` every time — `bd`'s own binary strings confirm `--git-common-dir`-
  based worktree DB-sharing is a deliberate, documented feature (1.1.0 changelog: "Enhanced Git
  Worktree Support — Shared .beads database across worktrees"). **Human decision (recorded in
  lode-fzau's notes): build the backstop (option (c) from the ticket), not a fix for the DB-resolution
  mechanism (option (a)) or a doc mandate to always run bd writes from the main checkout (option
  (b))** — both of those target a failure mode that today looks already structurally prevented by bd
  itself; a fix for an unreproduced mechanism risks being no fix at all, while the backstop would have
  caught the real incident regardless of which mechanism eventually turns out to explain it.

  **What was built:** `scripts/bd-dolt-push-guard.sh`, called once at the top of
  `scripts/bd-dolt-push.sh` — the highest-value chokepoint, since it is the step that actually
  publishes cross-machine. Guarding *every* bd write was considered and rejected: a much larger
  surface for comparatively little extra safety, since a write that lands on a stray DB cannot reach
  another machine until something publishes it.

  **Two limits of that scope, both accepted, both on the record rather than papered over:**

  1. **`bd-dolt-push.sh` is the main publisher, not the only one.** `/challenge` calls `bd dolt push`
     directly, as a *deliberate* exemption from the wrapper (lode-bpl explicitly says not to "fix" it
     by wrapping it: it is human-invoked and interactive, so a failed push is observed in the
     transcript rather than silently stranding a hand-off). So the guard does not cover `/challenge`'s
     publishes. Accepted: that path is interactive and observed by a human, which is the same property
     that earned it the wrapper exemption in the first place. Revisit if `/challenge` ever becomes
     unattended.
  2. **The push guard does not cover the incident's *second* harm.** Publishing a stale DB is the
     larger blast radius, but the ticket describes a local one too: a label swap that silently
     succeeds against a stray DB strands the ticket at `ready-for-code-review` forever, invisible to
     `/land` — silent work loss that needs no push at all, and which this guard structurally cannot
     see. So "harmless until someone publishes" is true of *other machines*, not of the local
     workflow. Accepted deliberately: catching it would mean guarding every bd write (the rejected
     option above), and the /code producer loops already re-read ticket state after writing, so a
     stranded ticket surfaces as a stalled queue rather than as corrupted shared state. Filed as
     **lode-zz7x** to track rather than expand this branch's scope; an explicit "accepted, won't fix"
     is a valid outcome there.

  It refuses (loud stderr, non-zero
  exit, `bd dolt push` never invoked) when EITHER: (1) the resolved `.beads` directory (`bd where
  --json`) carries bd's own `.auto-import-issues.jsonl` marker — direct evidence of exactly the
  hydration-from-jsonl the incident showed; or (2) the current issue count (`bd count --json`) is below
  `BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT` (default 90%) of a local, per-DB-path high-water-mark cache file
  that `bd-dolt-push.sh` itself writes immediately after every successful push. That cache is a
  **network-free proxy** for "wildly below the remote's count": our own last confirmed-pushed count is
  a hard floor, since the real remote's count can only have grown or matched it since — this avoids
  needing to contact the remote on top of what the push itself already requires, satisfying the
  ticket's explicit constraint against gating *every* bd write behind network reachability (the
  constraint is about ordinary bd writes, not about `bd-dolt-push.sh` itself, which already needs the
  network to do its job).

  **Deliberately does not false-positive on a fresh clone / `bd init`** (the other named failure mode):
  `bd init` never calls this script, restores state via `bd dolt pull` (never a jsonl import, so no
  marker is ever created by it), and a freshly-initialized DB has no high-water-mark cache file yet —
  the count check treats "no cache" as "no baseline", not "suspicious", and does not fire. Both checks
  also fail OPEN (do not block) if `bd where`/`bd count` themselves cannot be read, since the real `bd
  dolt push` will surface that failure on its own. `BD_DOLT_PUSH_GUARD_FORCE=1` bypasses both checks
  loudly, for the rare deliberate case (disaster recovery, an intentional bulk prune immediately
  followed by a push). Full mechanism and both env overrides (`BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT`,
  `BD_DOLT_PUSH_GUARD_FORCE`) are in `scripts/bd-dolt-push-guard.sh`'s own header comment — this is
  dev-tooling/workflow config, not application config, so (matching `BD_DOLT_PUSH_MAX_ATTEMPTS` /
  `BD_DOLT_PUSH_BASE_DELAY` just above) it is not duplicated into
  [configuration.md](configuration.md), which is scoped to what `lode` itself reads at runtime.

  **Why the ratio floor is 90% and not 100%, given the count only ever grows.** `bd count` counts
  *every* issue, open and closed alike (verified: 412 total = 393 closed + 19 open), so a `bd close`
  spree — by far the most common bulk operation here — never lowers it. Under ordinary use the count
  is monotonically non-decreasing, which would argue for a 100% floor (refuse *any* decrease). The
  10% slack is deliberate: the only things that legitimately shrink the count are rare, deliberate
  deletions/prunes, and a 100% floor would turn every one of those into a `BD_DOLT_PUSH_GUARD_FORCE=1`
  ceremony — pushing a backstop that should fire ~never toward fail-*annoying*, which is the failure
  mode this guard most needs to avoid (it is defending against something that has happened exactly
  once). The real incident sat at 245/404 ≈ 61%, far under either floor, so the slack costs nothing
  against the case that actually motivated the guard. **Revisit if:** a real stale-DB drop ever lands
  between 90% and 100% (i.e. the slack is what let it through), or if deliberate prunes turn out to be
  common enough that the interactive `FORCE` ceremony is not the right shape.

  **Two things a future reader should not misread:**
  - **Check 2 is not what would have caught the incident.** It is keyed on the *resolved DB path*, and
    a stray worktree-local DB is by definition a path that has never been pushed from — so it carries
    no high-water cache, has no baseline, and check 2 fails open on it. **Check 1 (the marker) is the
    check that catches the documented incident** (the marker was present in the incident's own
    transcript). Check 2 covers the different scenario of one *established* DB path shrinking between
    pushes. Do not drop check 1 as redundant on the theory that check 2 covers the incident.
  - **A malformed `BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT` fails CLOSED, on purpose.** The ratio feeds
    `$(( ))` inside an `if` condition, where `set -e` is suspended, so before this was validated a
    value like `90%` raised a bash syntax error, evaluated false, and let the push through — a guard
    reporting all-clear while doing no checking, from a one-character typo. It is now validated up
    front and refuses, mirroring the rule `bd-dolt-push.sh` already applies to a non-numeric
    `BD_DOLT_PUSH_MAX_ATTEMPTS` ("never succeed without having pushed"): a broken guard *config* must
    never be indistinguishable from a clean DB. Validation sits *after* the `FORCE` bypass, so the
    escape hatch still works with a typo'd ratio in the environment.

  **Known residual, accepted:** the `.auto-import-issues.jsonl` marker, once created, is treated as
  permanently disqualifying for that DB path unless `BD_DOLT_PUSH_GUARD_FORCE=1` is used — there is no
  mechanism to "clear" it short of the override. Given the backstop's rarity of firing at all and the
  override's availability, this was judged acceptable rather than adding logic to distinguish a stale
  marker from a fresh one. **Revisit if:** the marker check false-positives in practice on a DB that
  has since become legitimately caught-up via real `bd dolt pull`s.

  **Both of the guard's inputs are gitignored, and that is load-bearing, not hygiene** (added in
  review; the **root** `.gitignore` — NOT `.beads/.gitignore`, which `bd init` regenerates, so a rule
  placed there would be silently rewritten away; same hazard CLAUDE.md documents for the BEADS
  INTEGRATION markers). The guard reads two files to decide whether to BLOCK a push, and each
  is per-machine state that is actively harmful if it travels: `.bd-dolt-push-guard-highwater` is a
  baseline for one DB path on one machine — committed, it would land on other clones asserting a count
  they never pushed, and a *wrong* baseline in a blocking guard is worse than the *no* baseline the
  guard already handles safely. `.auto-import-issues.jsonl` is worse still: the guard treats it as
  permanently disqualifying, so a committed copy would block **every push on every clone, forever**,
  escapable only via `FORCE`. That file is bd's, not ours, and was *inert* before this guard existed —
  the guard is precisely what makes committing it costly, which is why ignoring it belongs to this
  change and not to bd. Neither was ignored by default (`git check-ignore` matched neither), and
  `.beads/` is exactly the directory CLAUDE.md warns a `git add -A` sweeps into.
- **The stray-DB label-swap stranding residual: accepted, won't fix (lode-zz7x).** `lode-fzau`'s guard
  (above) covers only the incident's *larger* harm — publishing a stale DB over `refs/dolt/data`. Its
  own description names a second harm the guard structurally cannot see: the reviewer's hand-off swap,
  `bd update <id> --remove-label ready-for-code-review --add-label ready-for-land`, succeeding
  *silently* against a stray, worktree-local DB. No push is involved at all, so the guard never runs;
  the ticket is simply stranded at `ready-for-code-review` forever — the label swap that would have
  promoted it never reached the authoritative DB, so `/land` never picks it up. This entry is
  `lode-zz7x`'s own decision record — the reasoning was previewed inside the `lode-fzau` entry above
  (point 2) as "an explicit accepted, won't fix is a valid outcome there"; this makes that outcome the
  actual, recorded decision rather than leaving it implied.

  **Decision: accepted, won't fix.** Not option (a) (a post-write read-back in the producer loops) and
  not option (b) (a dedicated staleness sweep over `ready-for-code-review`) — both from `lode-zz7x`'s
  own list of directions.

  **Why (a) doesn't actually work as stated.** A read-back that re-queries immediately after the write,
  from the same worktree cwd, resolves to the *same* DB the write just landed on. If that DB is the
  stray one, the read-back sees the label present — it confirms the write against the very DB that is
  wrong, not against the authoritative one. The only way a read-back could actually catch this is by
  querying from an *independent* vantage point (the main checkout, by a fixed path, ignoring whatever
  the worktree's own `cwd`-based resolution did) — on *every* producer hand-off, forever, to guard a
  mechanism *observed* exactly once (the original live incident) and *reproduced* zero times across 11
  probes: the 10 in `lode-fzau`'s diagnostic (9 live worktrees + 1 deliberate re-run of the repro), plus
  an 11th taken live from this ticket's own review worktree, where `bd where --json` resolved to the
  main checkout's `.beads` and `bd count` matched the main checkout exactly (421/421), with no
  worktree-local `embeddeddolt` and no `.auto-import-issues.jsonl` marker. That is a permanent tax on
  the hot path of every single build and review for a mechanism nobody can currently make happen on
  demand.

  **Why (b) is not worth building — but not for the reason it first looks like.** The residual has two
  variants, and they differ sharply in how visible they are. Do not collapse them:

  1. **The reviewer-side variant — the one both `lode-fzau` and `lode-zz7x` actually document — is
     already covered, so (b) is not unbuildable, it is *already built*.** When the reviewer's swap to
     `ready-for-land` lands on a stray DB, the authoritative DB is left holding exactly
     `status=in_progress` + label `ready-for-code-review` — an entirely *clean*, targeted signal, not an
     ambiguous one. And `/code`'s step-1 stranded-review sweep already runs precisely that query
     (`bd list --label ready-for-code-review --status in_progress --json`, `.claude/skills/code/SKILL.md`
     step 1), confirms `review_head` is non-empty, and re-dispatches a `code-reviewer` — which re-reviews
     and re-swaps, clearing the stranding. That sweep exists for a different reason (a human's exit-(a)
     re-entry, lode-t83), but it is label-and-status keyed, not cause-keyed, so it catches this variant
     for free on the next `/code` invocation. A dedicated detector would duplicate it.
  2. **The builder-side variant genuinely has no clean signal — which is why no *targeted* sweep is
     designable for it.** If a builder's own hand-off write (`--add-label ready-for-code-review`) is the
     one that lands on the stray copy, the authoritative DB never receives the label at all, and the
     ticket is indistinguishable from one legitimately still `in_progress`. There is nothing to key on
     beyond the generic "this ticket has sat `in_progress` a long time" — already visible to a human (or
     a future `/sweep` staleness check) without new machinery specific to this mechanism.

  So (b) is redundant where the signal is clean, and impossible to target where it is not. Neither half
  argues for building it.

  **Why accepting it is consistent with `lode-fzau`'s own risk posture, not a lower bar.** That entry
  already rejected guarding every `bd` write as too large a surface for too little benefit given the
  local, recoverable blast radius (a stalled ticket, not corrupted cross-machine state). Option (a),
  done correctly, is exactly that rejected shape: a cross-checkout read-back on every ordinary producer
  hand-off, forever, in service of a mechanism that remains unreproduced. Option (b) fails a different
  test, as above. Both land in the same place: the actual harm — a build sits claimed but silently makes
  no further progress — is a symptom an operator (or `/code`'s own fan-out bookkeeping) already has a
  fair chance of noticing on its own.

  **No code change.** No new guard, no new sweep, no change to `.claude/agents/coding.md` or
  `.claude/agents/code-reviewer.md`'s hand-off steps.

  **Revisit if:** the stray-DB mechanism itself is ever reproduced again (even once, on any machine) —
  at that point a real repro exists to design a targeted fix or detector against, rather than guarding
  speculatively; or if tickets are observed sitting `in_progress` with no visible builder/reviewer
  activity for an extended period and no other explanation surfaces, which would be the first empirical
  sign this residual is firing in practice rather than remaining purely theoretical.

- **Atlassian connectors (JIRA + Confluence Cloud, `lode-gpzn`) — locked scope + `/challenge`
  refinements, resolved (owner, 2026-07-17).** Extends the web draw-down connector so it recognizes
  Atlassian **Cloud** links and ingests them via authenticated REST APIs instead of
  trafilatura-scraping a login page. Full write-up: [externals.md](externals.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn);
  knobs: [configuration.md](configuration.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn).
  **Locked scope decisions:**
  1. **Cloud only** — Basic auth (account email + API token), JIRA REST v3, Confluence Cloud REST.
     Data Center / Server is explicitly out of scope. (Deferred, not rejected — see the entry below.)
  2. **Credential resolution: env var primary, `config.toml` fallback.** `LODE_JIRA_TOKEN` /
     `LODE_JIRA_EMAIL` / `LODE_CONFLUENCE_TOKEN` / `LODE_CONFLUENCE_EMAIL` checked first; a matching
     `config.toml` key is an optional fallback. No secret is *required* to live in `config.toml`, but
     may. Feature-flagged per product, default **off**.
  3. **`external_id` is the semantic key** (`JIRA-1234` / a Confluence page id) parsed from the pasted
     URL, not the canonical URL — so a browser permalink, an API URL, and any other id-bearing URL
     form of the same issue/page collapse to one `externals` node.
  4. **Raw `httpx`, no Atlassian SDK** — reuses the existing injectable `Fetcher` seam
     (`lode.webfetch.Fetcher`) for offline tests, exactly as the web connector does.
  5. **Flag off / no credentials ⇒ fall through to the generic web connector** (login page ⇒
     tombstone). A connector activates only when flagged on **and** a token resolves
     (`lode.config.jira_active` / `confluence_active`).
  **Refinements resolved via `/challenge` (owner, 2026-07-17):**
  - **(A) Persisted API-base seam.** Because a semantic `external_id` is no longer itself a
    fetchable URL, the inferred-or-configured API base is persisted on a new `externals.api_base`
    column at link-detection time (one schema migration); the async fetch units rebuild
    `{base}+{key}` from it. A general seam for any future non-URL-keyed connector, not
    Atlassian-specific plumbing.
  - **(C) Shared, connector-neutral fetch-outcome classifier.** The HTTP-status half of the
    fetch-outcome taxonomy is factored into one function, `lode.fetch_outcome.classify_http_status`
    (prerequisite child `lode-gpzn.13`, extracted behavior-preservingly out of `lode.webfetch`) —
    reused by the web, JIRA, and Confluence legs rather than copied per connector. The dead-letter
    hook (`lode.worker._refresh_dead_letter_hook`) is generalized off `SOURCE_TYPE_WEB` the same way.
  - **(D) Backfill mints a fresh semantic external.** A per-connector backfill pass mints a
    **fresh, never-tombstoned** semantic external on first migration and enqueues a **plain**
    refresh; the tombstone-exclusion override (`lode backfill --retry-tombstoned`) is re-run
    idempotency only — it matters solely when the *new* identity's own head snapshot already
    tombstoned on a prior backfill pass, never on the first migration itself. Full detail:
    [externals.md](externals.md#backfill-per-connector-re-draw-down-lode-gpzn9).
  - **(E) Body representation: rendered HTML, the existing extractor.** Both connectors request
    the product's own server-rendered HTML (JIRA `expand=renderedFields`/`renderedBody`;
    Confluence `expand=body.view`) and reuse the existing `lode.webfetch._extract` (trafilatura)
    extractor, rather than writing a bespoke ADF walker or storage-format XHTML parser. Raw JSON is
    kept verbatim as `raw_payload` for anyone who later wants it.
  - **(F) Confluence routes only id-bearing URLs.** Only `/wiki/spaces/{SPACE}/pages/{id}/...`
    routes to the connector; a tiny-link (`/wiki/x/...`) or legacy `/display/{SPACE}/{Title}` form
    carries no page id and falls through to the generic web path, keeping link-detection
    synchronous and network-free. See the deferred-gap entry below for the tracked consequence.

- **Atlassian connectors — deferred, not built (three separate gaps, tracked here).**
  - **Data Center / Server support.** Locked decision 1 above scopes the connector to Atlassian
    **Cloud** only (`*.atlassian.net`-shaped hosts, Cloud REST APIs, Basic auth with an API token).
    JIRA/Confluence **Data Center or Server** (self-hosted, a different auth model — PATs or OAuth,
    not the same Basic-auth-with-API-token flow — and a different REST base path) is out of scope
    entirely; a Data Center link falls through to the generic web connector today, same as any other
    non-Cloud host. Revisit if self-hosted Atlassian becomes a real target.
  - **OS-keyring secret storage.** Credentials resolve env-var-primary with an optional
    `config.toml` fallback ([externals.md](externals.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn),
    `src/lode/config.py::_resolve_atlassian_credentials`) — a `config.toml`-stored token sits in
    **plaintext** on disk, the same as every other runtime knob. There is no integration with an
    OS-level credential store (macOS Keychain, the Secret Service API / GNOME Keyring, Windows
    Credential Manager, …). This is a deliberate simplest-thing-that-works choice, not an oversight:
    it mirrors every other secret-shaped knob this codebase already has (e.g. `jira_token`/
    `confluence_token`'s `secret=True` only controls display, not storage) and a cross-platform
    keyring dependency is real added surface for a single-user, single-machine tool. Revisit if
    multi-user/shared-machine use, or a real credential-leak concern, makes plaintext-on-disk
    storage genuinely insufficient.
  - **The id-less-Confluence gap is a known, tracked scope boundary, not a bug.** Refinement F above
    means a Confluence tiny-link (`/wiki/x/AbCdE`) or legacy display URL
    (`/display/{SPACE}/{Title}`) never routes to the structured connector — resolving either to a
    page id would need a synchronous API round-trip that link-detection must not make. Both forms
    silently fall through to the generic web path (login page ⇒ tombstone) on an otherwise-active,
    correctly-flagged-and-credentialed Confluence connector — the *same* outcome as if Confluence
    were flagged off entirely, which is exactly what makes this worth tracking explicitly rather
    than leaving implicit: a user pasting a tiny-link gets no signal that a structured connector
    exists and simply isn't reachable from that URL shape. No resolution mechanism is planned (an
    async two-step "detect as unresolved, defer id resolution to the refresh job" redesign would
    close it, at real complexity cost) unless this proves to matter in practice — revisit if
    tiny-links/legacy URLs turn out to be a common paste shape for real Confluence usage.

- **`tui/` top-level reorg — target layout locked, move discipline + acceptance pinned
  (`lode-zlmz.1`, `/challenge`-resolved 2026-07-19).** Follows the one-Screen/Widget-per-module
  fiat (`lode-s5kp`, [conventions.md](conventions.md#textual-one-screen-or-custom-widget-per-module)),
  which left `tui/` top level a grab-bag of four different kinds of module. This is a
  **decision-only** entry — no code moves here; the move children (`lode-zlmz.3`, `lode-2zj0`)
  build against it.
  - **Target layout (4-way split).** `src/lode/tui/` top level holds **only** `app.py` +
    utilities (`dates.py`, `latency_probe.py`) + `__init__.py`. Custom Widgets — `lode_footer.py`
    (`LodeFooter`) and `related_notes_panel.py` (`RelatedNotesPanel`) — move to a new
    `tui/widgets/`, mirroring the existing `tui/screens/`. The 5 non-UI service modules — `ask.py`
    (`run_ask`), `capture.py` (`save_capture`), `reconcile.py` (`Conflict`/`reapply`), `edit.py`
    (`load_head`/`delete_note`/`save_edit`), `related.py` (`find_related_notes`) — move to a new
    `tui/services/`. **Decided: `tui/services/`, not `lode/services/`** — all 5 import zero
    Textual, so the split is structurally clean, but nothing outside `tui/` actually *imports* any
    of them today (the `cli.py`/`notes_read.py`/`repository.py`/`timestamps.py` references are
    Sphinx `:mod:`/`:func:` docstring xrefs, not real imports), so the minimal collision-killing
    move wins; a broader "`lode/services/` app-service-layer" claim is a bigger claim than the
    evidence supports and is explicitly **deferred, not taken**. This kills the 4 existing name
    collisions: `tui/{ask,capture,edit,reconcile}.py` vs. `tui/screens/{ask,capture,edit,reconcile}.py`
    (disambiguated only by import path today; the `edit` pair is the newest — `screens/edit.py`
    arrived with the `lode-s5kp` browse.py split this reorg follows).
  - **Grouping by kind, not a dependency hierarchy (`/challenge` finding on this ticket, resolved
    by `lode-zlmz.4`).** "`app/screens/widgets/services`" reads like a layered stack, but it isn't
    one, and the decision should say so explicitly rather than let a reader infer a layering that
    doesn't exist. The split above sorts modules into four directories by **what kind of thing each
    module is** (the app shell; a full-screen UI; a reusable UI component; a non-UI service
    function) — it says nothing about which directories may import which. As landed, the
    `widgets/`↔`screens/` edge is **bidirectional**: `widgets/related_notes_panel.py` imports
    `screens.related_note_modal` (widget → screen), while ten screen modules — `ask`, `browse`,
    `capture`, `config`, `edit`, `external_picker`, `reconcile`, `tags`, `version_history`,
    `version_view` — import `widgets.lode_footer` (screen → widget). The move only rewrote import
    paths (per the move discipline below) and introduced no new cycle — the same widget→screen import
    existed at `tui/related_notes_panel.py` before the reorg — but grouping-by-kind is exactly why
    it's unproblematic: nothing about this layout claims or requires widgets to sit "below" screens.
  - **Move discipline (with the challenge carve-out).** Move children relocate class/function
    bodies **byte-identical, except the module/symbol's own import lines and Sphinx**
    **`:mod:`/`:func:`/`:class:` xref path strings** — this is how `lode-s5kp` actually operated.
    A literal "byte-identical bodies, no diff at all" reading is self-contradictory: the moving
    modules carry `:func:`/`:mod:` xrefs to *sibling moving modules* inside their own docstrings
    (e.g. `edit.py::save_edit`'s docstring cites `:func:`lode.tui.reconcile.conflict_from_error``),
    so once `reconcile.py` moves to `tui/services/reconcile.py`, that xref must change too or it
    points at a dead path — a diff in the moved symbol's body that the naive reading would wrongly
    bounce.
  - **Acceptance for the move child `lode-zlmz.3` (replaces the old "~38 files" estimate).**
    Full `nox -s tests` green **and**
    `grep -rn 'lode\.tui\.\(ask\|capture\|reconcile\|edit\|related\|lode_footer\|related_notes_panel\)\b' src tests`
    returns **only** new-path (`services/`, `widgets/`) lines — zero old-path references, whether
    import or xref. The "~38 files carry path refs" figure was an estimate, not a testable
    boundary (the actual count is 54 files touching `lode.tui.*`, including non-moving
    `screens/`/`app.py` files); the grep gate is mechanical and writable as a test.
  - **Sequencing.** `lode-zlmz.3` (the pure move: widgets + services in one pass) lands **first**;
    `lode-2zj0` (dissolving the browse↔edit import cycle via a content-view leaf module) rebases
    onto the new `tui/services/` paths afterward — `lode-2zj0` touches `edit.py` (a mover) and
    `browse.py`, so building it first would force `lode-zlmz.3` to rebase onto a moving target
    instead. The content-view leaf module `lode-2zj0` introduces lives in **`tui/screens/`** (it
    is UI — a navigation-glue leaf imported by the browse/edit screens — not a service).
  - **Why `lode-zlmz.3` is one pass, not two.** The widgets move and the services move both
    rewrite import lines in the same shared screen files (`screens/{ask,capture,edit,reconcile}.py`
    each import both a moving service and the moving `LodeFooter` widget, often on adjacent
    lines). Splitting them into two tickets would be two overlapping import-rewrite passes over
    the same 38–54 files, landing in either order — a guaranteed conflict on the shared files.
    Bundling them into one move avoids that by construction.
- **`land-review` dispatches now MUST run `isolation: "worktree"` — enforce at dispatch, don't patch
  the merge classifier (lode-g387, 2026-07-19).** `/land` runs on `trunk`, in the **main checkout** —
  the same tree its Section 3 batch-merges the accepted set into. `land-review` (its semantic gate,
  first task per branch) used to be dispatched into that same tree with no isolation option at all
  (Agent tool, `subagent_type: "claude"`, nothing else) — so the reviewer ran wherever the lander
  happened to be running: the main checkout, on `trunk`.

  **Observed twice, not once** — a 2026-07-19 pass reproduced an earlier, deliberately-unticketed
  occurrence symptom-by-symptom, which is what promoted it from "plausible one-off" to "systematic."
  The incident and its forensics (why the dirtied tree read as an unretried conflict rather than as
  what it was) are recorded once, in
  [agents-workflow.md — Isolating `land-review` dispatches](agents-workflow.md#isolating-land-review-dispatches-lode-g387);
  what belongs here is the choice it forced.

  **Decision: fix the isolation gap at dispatch, not the symptom in `merge_one`.** The ticket's own
  reasoning is the deciding factor — the repeat across two independent occurrences is evidence the
  cause is *systematic* (a dispatch-time gap), not incidental to one branch's contents, and a
  defensive patch to `merge_one` that recognized "dirtied by something other than the passive
  export" as its own failure class would only make the *symptom* legible; it would not stop a
  reviewer from dirtying the tree in the first place, and the tree it dirties is the one about to be
  merged into. `land-review` is now dispatched exactly like the producer-side agents already are
  (`code/SKILL.md`'s `coding`/`code-reviewer` dispatches, precedent already established): Agent tool,
  `subagent_type: "claude"`, **`isolation: "worktree"` mandatory** — launched already cwd'd inside its
  own `.claude/worktrees/agent-<hash>`, branched from **`origin/trunk`** (`worktree.baseRef: "fresh"`,
  `lode-jzbz`; can lag local `trunk` by however long since `/land`'s last push — usually small, never
  measured), entirely separate from the lander's checkout. `merge_one` itself is untouched by this
  ticket.

  **Costs nothing in capability, needs no new cleanup mechanism.** `land-review` only ever `git
  fetch`es the branch(es) under review and diffs them by ref (never checks anything out —
  [`land-review.md`](../.claude/agents/land-review.md)), so isolation changes *where*
  that happens, never *what* it does. And because `land-review` never commits, its scratch worktree's
  HEAD never diverges from the `origin/trunk` HEAD it was branched from, so the existing worktree-GC
  backstop (lode-h1vn / lode-amif, above) reclaims it under its existing predicate with no dedicated
  code — that pass's own GC step normally, and the next pass that reaches it if this one aborts
  early (see the agents-workflow.md section above for the exact bound). **Enforcement is
  instruction-only, deliberately:** there is no `PreToolUse` guard on agent dispatch, matching how
  the identical `isolation: "worktree"` requirement for `coding`/`code-reviewer` has always been
  specified in `code/SKILL.md`. Whether that class of rule should be mechanically enforced the way
  `gh` writes are (lode-o29m) was **resolved by lode-kt6g** (below): `land-review` moved from a skill
  to a dedicated agent definition (`.claude/agents/land-review.md`) carrying `isolation: worktree` in
  its own frontmatter, so the requirement travels with the role rather than staying call-site prose.
  Documented in [`land/SKILL.md`](../.claude/skills/land/SKILL.md#2c-run-the-semantic-gate),
  [`land-review.md`](../.claude/agents/land-review.md#how-to-use-me), and
  [agents-workflow.md — Isolating `land-review` dispatches](agents-workflow.md#isolating-land-review-dispatches-lode-g387).
- **`land-review` moves from a skill to a dedicated agent definition, carrying `isolation: worktree`
  in its own frontmatter (lode-kt6g / lode-c6ir, 2026-07-20).** lode-kt6g asked a narrow question left
  open by lode-g387 (above): every agent dispatch in the repo that needs `isolation: "worktree"` has
  enforced it as an **instruction at the call site** (an Agent-tool parameter the dispatcher remembers
  to pass) rather than mechanically — an asymmetry against the `gh`-write guard (lode-o29m), which
  *is* mechanically enforced (a committed `PreToolUse(Bash)` hook). lode-g387's own dispatch is the one
  instance that had already **failed twice** from exactly this gap, so the asymmetry was worth
  resolving rather than filing away as consistent-with-precedent.

  **Two options were on the table, and the human picked a third.** (a) a `PreToolUse` hook matching
  the agent-dispatch tool, shaped like the two existing `Bash` guards. (b) explicitly accept
  instruction-only enforcement for this class of rule and record why. **Rejected the hook (a):**
  `.claude/settings.json` grants a blanket `Agent(*)` allow (every subagent dispatch, of every type,
  needs to keep working), `subagent_type` arrives as unverified `tool_input` the hook would have to
  trust, and — the sharpest problem — a fail-tight guard denying a non-isolated dispatch of
  `land-review`/`coding`/`code-reviewer` would have to deny **all** `Agent` calls to stay fail-tight
  the way the `gh`-write guard does (lode-o29m's hook denies outright on a `jq` failure, e.g. missing
  `jq`), which would also deny the very dispatches that *fix* a broken guard — a hook that can brick
  agent dispatch entirely is a categorically worse failure mode than the one it's closing. **Rejected
  plain acceptance (b) too:** instruction-only had already cost two recoverable-but-manual incidents on
  this exact dispatch; recording "we accept the risk" without changing anything about *how easy it is
  to forget the option* would leave the same gap open for the next dispatch that needs it.

  **Why the gh-write default-deny posture doesn't transfer here (the asymmetry lode-kt6g named, now
  answered):** a `gh` write is **irreversible and public** — it files something under the user's real
  identity on a third party's server the moment it executes; no local undo reaches it. A non-isolated
  `land-review` dispatch is **local and recoverable** — the two observed incidents both resolved with
  a human `git reset --hard` and zero data loss, because everything a non-isolated dispatch can dirty
  lives in the lander's own working tree, never off-machine. Irreversibility earns a default-deny
  mechanical fence; a recoverable local mistake earns a structural fix that makes the mistake harder to
  make, which is what frontmatter isolation is.

  **The shape actually chosen:** move `land-review` out of `.claude/skills/land-review/` into a
  dedicated `.claude/agents/land-review.md`, with `isolation: worktree` **and** `model: opus` (a
  related, deliberate pin — land-review is a judgment call, and Opus tokens are worth spending on it,
  matching the peer `code-reviewer` agent) in its frontmatter. This makes the requirement a property of
  the **role** — anyone dispatching `subagent_type: "land-review"` gets isolation whether or not they
  remember to ask for it — rather than a boolean every call site must independently remember, which is
  the exact class of gap lode-g387 fell into twice. `/land`'s dispatch (`land/SKILL.md` §2c) initially
  kept passing the explicit `isolation: "worktree"` call-site option too, deliberately redundant: this
  was the **first** use of agent-definition `isolation` frontmatter anywhere in the repo, so dropping the
  known-working call-site mechanism before one full `/land` pass had confirmed frontmatter isolation
  alone actually launches the reviewer isolated would have risked silently reintroducing lode-g387's bug
  under a corrected-looking dispatch. A follow-up ticket (lode-p2vi) carried the confirmation and the
  drop — though *not* by the "one full `/land` pass" this plan originally anticipated: every real pass
  dispatched `land-review` **with** the call-site option, which can prove nothing either way about the
  frontmatter. It took a dedicated probe with the option deliberately absent.

  **Confirmed and dropped (lode-p2vi, 2026-07-20).** Two dedicated probe dispatches — both with no
  call-site `isolation` argument, differing only in `subagent_type` — isolated the variable cleanly:
  `subagent_type: "land-review"` landed in its own `.claude/worktrees/agent-<hash>`, while the control
  (`subagent_type: "claude"`, otherwise identical) ran in the main checkout on `trunk` — the exact
  lode-g387 hazard. Since the only variable between the two dispatches was the agent definition, the
  isolation is attributable to `.claude/agents/land-review.md`'s frontmatter alone. `land/SKILL.md`
  §2c no longer passes the call-site `isolation: "worktree"` option; frontmatter is the sole
  enforcement point for this dispatch.

  **Deliberately scoped to `land-review` alone — no consistency requirement across `coding` /
  `code-reviewer`.** Those two are *already* dedicated agent definitions (`.claude/agents/coding.md`,
  `.claude/agents/code-reviewer.md`, each pinning `model:`) — what they lack is the `isolation:`
  frontmatter key, so they have carried the identical instruction-only isolation requirement since they
  were written (`code/SKILL.md`) and have not produced this failure. Adding `isolation: worktree` to
  their frontmatter too is not ruled out, but nothing about this decision obligates it, and doing so
  speculatively would be scope this ticket never asked for. Revisit only if a comparable incident shows
  up on one of those dispatches. Documented in [`land-review.md`](../.claude/agents/land-review.md),
  [`land/SKILL.md`](../.claude/skills/land/SKILL.md#2c-run-the-semantic-gate), and
  [agents-workflow.md — Isolating `land-review` dispatches](agents-workflow.md#isolating-land-review-dispatches-lode-g387).

  **Update (lode-ojsr, 2026-07-27) — superseded, deliberately left as-written.** That branch added
  the `isolation:` key to both agent definitions, so "what they lack" above no longer describes
  `trunk`. The correction is the entry below, not an edit to this one; the identical claim in
  `land/SKILL.md` *was* fixed in place instead, because that file documents current operational
  behavior while this one is a dated record of a decision as it stood at the time.
- **The "revisit only if a comparable incident shows up" trigger above fired: `lode-ska2` (6-of-6
  `code-reviewer` dispatches with no worktree, in one fan-out) is exactly that incident, and every
  failure was on the call-site-only mechanism this entry left untouched.** `lode-ojsr` (2026-07-27)
  followed through: added `isolation: worktree` to both `coding.md` and `code-reviewer.md`'s
  frontmatter, matching `land-review.md`, and attempted to probe it the way `lode-p2vi` probed
  `land-review` above — dispatch with no call-site `isolation` option, confirm the frontmatter alone
  provisions the worktree.

  **The probe was structurally invalid, and why is itself the finding.** `lode-p2vi`'s probe dispatched
  from the **top-level orchestrating session** (main checkout, on `trunk`) — the same vantage point
  `/code`'s Phase 2 dispatches `code-reviewer` from, and where the real `lode-ska2` failures occurred.
  `lode-ojsr` is a `coding` producer, bound by this repo's own worktree-isolation rule to never leave
  its own launch worktree — so its probe dispatches were necessarily **nested** inside an
  already-isolated session, not top-level. Three nested dispatches were run, each with no call-site
  `isolation` option: `coding` (frontmatter now present), `code-reviewer` (frontmatter now present),
  and a negative control, `subagent_type: "claude"` (no `isolation` frontmatter key at all, mirroring
  `lode-p2vi`'s control). **All three landed in the identical worktree as the dispatching parent** —
  same `pwd`, same `git rev-parse --show-toplevel`, same branch — including the zero-mechanism control.
  A dispatch with no isolation-granting mechanism whatsoever produced the same outcome as the two
  frontmatter-bearing cases, so the observed isolation is attributable to **nested-dispatch cwd
  inheritance**, not to the frontmatter key — leaving the variable unobservable from a nested vantage
  point, and a genuinely top-level probe the only way to test it.

  **A second, independent reason the nested probe could not have been valid — and the precondition the
  top-level one must satisfy.** Whether a dispatched subagent's definition is resolved from the
  *dispatching session's cwd* or from the *main checkout* is unverified [Likely the latter: nothing
  documented suggests a branch checked out in one worktree can change what `.claude/agents/*.md` a
  dispatch elsewhere reads]. If it is the main checkout, then `lode-ojsr`'s two "frontmatter now
  present" test cases were **not actually frontmatter-bearing at all** — the key existed only on its
  unmerged branch — which invalidates the probe a second time over, independently of the nesting. It
  does not change the conclusion (still untested), but it does constrain `lode-09td`: a top-level probe
  runs from the main checkout on `trunk`, so **the frontmatter must already be merged to `trunk` before
  that probe can mean anything**, and `CLAUDE.md` forbids editing it there directly. That is the
  concrete reason the frontmatter belongs on *this* branch rather than deferred into `lode-09td` —
  deferring it would deadlock the follow-up.

  **The resolution rule does not rescue a nested probe either way.** If definitions resolve from the
  *dispatching cwd* instead of the main checkout, this second reason for the probe's invalidity
  dissolves — `lode-ojsr`'s two "frontmatter now present" dispatches genuinely had the key live — but
  that only reruns the primary finding above with a confirmed-live mechanism, and the outcome was
  still identical to the zero-mechanism control. That invalidates a nested probe *more* conclusively,
  not less. Live frontmatter producing no worktree admits two readings: nesting defeats the key, or
  the key never provisions for these two roles at all. A nested vantage point cannot separate them —
  the only contrast case, `lode-p2vi`, differs in *both* variables at once (top-level **and** a
  different agent). The precondition on `lode-09td` is untouched either way: a top-level probe reads
  definitions from the main checkout on `trunk` under both readings. So confirming the rule only tells
  you whether `lode-ojsr`'s specific probe was invalid once over or twice over, not whether some
  future nested probe could substitute for `lode-09td`.

  **What shipped anyway, and what didn't.** The frontmatter addition ships, and **not** on the strength
  of the probe: the trigger this entry itself recorded — "revisit only if a comparable incident shows up
  on one of those dispatches" — fired, and the change is that revisit. `code/SKILL.md`'s call-site
  `isolation: "worktree"` option for `coding`/`code-reviewer` is **left in place, deliberately** (unlike
  `land-review`'s, dropped above after `lode-p2vi`'s clean confirmation): no clean top-level
  confirmation exists yet for these two roles, so dropping the known-working call-site mechanism now
  would be an unjustified risk.

  **Running both mechanisms at once is safe, on this repo's own evidence — not an assumption.**
  `land-review` carried frontmatter `isolation: worktree` **and** the call-site option simultaneously
  for the whole window between `lode-kt6g` and `lode-p2vi`, deliberately redundant, across *every* real
  `/land` pass in that window (see this entry above: "every real pass dispatched `land-review` **with**
  the call-site option"). The only problem it ever caused was epistemic — it proved nothing about the
  frontmatter, which is why `lode-p2vi` needed a dedicated probe. No double provisioning, no orphaned
  worktree, no altered hand-off was ever observed. So the belt-and-braces posture for
  `coding`/`code-reviewer` is a configuration this repo has already run in anger, not a new risk.

  **This is the genuinely useful negative result the ticket asked to preserve if the confound went
  unconfirmed:** the frontmatter-vs-call-site hypothesis remains untested, not refuted and not
  confirmed, and the harness-side race/resource-pressure hypothesis in
  [agents-workflow.md — Isolation guard](agents-workflow.md#isolation-guard-lode-ska2--lode-jk44)'s
  "Root cause: not determinable" paragraph is unweakened by this probe. A follow-up, `lode-09td`, carries
  the top-level probe design forward — dispatch `coding`/`code-reviewer` and a `claude` control from the
  main/orchestrating session itself, exactly as `lode-p2vi` did for `land-review`. **It is labelled
  `human` deliberately:** no `coding` producer can execute it (a producer is always nested, which is the
  whole finding), so leaving it auto-selectable would have `/code` burn a build+review cycle
  rediscovering exactly that — the second failure mode `code/SKILL.md`'s `human`-label filter exists to
  prevent. Documented in
  [`coding.md`](../.claude/agents/coding.md), [`code-reviewer.md`](../.claude/agents/code-reviewer.md),
  and [agents-workflow.md — Isolation guard](agents-workflow.md#isolation-guard-lode-ska2--lode-jk44).
- **Confirmed and dropped for `coding` and `code-reviewer` (lode-09td, 2026-07-28/29) — the
  frontmatter-vs-call-site confound `lode-ojsr` could not reach is now resolved, the same way
  `lode-p2vi` resolved it for `land-review`.** Two dispatches, run on separate days, both from the
  **top-level orchestrating session** (main checkout, on `trunk`) — not nested inside any dispatched
  subagent's worktree, which is the vantage point `lode-ojsr`'s own probe could not achieve. Both
  dispatches in each run were issued in the same message (concurrent, identical conditions), neither
  carried a call-site `isolation` option, and the precondition (`isolation: worktree` live in the
  target agent's frontmatter on `trunk`) was re-verified with `git show` before each run rather than
  assumed.

  **Probe 1 (2026-07-28), `coding` vs a `claude` control:** `coding` landed in a linked worktree
  (`git rev-parse --show-toplevel` differed from the main checkout, `--git-common-dir` confirmed a
  linked worktree, branch `worktree-agent-<hash>`, `HEAD ==` the dispatching session's local `trunk`).
  The `claude` control —
  carrying **no** isolation frontmatter key at all — landed unisolated in the main checkout on
  `trunk`. **Probe 2 (2026-07-29), `code-reviewer` vs a `claude` control:** identical design, identical
  result — `code-reviewer` isolated, the control did not. `code-reviewer` is the role with the actual
  failure history (`lode-ska2` was 6-of-6 `code-reviewer` dispatches failing to isolate), so this
  closes the more consequential half of the question, not merely the cheaper one.

  **Conclusion:** since the only difference between control and test case in each run was the
  presence of the frontmatter key, the frontmatter key — not nested-dispatch inheritance of a parent
  cwd, which is what made `lode-ojsr`'s own probe invalid — is what provisions the worktree, for both
  roles. Three roles (`land-review` via `lode-p2vi`, `coding` and `code-reviewer` via `lode-09td`) now
  measure frontmatter alone as sufficient, against a keyless control that does not isolate.

  **What this does NOT establish — this entry owns the full text; a citing site must carry at least
  the gist of both limits, and link here rather than re-narrate the probes:**
  (1) each probe contrasts the target role's *whole* agent definition (system prompt, model, tools)
  against `claude`'s, not a single-variable ablation of the isolation key on one fixed definition —
  the key is the only isolation-*relevant* difference, but the probe design does not isolate it in the
  strict sense. (2) **each probe was a single two-dispatch run — one test role, one control, issued
  concurrently — never a fan-out.** They
  establish the mechanism works under light load; they say **nothing** about concurrency pressure, and
  do **not** refute the harness-side race/resource-pressure hypothesis in
  [agents-workflow.md — Isolation guard](agents-workflow.md#isolation-guard-lode-ska2--lode-jk44)'s
  root-cause section. The sharp edge cuts both ways: `lode-ska2`'s 6 failures happened *with* the
  call-site option present, so that option demonstrably did not prevent them either — dropping it is
  not claimed to reduce fan-out risk, only to remove a mechanism with no measured protective effect
  against the one incident it was added for.

  **Decision (human, 2026-07-29): drop `code/SKILL.md`'s call-site `isolation: "worktree"` option for
  `coding` and `code-reviewer`.** Frontmatter is now the sole enforcement point for all three roles —
  `land-review`, `coding`, `code-reviewer` — exactly the same rule everywhere. Reasoning: the call-site
  option was added *for* `lode-ska2` and had zero demonstrated protective value against it; frontmatter
  travels with the role, so every dispatch site benefits, not just `/code`'s; the call site only
  protects the sites that remember to ask for it. Every `code/SKILL.md` dispatch site was updated to
  match, as was `land/SKILL.md`'s then-stale "no top-level probe confirming frontmatter alone
  suffices for them" sentence. This does not touch the open fan-out question above — see limit (2).

  **What makes a single enforcement point acceptable here** (the question a reader should ask, since
  the failure mode — a builder or reviewer writing the main checkout on `trunk` — is unrecoverable):
  the surviving point is *gated* and the dropped one never was, so this removes the ungated mechanism
  and keeps the gated one — the reverse of how "belt and braces" is usually worth defending.
  `tests/test_isolation_guard.py::test_every_agent_definition_pins_isolation_in_frontmatter` fails if
  any `.claude/agents/*.md` loses the key (parsing the frontmatter block, not the whole file — see its
  helper's docstring for why a substring check is not enough), and `scripts/isolation-guard.sh` still
  hard-stops any dispatch that arrives unisolated regardless of *why*. Nothing ever failed on a
  deleted call-site option.
- **Update (lode-nt98, lode-qv5t, 2026-07-20) — the "qualifies by construction" / "no dedicated
  cleanup" claim above is falsified; lode-qv5t closes the gap it left open.** Everything above this
  entry reasoned about `land-review`'s scratch worktree correctly on the axis it was checking
  (correctness — a non-isolated dispatch could dirty the lander's tree) but rested the *worktree-GC*
  claim ("HEAD never diverges … qualifies by construction") on an assumption lode-nt98 falsified after
  this entry was written: the harness's `isolation: "worktree"` hand-off does not reliably start a
  dispatched agent at `origin/trunk` HEAD, and `land-review` shares that dispatch mechanism, so a
  recycled worktree handed to it starts already diverged before `land-review` ever runs and leaks past
  the existing worktree-GC backstop indefinitely — even though `land-review`'s **correctness** exposure
  stays nil throughout (it only ever fetches and diffs by ref; this is purely a worktree-leak fix, kept
  distinct from the correctness question in the canonical account).

  **Fix, mirroring lode-nt98 exactly:** `land-review.md`'s frontmatter role now carries the identical
  guard the builder and reviewer already carry, closing the **ancestry** axis only. Why lode-nt98 had
  not already covered it: it explicitly scoped `land-review` **out** — that same nil exposure read, at
  the time, as *no* exposure at all, and unpicking that conflation is what lode-qv5t was filed for.
  Left open on the
  **dirt** axis — a worktree recycled onto an already-landed `land/<other-id>` still slips past
  undetected — tracked separately as **lode-3v1p**, below.

  Guard predicate, remediation, and full mechanism are canonical in [agents-workflow.md —
  Recycled-worktree guard](agents-workflow.md#recycled-worktree-guard-lode-nt98) / [Isolating
  `land-review` dispatches](agents-workflow.md#isolating-land-review-dispatches-lode-g387); not
  re-derived here. Also documented in [`land-review.md`](../.claude/agents/land-review.md) and
  [`land/SKILL.md`](../.claude/skills/land/SKILL.md#2c-run-the-semantic-gate).
- **lode-3v1p (2026-07-20) closes the dirt-axis residual left open above: `git clean -fd` now runs
  unconditionally at every recycled-worktree guard site, not just inside the failed-ancestor-check
  branch.** The gap and why it's harmless on the ancestry axis but not the dirt axis are covered in the
  canonical account — [agents-workflow.md — Recycled-worktree
  guard](agents-workflow.md#recycled-worktree-guard-lode-nt98); not re-derived here.

  **Three options were on the table; picked the first as the simplest thing that actually closes the
  gap:**
  1. **Run the existing remediation's cleanup arm unconditionally** — move `git clean -fd` out of the
     `if ! merge-base --is-ancestor …` block so it runs every time the guard is reached, pass or fail,
     still gated by the same `.claude/worktrees/`-only `case` that already wraps the destructive branch.
     **Chosen.** It is a one-line move at each guard call site, touches nothing outside the guard
     itself, and needs no new precondition: `git clean -fd` (no `-x`) never removes `.gitignore`d build
     state (`venv/`, `.nox/`, `__pycache__/`), so on a genuinely fresh worktree — the overwhelming
     common case — it is a pure no-op; on an undetected recycle it removes exactly the leftover dirt.
     It also keeps the two concerns cleanly separated: the ancestor check stays a narrow, commit-graph-
     only predicate (never widened into a general clean-tree assertion, which would blur what it's
     actually testing), and cleanup becomes an unconditional, independent step run right after it.
  2. **Have `/land`'s Section 4 backstop sweep judge "recycling dirt" separately from ordinary dirt.**
     Rejected: the sweep has no way to distinguish, from outside, dirt left by a recycled prior
     occupant from dirt that is a live agent's genuine uncommitted work it must never destroy — that
     distinction is exactly why the lode-9hgu dirty-tree guard exists as a blanket "never reclaim a
     dirty worktree" rule in the first place (full account: the lode-9hgu entry, `land/SKILL.md`
     Section 4). Teaching the shared, heavily-audited sweep script a special case for "dirt I should
     actually be willing to nuke" reopens exactly the risk lode-9hgu closed, in the one place (a
     landing-critical, cross-ticket shared script) where a mistake is most expensive — versus a
     one-line, per-site fix that costs nothing shared.
  3. **Have each role assert a clean tree after the guard and escalate/report rather than silently
     clean.** Rejected as unnecessary caution: the worktree being asserted against, at this point in
     each cycle, is understood by construction to be either a genuinely fresh checkout or scratch left
     behind by a *previous*, already-superseded dispatch (the guard runs as the very first action, before
     any of this cycle's own real work begins) — there is no live human or in-flight agent whose work
     could be sitting in that dirt (the `case` precondition already rules out the one class of worktree
     where that could be true, the user's main checkout). The existing failed-ancestor-check branch
     already cleans silently rather than escalating; there is no reason for dirt discovered via the
     ancestor-check-*passed* path to be treated more cautiously than dirt discovered via the
     ancestor-check-*failed* path, since both are the identical class of pre-cycle scratch.

  Documented at every guard site and in [agents-workflow.md — Recycled-worktree
  guard](agents-workflow.md#recycled-worktree-guard-lode-nt98).

- **2026-07-21 (lode-ag7j) — CC 2.1.216 shipped three worktree fixes; recorded as a data point for
  this thread, guards KEPT unchanged.** Verified live on 2026-07-20 (main session on 2.1.216;
  `.claude/settings.json` still carried the undocumented `worktree.baseRef: "head"` at that point).
  None of the three changelog fixes is a confirmed fix for lode-nt98 — the tempting candidate ("land in
  another project's leftover worktree") is framed cross-project, whereas lode-nt98 is same-project,
  same-repo. **Verdict: keep every guard, unchanged** — cheap defensive assertions against a
  catastrophic and irreversible failure mode, and "probably fixed upstream" is not grounds to retire
  on. Sets up a falsification test: watch whether the guard ever fires again on `>= 2.1.216`; if it
  stops firing over a sustained window, file a follow-up to retire the lode-nt98 guard family and
  revisit `baseRef`.

  Full honest-mapping analysis (fix by fix), the verdict's reasoning, and the falsification test are
  canonical in [agents-workflow.md — Recycled-worktree
  guard](agents-workflow.md#recycled-worktree-guard-lode-nt98); not re-derived here.

- **Markdown editing — open items parked in [editing.md](editing.md).** `docs/editing.md`
  (`lode-ev5j`) records the shipped markdown-editing surface but leaves the following unresolved,
  pointered here so a decisions.md sweep surfaces them. Ownership differs per item — only the first
  has a ticket:
  1. **Lint linter choice** — hand-rolled rules vs. `pymarkdownlnt` vs. something else, plus the
     range-granularity question it forces. **Owned by `lode-o7pf`**, which makes this call before
     building anything. See
     [editing.md — Inline lint squiggles, deferred](editing.md#inline-lint-squiggles--deferred-not-built-see-lode-o7pf).
  2. **Whether a custom `.scm` injection query ever ships** to reach inline colouring (emphasis,
     strong, inline code, inline links) — currently block-level only. **Unowned; leaning no** — it
     means hand-building an injection subsystem against Textual's private highlight path, the same
     fragility class this epic already refused. See
     [editing.md — Live syntax colouring](editing.md#live-syntax-colouring--block-level-only-on-four-screens).
  3. **Re-open condition for mouse-clickable links** — not a live question: the concession
     *stands*, and only the grounds for revisiting it are open. Unowned. Any reconsideration must
     start from a real-terminal OSC-8/click test rather than the original "provably inert" framing.
     See [editing.md — Mouse-clickable links: conceded](editing.md#mouse-clickable-links-conceded-in-favour-of-a-keyboard-binding).

- **Full embedder-revision pinning — deferred, not rejected (lode-crh8.1).** `lode-crh8.1` settled the
  embedder's model-provenance data shape and mismatch behavior as **DETECT** (a read-only
  `huggingface_hub.model_info(repo).sha` probe, recorded per passage vector) rather than **PIN** (lode
  pre-materializing weights at a chosen SHA via `huggingface_hub.snapshot_download(repo,
  revision=<sha>)` and handing `fastembed` `specific_model_path`, bypassing its own downloader) — full
  write-up: [storage.md](storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81).
  Pinning is real and achievable (`lode-g274.4`'s FINDING note verified the `specific_model_path`
  lever against the installed `fastembed` source), and is strictly *stronger* than DETECT — it would
  guarantee a fresh install resolves the exact same weights a prior one did, not just warn after the
  fact when it doesn't. **Deferred because the cost is real and ongoing, not one-time:** owning the
  download path means lode also owns bootstrap (what happens before any SHA is pinned), the
  offline/air-gapped fallback story, partial-download recovery, and a deliberate re-pin workflow for
  when the model is intentionally upgraded — none of which `fastembed`'s own downloader currently
  costs lode anything to get. DETECT ships the whole reproducibility/audit story (a per-vector
  manifest, `lode status` drift warning, and a regeneration path via `lode-g274.7`) without taking on
  any of that surface. **Revisit if:** a real, observed silent-drift incident occurs in practice (the
  live cache resolves a different revision than a prior embed used, undetected until well after the
  fact — the failure mode the whole epic exists to close) — DETECT can only warn *after* the drift has
  already happened, whereas PIN prevents it outright; if that gap is ever shown to bite, upgrading
  DETECT to PIN is additive (the manifest/mismatch mechanism is unchanged, only the download path
  gains an explicit pinned-SHA source of truth, which — unlike the runtime-only DETECT manifest —
  *would* become a genuine git-committed build constant, parallel to `_MODEL_CACHE_IDENTITY`).

- **Enrichment LLM: does its default having a dated-snapshot form reopen PIN-vs-DETECT?
  (lode-sdjb).** `lode-g274.5`'s enrichment-LLM decision ([configuration.md](configuration.md#model-provenance-the-enrichment-llm-decided-lode-g2745))
  assumed no PIN axis existed here, because no current-generation Anthropic ID had a dated-snapshot
  form to pin against. That premise is false for `enrichment_llm`'s own default:
  `claude-haiku-4-5-20251001` exists (verified against the `claude-api` skill's model catalog),
  unlike the Q&A tiers' current defaults, which have none. So a pinnable identifier does exist for
  the model whose output actually persists into the DB. Corrected at the source; **unowned, not
  decided here.** Whether to adopt PIN for `enrichment_llm` (recording and verifying the dated
  snapshot rather than just the bare id) is a real design question now that a lever demonstrably
  exists. Note the embedder-pinning entry above defers for a cost that does **not** transfer — lode
  never downloads an Anthropic model, so there is no download path to take ownership of — but a
  nearer one does: a dated snapshot is retired on the vendor's schedule, so pinning one means owning
  that migration, and dated-snapshot availability is not itself stable enough to build a permanent
  mechanism on (a future model swap could remove the option again).
  **Revisit if:** enrichment silent-drift is ever observed in practice (mirrors the embedder entry's
  own revisit trigger), or when `enrichment_llm`'s default model next changes.

- **2026-07-23 (lode-568v.1) — LLMProvider vendor-neutral seam pinned; design-first ahead of any
  provider code.** Full write-up: [stack.md — LLM provider seam](stack.md#llm-provider-seam-decided-lode-568v1).
  Settled so `lode-568v.2` (Anthropic behind the seam) and `lode-568v.3` (OpenAI-via-Azure) build
  against one decided contract rather than inventing it mid-implementation:
  - **Module home:** new `src/lode/llm_provider.py`, not `auth.py` — preserves `auth.py`'s
    deliberately-cheap-to-import property (`lode-4q97`) rather than re-litigating it.
  - **Protocol, not ABC** — matches the existing `Embedder` Protocol precedent the epic body itself
    names.
  - **One generic `structured_call` method** serves both the enrichment (forced tool-use) and Q&A
    (`messages.parse`) immediate surfaces — normalizing *across providers*, not *across call
    surfaces*: `AnthropicProvider` keeps the two existing wire mechanisms **literally distinct**,
    selected by an optional `tool_name` param, rather than asserting the two mechanisms are
    wire-equivalent (a claim this docs-only ticket can't verify, and `lode-568v.2`'s acceptance bar
    is explicitly "byte-for-byte equivalent").
  - **Batch stays two-phase** (`submit_batch`/`collect_batch`); a provider without a batch API
    satisfies it by running every request synchronously inside `submit_batch` and self-encoding the
    already-computed results into the returned handle string — `collect_batch` just decodes it,
    always `"ended"`. No schema/mechanism the caller (`enrich.py`) needs to know about.
  - **Provenance:** a new nullable `annotations.provider` column (not a composite string encoded into
    `annotations.model`) — `NULL` means "anthropic" by convention, no backfill needed. Same treatment
    for `egress_log.provider` (an audit trail's whole point is which vendor content went to). Schema
    migration + write path is `lode-568v.4`'s scope; the read-side staleness-comparison consequence
    (`lode-o9k3`'s `_enrichment_model_stale`) is already split out and tracked as `lode-568v.6`, not
    re-opened here.
  - **Challenge item (a) resolved:** each per-surface tier (`enrichment_llm`/`qa_llm`/
    `qa_think_harder_llm`) becomes a `ModelTier(model, reasoning_effort)` pair rather than a bare
    string, with back-compat coercion from a plain TOML string — this alone (no new abstraction)
    lets "think harder" be a deployment swap, an effort bump on one deployment, or both, per config.
  - **Challenge item (b) resolved:** `anthropic_call_timeout_s` renamed vendor-neutral to
    `llm_call_timeout_s` (same default, same meaning); `load_settings()` gains a back-compat key
    remap so an un-migrated `config.toml` keeps working under `extra="forbid"`.
  - **Error contract:** every provider's failure paths raise `LLMProviderError`/`LLMAuthError`
    (`.provider`, `.status_code`, `.request_id`, chained `__cause__`) rather than a raw SDK exception
    or a generic lode error — addresses the challenge addendum's diagnosability concern for failures
    only observable in a real Azure environment (api-version skew, content-filtering). The concrete
    OpenAI/Azure field mapping is deferred to `lode-568v.3`; only the shape is pinned here.
  - **No code changed by this ticket** — `src/lode/auth.py`, `enrich.py`, `qa.py`, `config.py`, and
    `schema.sql` are all unmodified; every reference above is a *pinned design* for the tickets that
    build it.

- **2026-07-23 (lode-568v.2) — implementation details resolved while building the seam against T1's
  pinned contract; recorded here since they're load-bearing for `lode-568v.3` (OpenAI/Azure) and not
  yet written down anywhere else:**
  - **`structured_call` gained a `tool_description` keyword beyond T1's pinned signature.** Required
    for `AnthropicProvider`'s forced tool-use branch to send the *exact* tool description text
    `enrich._call_haiku` sent pre-seam — byte-for-byte wire equivalence is this ticket's own acceptance
    bar, and the pinned signature had no way to carry it. Same addition on `BatchRequest` for the batch
    path. `lode-568v.3`'s `OpenAIProvider` can ignore it (its Responses-API structured output has no
    per-request description field) or use it as a hint; the field is optional either way.
  - **`BatchResult.parsed` holds the provider's raw decoded wire payload, never a schema-validated
    domain object.** Wrapped in a `pydantic.RootModel[dict]` — literally satisfies T1's pinned
    `BaseModel | None` type — rather than `EnrichmentResult` or any other caller-specific schema. This
    is what keeps `AnthropicProvider` generic (it never needs to import or know about
    `lode.enrich.EnrichmentResult`) while preserving the resume-on-restart durability
    `collect_enrich_batch` depends on (`lode-i05.5`): the Anthropic batch handle stays the bare
    `batch.id` string exactly as before, with no schema information encoded into it, because a fresh
    process's freshly-built `AnthropicProvider` has no in-memory state to lose across a restart in the
    first place. The caller (`enrich.py`) does its own `EnrichmentResult.model_validate(result.parsed.root)`
    — exactly what it did pre-seam, just fed from the provider's raw payload instead of
    `tool_block.input` directly.
  - **`build_provider`'s Anthropic branch does NOT wrap a missing-credential failure into
    `LLMAuthError`** — `lode.auth.build_client`'s `AuthError` propagates unchanged. T1's pinned
    docstring for `build_provider` says it "raises `LLMAuthError`"; this ticket deliberately does not
    implement that for the Anthropic branch, because `lode.worker`'s permanent-failure handling
    (`lode-9yy`, `except AuthError`, dozens of pinning tests) is untouched by this ticket and stays
    correct only as long as the exception type it catches is still what gets raised. Wrapping it would
    have meant either (a) widening every `except AuthError` in `worker.py` to also catch
    `LLMAuthError`, a change with no behavioral upside for an Anthropic-only build, or (b) making
    `LLMAuthError` subclass the Anthropic-specific `AuthError`, which inverts the vendor-neutral seam's
    whole point. Neither was worth doing for a zero-behavior-change ticket. **Follow-up, not resolved
    here:** when `lode-568v.3` (OpenAI/Azure) lands, its own credential failures have no existing
    exception type to preserve, so they should raise `LLMAuthError` for real — at that point
    `worker.py`'s exception handling needs widening (catch both, or retire `AuthError` in favor of
    `LLMAuthError` everywhere). `LLMAuthError`/`LLMProviderError` are still defined now, per the pinned
    shape, ready for that provider to use.

- **2026-07-23 (lode-568v.3) — `OpenAIProvider` implemented; the named acceptance risk from the
  original challenge review, and how it was mitigated as far as this repo can:**
  - **Acceptance is mock-only — named explicitly, as the challenge review required.** No live
    Azure/OpenAI endpoint was reachable from this build. What *was* verified directly: the installed
    `openai==2.47.0` SDK's actual `responses.create()` signature, and the real field shapes of
    `Response`, `Response.incomplete_details` (`IncompleteDetails.reason` is genuinely
    `Literal["max_output_tokens", "content_filter"] | None` — confirmed by introspecting the installed
    package, not assumed), `ResponseOutputRefusal` (`.refusal`, `.type`), and `openai.APIStatusError`
    (`.status_code`, `.request_id`, `.body` all populate as expected from a constructed instance). What
    remains **unverified**: the actual runtime *content* of a real Responses API call/response against
    a live Azure deployment — whether `text.format` `json_schema` with `strict=False` behaves as
    documented, whether a real content-filter rejection actually surfaces via
    `incomplete_details.reason == "content_filter"` vs. a raised `APIStatusError` vs. something else
    Azure-specific, and whether `reasoning={"effort": ...}` is accepted by every deployment this ticket
    might be pointed at. The diagnostic-logging compensating control (`docs/stack.md` "Error contract")
    is the mitigation the challenge addendum asked for: the first real run's failure is diagnosable
    from logs (model/endpoint/api-version, raw payload, content-filter category) even though this repo
    could not exercise the real wire to find failures itself.
  - **`strict=False`, not `True`, for the Structured Outputs `json_schema` format** — a deliberate
    choice, not an oversight. `pydantic`'s `model_json_schema()` does not produce a
    strict-mode-compliant schema (would need `additionalProperties: false` + all-properties-required
    recursively), and transforming it to be so was assessed as its own unverified-against-the-wire risk
    — exactly the class of assumption the challenge review flagged. Non-strict mode is a smaller,
    better-understood risk, and `OpenAIProvider.structured_call`'s own `model_validate` against
    `output_schema` is the real conformance check either way once a typed result is needed.
  - **One wire mechanism for both call surfaces, per the pinned design** — `tool_name`/`tool_description`
    are honored as the Responses API json_schema format's `name`/`description` fields (not ignored),
    but there is no separate function-calling code path; `docs/stack.md` "2 & 3." already pinned this.
  - **Worker exception-handling widened, closing lode-568v.2's tracked follow-up**: `lode.worker`'s
    three `except AuthError` sites (`run_one`, `_batch_submit_enrich`, `drain`) now read
    `except (AuthError, LLMAuthError)` — a missing OpenAI/Azure credential now gets the identical
    permanent, no-retry, no-dead-letter treatment `lode-9yy` already gives a missing Anthropic
    credential. `AuthError` itself is untouched (Anthropic's own credential failures still raise it,
    unwrapped, exactly as `lode-568v.2` left it) — this is purely an addition to the `except` tuple,
    not a change to what Anthropic raises.
  - **Batch handle is a self-encoded JSON blob**, exactly as `lode-568v.1` pinned: `submit_batch` runs
    every request through the same Responses-API call synchronously and encodes the resulting
    `BatchResult`s (success payload or a serialized `LLMProviderError`) into the string returned as the
    handle; `collect_batch` decodes it with no network call, always `("ended", …)`. Note this handle is
    duplicated verbatim across every job row in the submitted set (`enrich.py`'s existing
    `UPDATE jobs SET batch_handle = ?` loop writes the identical string to each row, the same way it
    writes Anthropic's single server-side `batch.id` to each row today) — an accepted, unoptimized cost
    of the degenerate "serialize" strategy, not a bug.
  - **`azure_openai_api_version` required whenever `azure_openai_endpoint` is set**, enforced by a
    `Settings` `model_validator(mode="after")` — fails at config-load time rather than as an opaque
    request failure from the SDK/HTTP layer at the first real call.
  - **`openai` added as a runtime dependency**, `requirements.lock` regenerated via `uv pip compile`
    per `docs/stack.md`'s dependency-locking split. That regeneration also picked up an unrelated
    `numpy` version drift (`2.5.1` → `2.4.6`, confirmed via a side-by-side regeneration against the
    *unmodified* `pyproject.toml` — the same drift happens with no `openai` dependency added at all, so
    it is pre-existing PyPI/resolver drift since the lock was last regenerated, not something this
    ticket's dependency addition caused) — left as-is; hand-editing a generated lock file to avoid it
    would violate the lock's own "regenerated, never hand-edited" rule.
- **2026-07-23 (lode-568v.4) — implementation details resolved while building the provenance write
  path against T1's pinned `annotations.provider` / `egress_log.provider` shape:**
  - **Migration mechanism:** both columns are added the same way every other post-deployment column in
    this schema is (`storage.py`'s `_apply_migrations` forward-only `ALTER TABLE ... ADD COLUMN`,
    guarded against the existing-column `OperationalError`) — no new migration mechanism, and per T1's
    pinned "NULL means anthropic" convention, no backfill `UPDATE` either; every pre-existing row reads
    back `NULL`, which already means anthropic.
  - **`provider_identity(settings)` (new, `lode.llm_provider`)** is the one place the "write `NULL`
    while the active provider is anthropic, else the literal provider string" rule lives — both
    `_write_enrichment` call sites and both `enrich.py` `log_egress` call sites compute it from this
    single function rather than re-deriving `settings.llm_provider == "anthropic"` at each site.
  - **Local variable named `provider_name`, never `provider`, at every enrichment call site.** All four
    functions that needed this value (`enrich_version`, `submit_enrich_batch`, `collect_enrich_batch`,
    and `_write_enrichment`'s caller-side computation) already bind `provider` to the
    :class:`~lode.llm_provider.LLMProvider` *instance* (the seam object T2 introduced) — reusing that
    name for the provenance string would shadow it. `provider_name` is the identity string;
    `_write_enrichment`'s own parameter is still named `provider` since that function has no seam
    object to collide with.
  - **`egress.log_egress` gained an optional keyword-only `provider: str | None = None`,** not a
    required positional — the Q&A send (`gate_qa_egress` → `log_egress`) is explicitly out of this
    ticket's scope (see T1's note above: "the read-side staleness-comparison consequence is already
    split out ... not re-opened here" applies symmetrically to the write side for Q&A, which this
    ticket's own scope text never named). Leaving Q&A's call site unchanged and defaulting to `None` is
    also the *correct* value today, not just a scope dodge: Q&A is Anthropic-only regardless, and `NULL`
    is exactly what `provider_identity` would have computed for it.
  - **Not done here (deliberately deferred to `lode-568v.6`):** no read-side consumer — `lode status`,
    `lode reenrich`, or `_enrichment_model_stale` (`lode-o9k3`) — was touched. This ticket is schema +
    write path + migration only, per its own acceptance criteria; a provider switch on an unchanged
    model string does not yet mark the corpus stale.

- **2026-07-23 (lode-568v.6) — read-side provider-aware staleness implemented, closing the gap
  `lode-568v.4` left open:**
  - **`_STALE_ENRICHMENT_LIVE_HEADS_SQL`'s per-branch mismatch predicate becomes `(a.model != ? OR
    a.provider IS NOT ?)`**, not two independently-OR'd checks — `IS NOT`, not `!=`, for the provider
    leg, and deliberately in both directions: a stored `NULL` means "anthropic" by convention
    (`lode-568v.4`), and the current provider passed in is itself `NULL` while the active provider is
    anthropic. A plain `!=` against a NULL operand in SQLite is never true, which would silently
    exempt the anthropic-vs-anthropic comparison — the common case today, before a second provider
    ships — from ever resolving correctly either way.
  - **Both `_stale_enrichment_heads` and `_enrichment_model_stale` gained a required
    `current_provider: str | None` parameter** — no default — so every call site names its intent
    explicitly rather than silently falling back to "anthropic." Both `lode status` and `lode
    reenrich` pass `lode.llm_provider.provider_identity(settings)`, never `settings.llm_provider`
    directly, keeping the write-side and read-side convention identical by construction rather than by
    two independently-maintained call sites agreeing to use the same string.
  - **No user-facing wording change.** `lode status`'s hint text and `lode reenrich`'s summary line
    still read "disagree with the currently configured enrichment_llm" — accurate as shorthand for
    "enrichment identity," and changing it risked nothing but test churn for no behavioral gain; the
    docstrings on both functions spell out the provider leg for anyone reading the code.

- **2026-07-29 (lode-sx17) — the test network guard stopped depending on luck; `HF_HUB_OFFLINE`
  declined, `HF_HUB_DISABLE_TELEMETRY` adopted.** `tests/conftest.py`'s autouse guard (lode-85q)
  failed a test that touched the network by raising `pytest.fail()`, whose `_pytest.outcomes.Failed`
  is a `BaseException`. That choice was made deliberately, to clear `lode.worker.run_one`'s
  `except Exception` — but it was *also* doing unplanned work: clearing third-party best-effort
  swallows it was never designed for. `huggingface_hub`'s `utils/_detect_agent.py` fetches an
  agent-harness registry from the Hub while building the headers for **every** Hub request, and
  wraps the load in `except Exception` (with a second one inside the fetch), on the stated contract
  that "detection must never make a process fail". A `BaseException` clears both today; nothing
  obliges them to keep it that way, and one `except BaseException` on their side would have made
  that egress permanently **invisible** rather than merely non-failing.
  - **Decided: two independent layers, not one.** Cutting the known egress site and hardening the
    guard are different jobs — the first removes an egress, the second removes the *class* of
    silent failure — and doing only the first would have left the next such library site to be
    discovered the same way this one was (by reading an installed package, off the back of an
    unrelated review).
  - **Layer 1, source cut — `HF_HUB_DISABLE_TELEMETRY=1`, adopted.** Set **process-wide**, at
    `tests/conftest.py` module level, so it lands before anything imports the hub.
    `@pytest.mark.network` deliberately does **not** lift it, for two reasons: it *cannot* (the hub
    freezes the env into a module constant at import, so a per-test `monkeypatch.setenv` is a no-op
    against an already-imported hub — the same import-time-freeze trap as rich's `Console`,
    lode-kq4v), and it need not — the var suppresses telemetry, not Hub access, so a `network`-marked
    test that genuinely needs a Hub call still works with it set.
  - **`HF_HUB_OFFLINE=1` — declined.** It would also stop the registry fetch, but it disables the
    Hub outright, breaking two things that reach it legitimately: the `@pytest.mark.slow` reranker
    tier's one-time cold-cache weights download (the deliberate exception `tests/conftest.py`'s
    module docstring records, lode-gmo/lode-pql) and any `@pytest.mark.network` test needing a real
    Hub call. `HF_HUB_DISABLE_TELEMETRY` costs nothing functional by comparison: verified against
    the installed huggingface_hub 1.24.0, it is read in three places, of which two are functional —
    the user-agent enrichment and a fire-and-forget telemetry ping — the third being
    `_runtime.py`'s `dump_environment_info`, which only prints it in the `huggingface-cli env`
    bug-report dump. (Stated exactly so re-running the grep matches the claim: an earlier draft
    said "exactly two", which would have sent a future auditor re-deriving the whole
    justification to find out the claim was merely imprecise rather than stale.)
  - **Layer 2, the actual fix — record, then raise.** Both guards now append the violation (plus the
    stack captured at interception) to a process-global list *before* calling `pytest.fail`, and the
    autouse fixture's teardown fails the test on anything left unconsumed. The raise still gives the
    immediate, well-placed traceback; the record is what survives a caller that swallows it. This is
    strictly a *reporting* fix — the connect was always blocked either way.
  - **It also closes a limit this file's predecessor prose called permanent.** `tests/conftest.py`
    claimed a connect made off the main thread "prevents the call but cannot itself fail the test",
    and that "lode makes no such calls today". The second half was already false when written
    (lode-fr3p/lode-7ypf: a Textual worker reaching `asyncio.to_thread` in the related-notes panel),
    and the first is no longer true — the teardown check reads the record regardless of which thread
    appended it. Corrected in place there and in [`onboarding.md`](onboarding.md). A **subprocess**
    connect remains out of reach, and is now the only stated limit.
  - **Residual, accepted: attribution, not detection.** A record appended after its own test's
    teardown — a straggler worker outliving the test that started it — is blamed on whichever test
    is running when it is next checked. Every message therefore carries the interception-time stack,
    and says in as many words to trust that stack over the test name. Detection is not affected;
    only the label on it is.
  - **`@pytest.mark.trips_network_guard`** (new) is how a test that trips a guard *on purpose*
    consumes its own record. It is checked in **both** directions — a marked test that trips nothing
    fails too, because a marker that has stopped being needed silently disables the backstop for
    that test.

- **2026-07-29 (lode-7ypf) — the unstubbed-`FastEmbedEmbedder` leak class closed with ONE autouse
  stub, scoped by the socket guard's own predicate.** Not planned for this branch: it was forced by
  the lode-sx17 entry above. The moment the network guard stopped depending on nobody swallowing its
  raise, this leak stopped being stderr noise and became an intermittent gate failure — 3 of 3
  full-suite runs under artificial CPU load went red, and 1 of 2 on an idle machine, in
  `test_tui_browse_screen.py`, `test_tui_quit.py`, `test_tui_capture_save_and_new.py` and
  `test_tui_reconcile_screen.py`. The captured interception stack named the path every time:
  `RelatedNotesPanel._search_related` → `asyncio.to_thread` → `find_related_notes` → `embed_query`
  → `_load` → `resolve_model_revision` → `huggingface_hub.model_info` → httpx → `socket.connect`,
  from a `concurrent.futures` worker thread. So lode-sx17 could not land without it.
  - **Decided: an autouse stub, not ~40 hand-patched call sites.** lode-7ypf's own acceptance left
    the choice open and asked for one or the other, deliberately, not both. Six test modules had
    already written the same `_StubEmbedder` for themselves and ~35 more call sites had not; a
    convention that must be remembered at every new `TextArea` test is the thing that failed here.
    The local stubs are **left in place** — several count constructions or record calls, which is
    the point of the test they belong to, and a test's own `monkeypatch.setattr` runs after the
    fixture's and so still wins.
  - **Scope = `_egress_guard_applies`, a predicate now shared with guard 2**, rather than a second
    marker list that could drift. The stub exists only to remove egress the socket guard would
    otherwise block, so the set of tests it covers *must* be the set that guard polices: a test
    allowed to reach the network (`network`) or to load a real model (`slow`) gets the real class.
    One predicate is what makes that true by construction instead of by two lists agreeing.
  - **Both call-time bindings are patched**, `lode.embedding` (what the deferred imports in
    `RelatedNotesPanel._ensure_embedder` and `lode.cli` resolve against) and
    `lode.tui.services.related` (its own import-time binding, used by `find_related_notes`'s
    `embedder or FastEmbedEmbedder(settings)` fallback). No test reaches the second today; it is a
    live production path one test away from leaking the same way, and patching one binding but not
    the other is the half-fix that gets rediscovered.
  - **`@pytest.mark.real_embedder` (new) is the opt-out, with exactly one user:** the canary that
    pins the *installed* fastembed's exhausted-sources error string (`tests/test_cli.py`). It
    deliberately does **not** reach for `slow`/`network` — it is hermetic via `HF_HUB_OFFLINE=1`
    against a cold `$LODE_HOME`, and the socket guard staying on is part of what it asserts.
  - **The stub mirrors the whole duck-typed surface, not just the two `Embedder` protocol methods.**
    lode probes `warm()` (`lode models pull`) and `model_revision()` (`_embedder_model_revision`,
    for vector provenance) by `hasattr`. Omitting either does not fail loudly — it silently routes
    the code under test down the *absent-method* branch, which production never takes.
  - **Verified:** 5 consecutive full-suite runs under the same 20-spinner CPU load that had produced
    3 of 3 failures, all green, no "Task exception was never retrieved". Pinned by four tests in
    `tests/test_network_guard.py` (kept there because the stub's scope is *defined by* the guard's),
    each proven against a sabotage. Two of those tests were themselves caught being order-dependent
    by their own sabotage — a module first imported *inside* a test body binds the already-patched
    value and asserts nothing — and now import at module scope so collection binds them first.
  - **Not fixed here: lode-dj6m**, the product-side defect underneath
    (`FastEmbedEmbedder._load` resolves the HF revision eagerly even on query-only paths). This
    entry removed the *test*-side exposure only. **Since fixed by lode-dj6m**, which moved the probe
    out of `_load` into `model_revision()` behind an idempotent flag under the same lock: the
    query-only path (`embed_query` — related-notes, `ask`/`retrieve`) now makes no HF probe at all,
    pinned by `tests/test_embedding.py::test_embed_query_never_probes_the_revision_even_with_a_warm_cache`.
    The autouse stub above stays necessary regardless — the real embedder still loads hundreds of MB
    of ONNX weights on first use. Left open after lode-dj6m, and deliberately not folded into it:
    the *write* path re-probes per indexed version, so `lode models pull`'s "every subsequent run is
    fully offline for indexing and retrieval" remained false for indexing. **Resolved by lode-r4r2**
    — see that entry below for the resolution (docstring corrected to promise offline retrieval
    only; the probe now short-circuits under `HF_HUB_OFFLINE`; persisting the revision to make the
    write path genuinely offline was considered and rejected).

- **2026-07-30 (lode-r4r2) — `lode models pull`'s "fully offline" promise, resolved via docstring +
  offline short-circuit, not by persisting the revision.** Filed during lode-dj6m's review: after
  that fix, retrieval (`embed_query`) makes no HF probe on a warm cache, but indexing (`embed`) still
  makes one live `huggingface_hub.model_info` call **per indexed version** to stamp per-vector
  provenance (`model_revision`), because the resolved value is per-instance in-memory state that
  `FastEmbedEmbedder.warm()` cannot usefully prepay — a *later*, separate process's embedder starts
  with `_revision_probed = False` regardless of an earlier `models pull`. (The ticket said "per
  process"; this review measured it as worse than that — `lode work`'s drain builds a fresh embedder
  per queued job, so a drain of N versions pays N probes and N ONNX reloads. That behaviour is
  `lode-j5r2`, filed rather than fixed here: changing it is a behaviour change, and this ticket's
  whole deliverable was making the docs agree with the behaviour that exists. The wording everywhere
  says "per indexed version" for that reason; `lode-j5r2` landing should make the original "per
  process" wording true and is the trigger to revisit those sites together.)
  **Update (lode-j5r2) — that landing happened; those sites say "per process" again. See the marker
  at the end of this entry.**
  The ticket named three options and left the choice
  open, deliberately, as a design call rather than a mechanical fix:
  - **(a) Persist the resolved revision** next to the weights cache so a warm genuinely prepays it.
  - **(b) Correct the docstring** to promise offline retrieval only.
  - **(c) Make the probe respect `HF_HUB_OFFLINE`** so it short-circuits instead of blocking on a TCP
    timeout with no network — *as the ticket framed it; that framing turned out to be wrong, below.*
  - **Decided: (b) + (c), not (a).** (a) is the only option that makes the original sentence
    literally true, but it changes what the recorded `model_revision` *means*: `docs/storage.md`'s
    DETECT-not-PIN decision (`lode-crh8.1`) frames the per-vector field as the **live, currently-resolved**
    revision at embed time — "a fact about a given installation's pull history" that two installs
    embedding on different days can legitimately disagree about — not a value read back from a prior
    `models pull` that can go stale between pulls. Making indexing read a cached local value instead
    of probing live would quietly redefine that field toward PIN semantics, which is exactly the
    revisit-only-deliberately territory `docs/storage.md` already reserves for a separate decision,
    not something to fold into a docstring-accuracy ticket. (b) and (c) are cheap, compatible, and
    leave that architecture untouched: the write path still makes one live probe per indexed version
    (this is a real, accepted cost — not eliminated), but the promise made about it is now accurate.
  - **(c) turned out to be a no-op in behaviour, and is kept anyway — knowingly.** The ticket's
    premise for (c) was that `HF_HUB_OFFLINE=1` on a black-holed network still stalls on a TCP
    timeout. **That premise is false**, measured during this review on the pinned `huggingface_hub`
    1.24.0 with `socket.socket.connect` patched to record attempts: with the guard removed and
    `HF_HUB_OFFLINE=1` set, `model_info` returned control in 0.2s with **zero** connect attempts —
    `utils/_http.py`'s `hf_request_event_hook` raises `OfflineModeIsEnabled` (a `ConnectionError`)
    before any socket work, and `resolve_model_revision`'s existing `except Exception` already folded
    that into the same `None`. (The repo knew this: `tests/conftest.py` says so in passing, in its
    "why `HF_HUB_DISABLE_TELEMETRY` and not `HF_HUB_OFFLINE`" note.) The guard is kept because lode
    now *promises* this behaviour to users in four places, and a promise worth making is worth
    enforcing and testing locally rather than inheriting from a transitive dependency — and because
    lode's check is read live, where `huggingface_hub` freezes `HF_HUB_OFFLINE` into a module
    constant at import (the same import-time-freeze trap `tests/conftest.py` documents). It is
    recorded as belt-and-suspenders, **not** as a stall that was fixed.
  - **The stall the premise described is real, just elsewhere: `lode-w5nr`.** With `HF_HUB_OFFLINE`
    *unset* and no network, the probe is genuinely unbounded — `huggingface_hub`'s
    `default_client_factory` builds its `httpx.Client` with `timeout=None`. No offline-flag check can
    see that case; it needs a bound of its own.
    **Update (lode-w5nr, 2026-08-02) — bounded; see the `lode-w5nr` entry at the end of this file.**
  - **What changed:** `lode models pull`'s docstring (`src/lode/cli.py`) now says retrieval is fully
    offline after a warm and indexing makes one metadata call per indexed version, rather than
    claiming both are offline. `resolve_model_revision` (`src/lode/embedding.py`) now checks
    `lode.config.hf_hub_offline()` before attempting the HTTP call and returns `None` immediately if
    set. The private `_hf_hub_offline()` helper that
    used to live only in `cli.py` moved to `lode.config.hf_hub_offline()` (public) once a second
    module needed the identical check, rather than duplicating it. `README.md`, `docs/onboarding.md`,
    and `docs/configuration.md`'s "Models" section, which all repeated the same "fully offline for
    indexing and retrieval" claim, are corrected to match.
  - **Test trap, hit for the second time in this one function.** The acceptance test as first written
    stubbed `huggingface_hub.model_info` to *raise*, which `except Exception: return None` silently
    swallows — it stayed green with the short-circuit deleted. Now counts calls instead (see the
    docstring on `test_resolve_model_revision_short_circuits_under_hf_hub_offline`). **Anything
    testing this function must count or time, never raise** — lode-dj6m's review caught the identical
    vacuity in the sibling test.
  - **If a future ticket wants (a) anyway** (e.g. because the live-probe network cost on every
    indexing run turns out to matter more than the DETECT-semantics purity), it needs to re-open and
    explicitly resolve the tension with `docs/storage.md`'s DETECT-not-PIN framing first — this entry
    is that trigger, not a blanket "don't."
  - **Update (lode-j5r2):** landed — the probe is once per **process** again, so the four doc sites
    this entry names above are corrected back to "per process". This entry's own "per indexed
    version" wording stays as written (this file is a dated log — an entry narrates history, it is
    not rewritten); the fix itself is the next entry below.

- **2026-08-02 (lode-j5r2) — `worker.drain` hoists ONE embedder across its main loop; `lode work`
  shares one further still across every poll pass of its run.** Filed during `lode-r4r2`'s review: that
  ticket corrected `lode models pull`'s docstring (and three sibling docs) to say indexing makes one
  HuggingFace metadata call **per indexed version**, because that was what `_embed_handler` actually
  did — `lode.embedding.embed`'s `embedder or FastEmbedEmbedder(settings)` fallback built a brand-new
  instance every job, so a drain of N queued versions paid N full ONNX model loads (measured ~1.5s
  each) *and* N live `model_info` probes, not one.
  - **The fix:** `drain()`'s main claim/run loop now constructs (or reuses, if the caller supplies
    one via the new `embedder=` parameter) exactly ONE `FastEmbedEmbedder` and threads it into every
    `embed` job via `_embed_handler`'s own new `embedder=` parameter — the same seam `embed()` already
    exposed and the TUI's capture screen already uses for the identical reason (module docstring,
    `lode-0wj.4`). The swap is guarded on handler *identity*
    (`registry.get("embed") is _embed_handler`), not job-type membership, so a test that injects its
    own stub "embed" handler is completely unaffected — no `functools.partial` wrapper is ever applied
    over a caller-supplied handler, only over the real one, and the module-level `_REGISTRY` singleton
    is never mutated (the swap builds a shallow per-call copy). `lode work`'s CLI command (`cli.py`)
    goes one step further: it constructs the shared embedder itself, once, *before* the polling
    `while True:` loop, and passes it into every `drain()` call that loop makes — every pass of
    `--loop` *and* of `--wait`, which share that loop — so the amortization holds for the whole
    process, not just a single drain pass with several jobs queued at once.
  - **Why not cache across `drain()` calls by default:** `drain()`'s own default (`embedder` omitted)
    still constructs a fresh instance per *call* — correct for a caller with no reason to keep one
    around (e.g. a test, or a future one-off caller). Sharing across an entire process's lifetime is a
    decision the *caller* makes by holding one instance and passing it in every time, which is exactly
    what `cli.py`'s `work` command now does; `drain()` itself stays a plain, stateless function with no
    module-level embedder cache of its own.
  - **Docs corrected back to "per process":** `lode models pull`'s docstring (`src/lode/cli.py`),
    `README.md`, `docs/onboarding.md`, and `docs/configuration.md`'s "Models" section — the same four
    sites `lode-r4r2` corrected to "per indexed version" — now say "per process" again, since it is
    true again. `FastEmbedEmbedder.warm()`'s own docstring (`src/lode/embedding.py`), which had named
    this exact gap ("worse than once per process ... that is lode-j5r2, filed rather than fixed
    here"), is corrected in place. The *live* home for the fact itself is `docs/storage.md`'s async
    work queue section, per CLAUDE.md's routing rule — this entry is the dated log, not the record a
    reader should have to find.
  - **Test proven non-vacuous the way this function's own history demands** (`lode-dj6m` and
    `lode-r4r2` each caught a raising-stub trap here once already): a new `drain()`-level test counts
    both `FastEmbedEmbedder` constructions and `model_revision()` probes across a 3-job drain using a
    counting stub — never a stub that raises, which `_embedder_model_revision`'s own
    `except Exception: return None` would silently swallow into a false pass. The stub mirrors the
    real class's own one-time-probe caching (`lode-dj6m`) because without it even a correctly *shared*
    instance would probe once per `embed()` call and the probe assertion would fail for the wrong
    reason; asserts exactly 1 construction and exactly 1 probe across 3 queued jobs, and that all 3
    land `done`. Re-proven under sabotage during review: with the identity guard forced false the test
    fails `3 == 1`. A **second** test (`tests/test_cli.py`) pins the *process*-level half separately —
    two poll passes of `lode work --loop`, one construction — because nothing else does: the obvious
    future tidy-up (moving `cli.py`'s construction inside the polling loop) would otherwise revert it
    with every gate green and six doc sites left lying.
  - **The trade this makes, named because half of it was a surprise (found in review).** A long-lived
    embedder keeping the ONNX model resident is the expected, intended cost. The unintended half is
    that `FastEmbedEmbedder.model_revision()` latches its first result for the instance's lifetime and
    **deliberately caches a FAILED probe too** (`embedding.py`'s own comment: keying off the value
    would re-probe on every call for exactly the offline case that can least afford it). That latch
    was written when instances were per-job; it now spans a process. Measured during review, first
    probe failing and every later one succeeding: a shared instance returns `None` five times for one
    `resolve_model_revision` call, where five per-job instances return `None` once and then the real
    SHA. So a single transient probe failure at the head of an hours-long `lode work --loop` stamps
    `model_revision = NULL` on **every** version indexed for the rest of that process — degrading
    `lode status`'s drift check to "mixed" for the whole session, with no self-heal short of a
    restart, where before it cost exactly one version. Accepted, not fixed here: re-probing per
    `drain()` call is a behaviour change of its own and is filed as `lode-fxse`. Note this makes
    `lode-w5nr` (bounding the probe) matter *more*, not less: post-fix the unbounded no-network stall
    is paid once per process rather than once per version, but its NULL result now sticks for the run.
  - **This is not the `lru_cache` alternative the ticket rejected, and not a drift toward PIN.** The
    ticket rejected memoizing `resolve_model_revision` partly because a long-running process would
    then read a cached rather than live revision. Per-*process* caching is not that concession: it is
    the semantics `lode-r4r2`'s entry above already describes as intended ("one metadata call per
    process"), and the per-*job* probing this fixes was the accident. `lode status`'s drift check is
    untouched — it calls `resolve_model_revision` directly, never through an embedder, so it still
    reads live. The `DETECT, not PIN` decision (`storage.md`, `lode-crh8.1`) is unaffected.
  - **Update (lode-fxse) — fixed: option (b), a bounded per-`drain()`-call retry, not left as
    documentation-only.** Of the three options this entry's own ticket named — (a) leave it,
    documented only; (b) retry the probe once per `drain()` call; (c) have
    `resolve_model_revision` distinguish "deterministically unavailable" from "transiently failed"
    so the latch means what its comment claims — **(b)** landed.
    `FastEmbedEmbedder.reset_revision_probe()` (`embedding.py`) is a new seam `drain()` calls once
    per call, before that call's jobs run — never once per job, and `worker.py` never reaches into
    `_revision_probed` itself, exactly as this ticket demanded. **It is conditional, not an
    unconditional cache-bust**: it re-arms the latch only when the cached result is already `None`
    (a prior probe failed) and is a no-op once a probe has *resolved* a real revision — so a
    successfully-resolved installation keeps paying the probe exactly once per process, same as
    `lode-j5r2` intended, and only a probe that would otherwise stay stuck at `NULL` pays for a
    retry, at most once per poll tick, never once per job. Option (c) was **not** taken — a bigger
    change than this ticket's fix needed, since (b) alone already makes the retry safe for the
    deliberately-offline case without that distinction (next paragraph).
  - **Why (b) is safe for exactly the case its own comment worried about.** The rejected concern
    was "keying off the value would re-probe on every call for exactly the offline/unpinned-model
    case that can least afford it" — true if checked on *every* `model_revision()` call (which
    `reset_revision_probe()` never does), false at the *once-per-`drain()`-call* granularity this
    fix actually uses: `resolve_model_revision` short-circuits under `HF_HUB_OFFLINE` or an
    out-of-pinned-set `model_cache_identity` *before* touching `huggingface_hub` at all (`lode-r4r2`),
    so re-arming the latch for that case costs a cheap local check, never a live network round trip
    — retrying it once per poll tick is free. The only case a retry genuinely costs anything is a
    real, unreachable-network failure without `HF_HUB_OFFLINE` set, and there the cost is exactly
    what `lode-w5nr` already bounds it to (`settings.hf_probe_timeout_s`, 5.0s default) — once per
    poll tick with pending embed work, not once per job, self-healing within one
    `--loop`/`--wait` interval instead of needing a restart.
  - **Known and accepted: a SUSTAINED real outage has no backoff of its own** (review finding). The
    retry cadence is whatever `--interval` happens to be (`work_poll_interval_s`, 5.0s default) —
    a job-polling knob, now doubling as a network-retry cadence nothing explicitly owns. Through a
    multi-hour HF outage with embed work continuously pending, each tick adds up to
    `hf_probe_timeout_s` (5.0s) of blocking probe. Accepted rather than fixed: it is bounded per
    tick by `lode-w5nr`, costs nothing on an idle queue or a `HF_HUB_OFFLINE` install, and the
    obvious remedy (exponential backoff on consecutive failures) is state this seam deliberately
    does not carry — the whole point of (b) over (c) was that `drain()` owns *when* to retry and
    `FastEmbedEmbedder` owns *whether*, with neither modelling failure history. Revisit only with a
    real report; a user hitting it can set `HF_HUB_OFFLINE` and pay nothing.
  - **Test proven non-vacuous the way this area's own history demands** (lode-dj6m, lode-r4r2,
    and this ticket's own description each name the same trap): a stub that *raises* is swallowed
    by `_embedder_model_revision`'s `except Exception: return None`, so every new test here counts
    calls instead. `tests/test_embedding.py` pins `reset_revision_probe()` in isolation — one test
    proves a failed probe is retried and the healed result then latches again, a second proves a
    successful probe is untouched by a reset (probe count stays 1). `tests/test_worker.py` pins the
    `drain()`-level integration — two `drain()` calls against the *same* embedder (mirroring
    `cli.py`'s `work` command holding one embedder across every poll pass) prove a first-call
    failure is retried on the second call.
  - **Counting calls is necessary but not sufficient — the seam needed its own test (review
    finding).** `drain()` reaches the retry through `getattr(embedder, "reset_revision_probe",
    None)`, a string literal that no rename refactor and no type checker follows, and every
    `drain()`-level test above substitutes a stub defining whichever name that literal happens to
    say. So the literal agreeing with the real class was pinned by nothing: renaming
    `FastEmbedEmbedder.reset_revision_probe` (updating its real call sites, as a rename does
    automatically) left the `getattr` resolving to `None`, `drain()` silently no-opping, this whole
    fix dead — and the suite green, 135/135, measured. The branch's original stub-based mirror test
    for the *success* half was vacuous for the same family of reason, one step removed from the
    raising-stub trap: it hard-coded the "no-op on success" contract into its own stub's
    `reset_revision_probe`, so it asserted on itself and passed against the pre-fix code unchanged.
    Both are now replaced by one test driving the **genuine** `FastEmbedEmbedder` through three
    `drain()` passes (`@pytest.mark.real_embedder`, with only the ONNX load and
    `huggingface_hub.model_info` faked), asserting on the `model_revision` actually written to the
    vector rows: NULL on the blip's own pass, healed on the next, and still cached on the third. It
    is the only test that can pin the success half honestly, and it fails under all three sabotages
    (method renamed, `drain()`'s call removed, the reset made unconditional).
  - **The live record moved.** `docs/storage.md`'s async work-queue section (not this dated log)
    now describes the fixed, self-healing behavior instead of the unfixed latch — per this file's
    own preamble routing rule. `FastEmbedEmbedder.model_revision()`'s and `warm()`'s docstrings
    (`embedding.py`) and `drain()`'s own docstring (`worker.py`) are corrected in place; the module
    docstring narrative is not duplicated a further time here.

- **2026-07-28/29 (lode-yrtu) — HUMAN DECISION: who owns machine-local worktree-leak detection —
  widen `/land`'s existing Section 4 sweep, not a new entry point and not `/sweep`.** Discovered
  while landing lode-25xp: `/land`'s backstop sweep reported `not-merged=8` out of 14–18 worktree
  directories under `.claude/worktrees/`, and none of them were the single dirty-worktree residual
  lode-9hgu already accepted.
  - **The size motivation was inflated ~3x and re-measured.** Per-worktree `du -sh` counts `venv/`
    bytes that are hardlinked across every worktree, the main venv, and the uv cache — removing one
    worktree frees none of them. The only honest number is the sum of files with link count == 1
    (bytes that actually disappear on `rm`): **~940MB** across the target class (clean, not-merged,
    unlocked `worktree-agent-*`), not "the bulk of 2.2GB" as first measured. **Any future size claim
    in this area must use the unshared-bytes method**, not per-dir `du -sh` (which overstated by
    ~7x here) or even `du -sh --count-links` (which overstates the opposite direction by counting
    shared bytes as if every worktree owned them outright). The real disk hog on this machine is
    `~/.cache/uv` (3.1GB) and `~/.cache/pip` (1.0GB), which no worktree GC touches and which stayed
    explicitly out of this ticket's scope.
  - **Item-4 check, done FIRST as the note demanded, before widening anything:** three
    clean+merged+unlocked worktrees flagged on 2026-07-28 as "the existing sweep should already
    reclaim these" (`agent-a4b712b9130603520`/`land/lode-s1ia`, `agent-aa13e76dcfffb28f6`/
    `land/lode-sdjb`, `agent-aa5d13f2d86731578`/`land/lode-rfon`) were **re-checked on 2026-07-29 and
    are gone** — `git worktree list --porcelain` no longer lists any of them. Confirms this was a
    timing gap (the sweep simply hadn't run since they merged), **not a live predicate bug** in the
    existing merged-into-trunk reclaim path. Widening was safe to proceed with.
  - **Three options were on the table; (a) was chosen:**
    (a) widen `/land`'s existing Section 4 sweep to also reclaim clean, not-merged
    `worktree-agent-*` worktrees; (b) a separate machine-local `/gc` entry point; (c) `/sweep`
    surfaces the leak with a charter amendment. **(c) rejected**: `/sweep`'s own charter says
    "surface only, never act" and its dedup is a durable **cross-machine** digest issue — but a
    worktree leak is **machine-local**, a genuine design mismatch (a leak on machine A would surface
    as a phantom item on every other machine's digest), not merely a scope question. **(b)
    rejected**: a new entry point nobody runs automatically costs more than it buys when (a) is
    available at near-zero marginal cost — `/land`'s Section 4 already enumerates every worktree and
    already computes the bucket counts every pass, on the one machine that's the right machine to
    run this. **(a) chosen**: smallest change, no new skill, no charter conflict, and the sweep
    already runs unattended via `/loop 5m /land`.
  - **Mechanism: reclaim the DIRECTORY, keep the branch REF.** `/land`'s existing sweep coupled
    `git worktree remove --force` with `git branch -D` unconditionally; for a clean worktree those
    are separable — any commits the build made stay reachable via the surviving
    `worktree-agent-*` ref. Scoped via a `case "$BR" in worktree-agent-*)` guard in the loop's
    not-merged branch (`.claude/skills/land/SKILL.md` Section 4) — every other not-merged shape (a
    `land/<id>`-branched reviewer/rebase-pickup worktree, a detached worktree, anything else) is
    completely unaffected.
  - **Verified before shipping (acceptance criterion 3): removing the directory while keeping the
    ref loses no reachable commits, INCLUDING the detached-HEAD case — by construction, not by
    testing luck.** A detached worktree's porcelain `branch` field is always empty, and the `case`
    pattern `worktree-agent-*)` can never match an empty string — so a detached worktree structurally
    cannot enter this reclaim path at all; it always falls to the unchanged default arm (kept, dir
    and all). The new path is therefore only ever reachable for a worktree that DOES have an
    attached branch, which is exactly the case the "keep the ref" argument covers. No rescue-ref
    dance is needed because the case that would need one is provably unreachable here.
  - **Accepted residual: a dir-only-reclaimed branch ref persists indefinitely.** Nothing in this
    design ever deletes it — an abandoned/bounced branch, by definition, never merges into `trunk`,
    so the third bare-ref backstop's own `merged`-into-`trunk` guard (below) never fires for it
    either. A lightweight ref costs near-nothing next to the ~100MB directory it used to anchor;
    if the accumulation of such refs is ever material, a future ticket can add an explicit
    age-based ref sweep for this specific case. Out of scope here.
  - **Corollary for an INTERRUPTED build whose commits were never pushed** (the shape this very
    ticket's own build hit — a producer killed by three API 500s, `origin/land/<id>` nonexistent, bd
    notes saying "DO NOT DELETE"): its worktree is a clean-or-dirty `worktree-agent-*` like any other,
    so if it is clean and ages past the floor, the sweep reclaims the DIRECTORY out from under a
    resume that was told to reuse it. Nothing is lost — the commits are on the kept ref — but the
    recovery is not automatic and is worth stating once: re-materialize with
    `git worktree add .claude/worktrees/<name> worktree-agent-<name>`, then resume in it. A build with
    UNCOMMITTED work is never in this position at all; the dirty-tree guard keeps it (lode-9hgu).
  - **Guard against eating an in-flight build: an AGE FLOOR on the worktree's last commit
    (`LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS`, default 21600s/6h), not the lock start-token check.**
    The ticket's own text suggested preferring the token check as "a stronger liveness signal than
    age alone" — it is, but it is unavailable for exactly the case that matters here.
    `.claude/agents/coding.md` unlocks its build worktree immediately after its FIRST commit
    (lode-oqr), and the entire rest of a build's cycle — more edits, gates, more commits, push,
    hand-off — runs UNLOCKED and NOT-MERGED from that point on. A `git worktree lock` reason (and
    the pid/start token in it) exists ONLY while the worktree is actually locked; once unlocked,
    that information is simply gone, so there is nothing left to compare a token against for the
    entire window this widening targets. A worktree that is unlocked, not-merged, and momentarily
    CLEAN (the instant between one commit and the build's next edit) is otherwise indistinguishable
    from a genuinely abandoned one using anything `git worktree list --porcelain` exposes. Age of
    the last commit is the only signal that remains, and it fails SAFE in the direction that
    matters: a build still actively cycling has a recent `HEAD` commit almost by construction, so a
    generous floor skips it every time — at the cost of a genuinely idle worktree waiting a few
    extra hours to be reclaimed, which costs nothing since the branch ref (see above) is never lost
    either way. 6 hours was chosen as comfortably longer than any single producer build-to-hand-off
    cycle (typically well under an hour per `.claude/agents/coding.md`'s own cycle), while still
    reclaiming space same-day. **The age floor is a stopgap, not the ceiling of what is possible** —
    recorded so a future reader does not have to rediscover it. The deeper fix is a liveness marker
    that outlives the lock: an agent (or the harness) touching a per-worktree heartbeat file for as
    long as a session still holds it would let the sweep separate "idle 6h because abandoned" from
    "idle 6h because between commits" exactly, instead of guessing from commit age. Not built here —
    it touches the producer/harness session lifecycle, well outside `/land`.
  - **Criterion 6 answered: the lock IS per-session, not per-agent, and the resulting leak class is
    fixed here, folded in per the human note (not split into a separate ticket).** Confirmed live:
    raw `.git/worktrees/<name>/locked` reasons showed 3 of 4 (and, in the original 2026-07-28
    measurement, 4 of 5) locked worktrees sharing ONE pid — the harness/session process, not a
    per-agent one. Since `locked` is tested first, ahead of every other predicate, a dead session
    leaked every worktree it had ever locked, forever. Fixed by `scripts/worktree-lock-stale.sh`
    (tested: `tests/test_worktree_lock_stale.py`, 9 cases against real processes and real
    `/proc/<pid>/stat` files, no mocking): a lock is treated as stale iff `kill -0 <pid>` fails, OR
    the pid is alive but `/proc/<pid>/stat`'s own `starttime` (field 22, robustly extracted via the
    LAST `)` in the line — `comm` can itself contain parens/spaces) no longer matches the `start
    <token>` the harness recorded at lock time (pid reuse). This closes the reuse hazard that makes
    plain `kill -0` alone unsafe, without resorting to a wall-clock window — unlike
    `scripts/land-lock.sh`'s OWN staleness window, which exists because ITS recorded pid is a single
    Bash tool sub-invocation, structurally always-dead by the time a later invocation reads it (pid
    liveness is meaningless there); the pid recorded in a `git worktree lock` reason is the long-lived
    session process, so liveness IS a meaningful signal once the reuse hole is closed. A lock this
    script cannot positively prove dead is left alone (fail closed) — getting this wrong in the other
    direction risks exactly what lode-oqr already cost once: destroying a live build's worktree. NOTE
    on drift: the original 2026-07-28 measurement said "4 of 5 share one pid (1105248)"; that pid was
    gone by 2026-07-29 (a different session), and the sharing was then 3-of-4 under a different pid.
    The PATTERN held, the specific numbers did not — do not hard-code any pid or count.
  - **Adjacent observability defect, also fixed here (folded into the same ticket per its own
    scope): the two bare-ref backstops (`land/*` orphans and `worktree-agent-*` orphans) now report
    only deletions that ACTUALLY happened.** OBSERVED live: a prior pass printed "backstop2: deleting
    stale local ref land/lode-rlyx--agent-aad6b30a923856fb7" while the ref still existed afterward —
    `git branch -D` had refused it (still checked out in a locked worktree) and the trailing `|| true`
    swallowed that failure silently. Same class of bug lode-bns3 already fixed for the main worktree
    loop (counting an attempt rather than the remove's real exit status), now applied to the two
    backstops that had missed it. Fixing this also required switching both loops from a trailing pipe
    (`git for-each-ref ... | while read -r BR; do ...; done`) to process substitution
    (`< <(...)`) — a counter assigned on the right side of a pipe runs in a subshell and is lost the
    moment the loop ends, so the old shape could never have reported real counts even with the exit-
    status check added; the new shape (matching the main worktree loop's own already-correct
    convention) makes the counters actually survive to the summary echo.
  - **Full mechanism lives in code + tests, not restated at length here:**
    `.claude/skills/land/SKILL.md` Section 4 (the widened loop + both backstop fixes),
    `scripts/worktree-lock-stale.sh` (the stale-lock detector), `tests/test_worktree_lock_stale.py`
    (its tests), and [docs/agents-workflow.md](agents-workflow.md#worktree-gc-widened-to-reclaim-clean-not-yet-merged-builder-worktrees-lode-yrtu)
    (the summary + the `LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS` tunable's home).

- **2026-07-28 (lode-ysr6) — `scripts/gate-lib.sh`'s `GATE_ADVISORY` contract made structural, not
  an ordering convention.** `GATE_ADVISORY` used to be set by a separate `GATE_ADVISORY=(...)`
  statement each consumer wrote below its own source line, so a `gate_could_not_run` call site placed
  above that statement still exited 2 with a correct banner while silently emitting only half the
  contract — and nothing mechanical caught it: not `set -u`, not shellcheck, not the library's own
  tests. **Decided: bind `GATE_ADVISORY` at source time instead**, from positional arguments on the
  source line itself (`. gate-lib.sh "advisory line 1" "advisory line 2"`) — the assignment becomes
  part of the `source` command, so there is no longer a separate statement for a call site to sit
  above.
  - **Correction the original proposal did not anticipate, verified empirically on bash 5.2:** `source
    file` with ZERO trailing tokens does not clear `$@` inside `file` — it inherits the calling
    script's own positional parameters unchanged. A naive, unconditional `GATE_ADVISORY=("$@")` would
    therefore have leaked a no-advisory consumer's own CLI argv (e.g. `release-bump.sh`'s range
    argument) into the advisory trailer. Closed by requiring an explicit `--no-advisory` sentinel:
    every consumer now passes either its advisory strings or that literal sentinel, never nothing — a
    narrower discipline than the old ordering convention, and swept once per consumer file by
    `tests/test_gate_lib.py`.
  - **Weighed and rejected: keeping the separate assignment because it reads better.** The ticket
    raised this explicitly — a plain `GATE_ADVISORY=(...)` near the top of a consumer is arguably
    easier to read than threading two strings through a source line. Overruled because the two are
    not comparable: the readability cost is paid once, visibly, by whoever writes the source line,
    while the ordering hazard was silent and unpoliceable by any of the three mechanisms above. The
    ticket also predicted the positional-parameter restore behaviour would need its own comment to be
    safe; it does, and the header carries it. Recorded here so the trade is not rediscovered a third
    time: the header states the outcome, not the alternative that lost.
  - **`scripts/gate-lib.sh`'s header comment (its `GATE_ADVISORY` section) is the OPERATIVE copy** —
    the full usage contract, the bash-5.2 verification and the enforcement story live there, and are
    corrected in place as the mechanism changes. The prose above is this log's dated snapshot of what
    was decided and why; if the contract later changes, it changes in the header, and this entry gets
    a dated supersession marker rather than a rewrite (see this file's preamble). (Recorded late:
    `lode-ur6o` was normalizing this file when `lode-ysr6` landed, so `lode-ysr6` left the record in
    the header and `lode-szgb` folded it in here afterwards.)
- **2026-08-05 (lode-nwqb) — REJECTED: replacing `gate-lib.sh`'s `--no-advisory` sentinel with an
  assignment-prefix binding (`GATE_ADVISORY_LINES=... . gate-lib.sh`).** Filed while reviewing
  `lode-ysr6`, which adopted the source-positional-args shape (see the entry immediately above) plus
  the `--no-advisory` sentinel. The alternative: bind the advisory through a variable-assignment
  *prefix* on the source command instead of positional args — `gate-lib.sh` reads
  `GATE_ADVISORY_LINES` and `mapfile`s it into `GATE_ADVISORY`; "no advisory" becomes the natural
  unset default, so the sentinel, its enumerate-and-assert sweep, that sweep's non-vacuity proof, and
  the per-consumer sentinel comments all disappear.
  - **Re-measured empirically on bash 5.2.21 (not taken on the ticket's word, per its own AC):** a
    prefix assignment on a `source`/`.` command does **not** persist into the calling shell in
    default mode (`GATE_ADVISORY_LINES` reads unset immediately after the source returns) but **does**
    persist under `set -o posix` (confirmed: it survives with the exact value intact). `.` is a POSIX
    special builtin, and the persistence of assignment prefixes on special builtins is specified
    behaviour, not an accident of this bash build.
  - **Decided: keep the `--no-advisory` sentinel.** Three reasons, weighed together rather than any one
    being decisive:
    1. This is a redesign of finished, gated, behaviour-verified shared gate infrastructure, not a
       review cleanup — `lode-ysr6`'s own ticket text said "decide before building," and a reviewer
       swapping the already-decided mechanism wholesale is out of remit for a follow-up ticket to do
       unilaterally without a fresh, deliberate decision. This entry is that decision, and it declines.
    2. The measured mode-dependent persistence trades one *documented, already-tested* subtlety (the
       positional-parameter restore behaviour `lode-ysr6` already covers) for a *less-known,
       mode-dependent* one. No consumer runs POSIX mode today, but this is the same class of
       bash-vs-sh divergence `lode-zlg8` exists to guard against — swapping a known hazard for a
       differently-shaped one is not a demonstrated improvement.
    3. It loses the bash array at the point the value is built: advisory lines become a
       newline-joined string that must be reconstituted with `mapfile`, which is more machinery, not
       less, exactly where `lode-ysr6` was trying to remove machinery.
  - Honest assessment carried over from the ticket that raised this: the prefix-binding shape is
    arguably cleaner at consumer call sites (no sentinel token to remember), but "arguably cleaner
    at the call site" was not enough to outweigh points 1–3 above. Recorded here, and in
    `scripts/gate-lib.sh`'s own header, so this is not rediscovered a third time.
- **2026-08-02 (lode-w5nr) — `resolve_model_revision`'s HF probe is now bounded by an explicit
  per-call timeout, closing the stall `lode-r4r2` named but could not cover.** Filed during
  `lode-r4r2`'s review: with `HF_HUB_OFFLINE` *unset* and the network black-holed (captive portal, VPN
  down, air-gapped host that never set the flag), `huggingface_hub.model_info()` blocked for the OS TCP
  connect timeout (~130s on Linux) before `resolve_model_revision`'s own `except Exception: return
  None` recorded `model_revision = NULL` anyway — no offline-flag short-circuit can see this case, since
  it fires only when the flag is set.
  - **Root cause, measured:** `huggingface_hub`'s `default_client_factory`
    (`utils/_http.py`) builds its shared `httpx.Client` with `timeout=None` — deliberately disabling
    `httpx`'s own 5s default. `HfApi.model_info()` does, however, accept a `timeout: float | None`
    keyword and forwards it straight to the per-request `get_session().get(..., timeout=timeout)` call.
    Verified empirically against a real black hole (a TEST-NET-1 address, `HF_ENDPOINT` pointed at it):
    with no `timeout` kwarg, a request to that address must be killed rather than waited out; passing
    `timeout=3.0` raised `httpx.ConnectTimeout` in ~3.02s. `httpx` honors a per-request timeout override
    even though the client itself was constructed with none. Note what a float `timeout` means to
    `httpx`: `Timeout(5.0)` sets `connect`/`read`/`write`/`pool` to 5.0 **each** — a per-phase bound, not
    a total-call one, and applied per hop under the client's `follow_redirects=True`. So `5.0` does not
    cap a slow-but-alive HF at 5s total; it caps each phase, which is why a black hole (connect only)
    returns in ~5s while a legitimately slow metadata GET is not cut short.
  - **This is option (a) of the ticket's three sketched directions.** Not (b), a lode-owned client
    factory via `set_client_factory()` — but **not for the reason first recorded here.** "It would also
    bound `fastembed`'s downloads" is refutable: `httpx.Timeout(connect=5.0, read=None, write=None,
    pool=None)` bounds exactly the black-hole phase and leaves a multi-hundred-MB read unbounded. The
    real reasons: (i) it would not be a complete bound anyway — `fastembed` fetches from its GCS mirror
    with a bare, untimed `requests.get(url, stream=True)`
    (`fastembed/common/model_management.py`), outside `huggingface_hub` entirely, so no client factory
    reaches it; and (ii) `get_session()` memoizes `_GLOBAL_CLIENT` on first use, so a factory must be
    installed before *any* hf call anywhere in the process — an import-time mutation of third-party
    global state from a library module, a worse altitude than a threaded kwarg, not a better one. lode
    owns exactly two hf call sites, so threading scales fine at n=2. Not (c) (a bounded wait wrapped
    around the call in lode's own code) either — unnecessary once (a) works.
  - **Scope: this bounds the probe, not the weights load.** It is the last unbounded hf call on a
    *warm*-cache `lode work`, which is what makes it the right fix — `fastembed`'s `download_model`
    tries `local_files_only=True` first, so a warm cache makes no network call of its own. But that
    attempt is wrapped in `except Exception: pass`, and a cold, partial, or unverifiable cache falls
    through to untimed `model_info` + `list_repo_tree` + `snapshot_download` under a retry loop. Those
    are `fastembed`'s call sites, not lode's; `HF_HUB_OFFLINE=1` (which flips `local_files_only` on) is
    still the only thing that bounds them, and `lode models pull` is where that cost belongs.
  - **What changed:** `resolve_model_revision(model_name, *, timeout_s)` (`src/lode/embedding.py`) takes
    a new required keyword-only `timeout_s`, forwarded to `model_info(hf_source, timeout=timeout_s)` —
    required, not defaulted, so a caller cannot silently reintroduce the unbounded wait by omission.
    Both production call sites thread it from the new `Settings.hf_probe_timeout_s` knob (`runtime`,
    default `5.0`, [configuration.md](configuration.md#models)): `FastEmbedEmbedder.__init__` caches
    `settings.hf_probe_timeout_s` and passes it from `model_revision()`, and `cli.py`'s
    `_model_revision_status` (the `lode status` drift check) passes it directly. `5.0` was chosen to
    match `httpx`'s own disabled default (the exact figure `huggingface_hub` turned off) rather than the
    existing `fetch_timeout_s` (`10s`) — this is a small metadata GET, not a page fetch.
  - **Blast radius, and why the fix doesn't need to change with it:** at the time this ticket was filed,
    the write path (`embed()`) constructed one `FastEmbedEmbedder` per indexed version, so a hang could
    recur once per version drained. A concurrent sibling, `lode-j5r2`, hoists ONE shared
    `FastEmbedEmbedder` across a whole `drain()`/`--loop` process, cutting that to one hang per process
    — reducing exposure, not eliminating the need for a bound: an untimed probe still blocks the first
    (and only) call for the full OS TCP timeout regardless of how many times it would otherwise have
    been made. The user-visible symptom described in this ticket (an unexplained multi-minute stall in
    `lode work` with no output) is unchanged in kind either way, just capped at once per process instead
    of once per version after `lode-j5r2` lands. `resolve_model_revision`'s call signature was extended
    (a new required kwarg), never moved or renamed, specifically so it merges cleanly regardless of
    which of the two branches lands first.
  - **Test trap avoided (third time in this function, after `lode-dj6m` / `lode-r4r2`):** a stub that
    merely raises is swallowed by `except Exception: return None` and asserts nothing about whether a
    timeout is actually applied. The acceptance tests instead capture the `timeout` kwarg
    `huggingface_hub.model_info` receives and assert its value
    (`tests/test_embedding.py::test_resolve_model_revision_forwards_timeout_s_to_model_info` and
    `::test_fast_embed_embedder_model_revision_resolves_caches_and_threads_timeout`, the latter proving
    the `Settings` knob reaches the network call through the real `FastEmbedEmbedder` seam, not just the
    bare function) — both were sabotage-tested non-vacuous by reverting the `timeout=timeout_s` argument
    and watching them fail before restoring it.
  - **A fourth instance of the same trap, found in review and fixed here:**
    `::test_resolve_model_revision_unknown_model_returns_none_no_network` had stubbed `model_info` with
    a stub raising `AssertionError("must not be reached")` — swallowed by the same `except`, so its
    `is None` assertion passed whether or not the stub ran. Proven by bypassing the `identity is None`
    early return and watching the raising form still pass; rewritten to count calls and assert
    `probe_calls == 0`. Counting, never raising, is the rule for this function.
- **2026-08-04 (lode-o7ai) — HUMAN DECISION: a `land-escalated` + `deferred` ticket stays in
  `/sweep`'s digest and current queue, but its `PushNotification` is suppressed; the report keeps
  double-listing it (the `NEW HUMAN-DECISION ITEMS` block, when it's new, plus §2a's unconditional
  deferred section) and now annotates the loud-block row `(deferred)`.** `lode-1q2i` settled only the
  FORWARD direction (a ticket entering/leaving `deferred` never itself drives `$CURRENT`/the
  digest/`PushNotification`) and deliberately left the CONVERSE open: `/sweep` §1's `land-escalated`
  query carries no `--status` filter, so a ticket that is independently BOTH `land-escalated` and
  `deferred` still flows into `$CURRENT` → the digest → (if new) the notify path, and is ALSO listed
  in §2a's unconditional deferred section. This entry is the resolution, on all three sub-questions
  the escalation that filed this ticket recorded as unsettled:
  - **Stay in `$CURRENT`/the digest? YES — §1 keeps no `--status` filter; option (a) (filter it out
    of §1) is REJECTED.** Two reasons: (i) the digest-deletion risk the ticket itself named —
    dropping the row from `$CURRENT_IDS` makes §5 see a removal and rewrite the digest WITHOUT it,
    silently deleting a real, still-open escalation from the durable cross-machine record; (ii)
    **`bd defer` is not one of `land-escalated`'s three documented resolution exits** (land-as-is /
    rebuild / drop — [agents-workflow.md](agents-workflow.md#the-landing-loop--build-review-land),
    "Resolving `land-escalated`"). Deferring therefore does not resolve the escalation — the label
    stays on — so filtering it out of §1 would leave an UNRESOLVED escalation with no surface
    anywhere in the system, defeating `/sweep`'s entire purpose rather than merely risking a
    deletion.
  - **Still notify? NO for the push, YES for the report row.** §7's `PushNotification` call is
    filtered: a row in `$NEW_IDS` whose `bd list` status is `deferred` is excluded from what gets
    pushed. Rationale: `deferred` status means a human has already said "I've seen this, deal with
    later" — re-pushing is noise about something already acknowledged. This is safe specifically
    because escalation always precedes defer in practice: `deferred` tickets are hidden from `bd
    ready` by design, so `/code` cannot pick one up to build and re-escalate it, meaning by the time
    a ticket is both `land-escalated` and `deferred`, a human necessarily saw the escalation before
    parking it. Scope: only the `PushNotification` tool call is filtered — the row still enters
    `$CURRENT`, `$CURRENT_IDS`, and the digest exactly as before, and `$NEW_IDS` itself is computed
    unfiltered, so the dedup state (what counts as "new" on a later pass) is unaffected by this
    filter.
  - **Double-listed in the report? YES, DELIBERATELY — but now annotated.** Neither the `NEW
    HUMAN-DECISION ITEMS` listing nor §2a's deferred listing is suppressed; a row appearing in both
    is information (an escalation that is parked), not redundancy needing cleanup. The `NEW
    HUMAN-DECISION ITEMS` block marks a deferred row's title with a trailing `(deferred)` so a reader
    sees at a glance why it wasn't pushed, and so the §2a duplicate reads as intentional rather than
    as a bug a later edit should tidy away. The **persisted digest body** is intentionally left
    unannotated and unfiltered — annotating it would go stale the moment a ticket's `deferred` status
    flips without its id entering or leaving `$CURRENT_IDS` (the digest only rewrites on an id-set
    change, per §5), so the annotation lives only in the freshly-recomputed, per-pass report, never
    in the persisted record.
  - **Accepted residual, recorded rather than left to be discovered:** if a deferred escalation is
    later un-deferred, it is already in `LAST_IDS` from the prior digest, so no fresh notification
    fires when it becomes active again. Accepted — the human un-deferred it themselves, so they
    already know it's back.
  - **State at decision time: latent, not live.** Verified in the 2026-07-28 `/sweep` pass that
    produced this decision: the open `land-escalated` set and the open `deferred` set had zero
    overlap. Nothing was misbehaving; this closes the gap before it can fire.
  - **Implementation:** `.claude/skills/sweep/SKILL.md` §1 (the `land-escalated` query's `jq` now
    also captures `.status`, `$ESCALATED`-only, into a 4th tab field — the value is already present
    on every row that query returns, no extra `bd` call), §7 (re-derives `$NEW_IDS` in its own
    fenced block per this skill's own cross-block-shell-state discipline — lode-sfnb / lode-x495 —
    filters the push, and produces the annotated report rows), §8 (report format documents the
    annotation and the deliberate double-listing), §2a and the Non-goals bullet (both restated to
    describe the decided behavior instead of pointing at this ticket as still open).

- **RTK (the token-optimizing command proxy) is removed from this repo — decided, done
  (2026-08-04, maintainer decision).** The `rtk` golden rule and command reference are gone from
  `CLAUDE.md`, every `rtk`-prefixed call site across `.claude/` skills and agents is now the plain
  command, `scripts/rtk-setup.sh` and the installer line in `scripts/update-tools.sh` are deleted,
  and the `Bash(rtk *)` project permission is dropped. The `rtk` tolerance was also stripped from
  the three guards that carried it (the two `PreToolUse` hooks in `.claude/settings.json` and
  `scripts/sha-fabrication-guard.sh`) and from their pinning tests — a deliberate call: with no
  `rtk` on any machine there is nothing for the alternation to match, and the residual risk is only
  a stale `rtk` binary surviving on some machine and carrying a `bd create --deps blocks:`, a `gh`
  write, or a fabricated SHA past its guard.

  **Update (2026-08-04, maintainer decision)** — this supersedes, in *effect* but not in *record*,
  every entry above that reasons about `rtk`: the `"rtk bd dolt push"` prefix-blind audit
  (`lode-bpl`), `rtk`'s reformatting of `git worktree list --porcelain` (`lode-9j7`), and the `rtk`
  member of the `gh`/`bd` guard wrapper enumerations (`lode-o29m`, `lode-9mbt`, `lode-ij24`). Those
  entries stand as written — they record what was true and why. What changed is only that the tool
  they reason about is no longer installed or referenced.

  The `git log` merge-commit caveat (`lode-eza9`) dies with it: `rtk git log` silently dropped
  `--no-ff` merge commits, which is the *only* reason `CLAUDE.md` carried a bare-`git log` exception
  and `.claude/skills/land/SKILL.md` §1 carried a call-site comment defending it. With `rtk` gone
  every `git log` is faithful, so the caveat and the comment are removed rather than preserved as a
  rule with no live cause. The residue print in `land/SKILL.md` §1 keeps its *substantive* comment
  (residue there is by construction merge commits, and the reset below destroys them) — that fact
  outlives `rtk`.

- **All three `PreToolUse(Bash)` guards extracted from inline config into tested scripts — decided,
  done (2026-08-04, maintainer decision).** The `lode-ij24` (`bd create --deps blocks:` inversion)
  and `lode-o29m`/`lode-9mbt` (external-tracker write) guards had their scanning logic inline in
  `.claude/settings.json` as ~1.4KB and ~3.3KB shell one-liners; they now live in
  `scripts/bd-deps-blocks-guard.sh` and `scripts/gh-write-guard.sh`, reached by the same thin
  wrapper shape `lode-fpmi` already shipped for `scripts/sha-fabrication-guard.sh`. Behaviour is
  unchanged — same regexes, same deny JSON, byte for byte — and the pre-existing hook-level tests
  passed untouched through the refactor, which is what establishes that. Rationale is `lode-fpmi`'s
  own acceptance criterion, applied to the two guards it left behind: *"the guard logic lives in a
  tested script, not untested inline shell"*, because ungated inline shell in config is where this
  repo has already shipped silent undetected-for-months bugs (`lode-mh9g`, `lode-54mo`). Full
  write-up: [agents-workflow.md](agents-workflow.md#all-three-pretooluse-guards-live-in-tested-scripts-not-inline-config-2026-08-04).

  **The fail-open path is new, was raised before landing, and was accepted knowingly.** Inline logic
  could not fail to run; a delegating wrapper can — if `CLAUDE_PROJECT_DIR` is unset *and*
  `git rev-parse` cannot resolve a root, or the script is missing/non-executable, the guard is
  silently skipped. The alternative considered and **rejected** was making the `gh` guard fail
  *closed* on an unresolvable script (consistent with `lode-oii9`'s reasoning for missing `jq`),
  which would brick every Bash call on such a machine. The maintainer chose fail-open for all three,
  matching `lode-fpmi`. **Accepted residual, recorded rather than left to be discovered:** on a
  machine where the root does not resolve, a `gh` write — whose whole premise is that a false allow
  is an unrecoverable public action under the user's name — is gated only by `CLAUDE.md`'s prose
  rule. Each wrapper's fail-open is pinned by a test, as is the exec bit, whose loss would otherwise
  disable a guard with every other test green.

  **`lode-9gm2`'s dash-safety bar moved with the logic** and now binds the wrapper (the part dash
  executes) rather than the collapse step, which runs under `bash "$SCRIPT"`. The static check is
  pattern-substitution-specific instead of a blanket `${` ban, since the wrapper legitimately uses
  POSIX `${CLAUDE_PROJECT_DIR:-…}`; the sabotage test proving dash dies on the bash-only form was
  retargeted at the wrapper, not dropped.

  **Update (2026-08-04, maintainer decision)** — a follow-up sweep removed the last `rtk` mention
  from `docs/` outside this file. The one site that had been *deliberately* kept — the `lode-bpl`
  paragraph in [agents-workflow.md](agents-workflow.md), which quoted the literal
  `grep -rl "rtk bd dolt push"` that the original audit ran — was rewritten to state the durable
  lesson (an enumeration that matches only one spelling of a call misses every site written
  another way) without naming the tool. Nothing was lost: the specific spelling was incidental to
  the failure, and no reader can now reach a dead tool name from an operational doc.
  **This file remains the sole exception, on purpose:** it is a dated log, so its `rtk` entries are
  the record of what was believed and when, and erasing them is exactly what its preamble forbids.
  `.claude/agents/` and `.claude/skills/` were swept in the same pass and were already clean; the
  passive `.beads/issues.jsonl` export is a historical snapshot and is never hand-edited (lode-6ra).

- **The three hand-written liveness pins stay separate, hand-written mechanisms —
  decided, rejected extraction (2026-08-04, maintainer decision, lode-7zap).** By the time
  lode-7zap closed the gap, three tests each pinned "does every allowlist/known-set entry still
  correspond to a real, live thing in a real corpus": `tests/test_land_skill_guard_coverage.py`'s
  `_dead_allowlist_entries` (exact command TEXT, keyed on a bare string, against `land/SKILL.md`'s
  fenced-bash corpus), `tests/test_skill_bash_state.py`'s `_dead_allowlist_keys` (a `(file, var)`
  tuple, matched by an unfiltered violation scan over the shipped skill/agent corpus), and
  that same module's `_dead_known_env_vars` (a bare name, matched by a used-but-unassigned scan
  with the known-set emptied, over the same corpus). The scan primitives each site uses are
  described here by what they compute, not by which function supplies them, because that plumbing
  is itself in motion — lode-dutt reworks how the two `test_skill_bash_state.py` pins obtain their
  unfiltered scan. That is compatible with, not a counterexample to, the decision below: sharing a
  genuine scan primitive between two pins that need the same scan is not the generic pin
  abstraction being rejected here. lode-e49j's own review had already argued both
  sides of extracting them into one shared helper and reached no verdict on purpose, leaving the
  question open across all three call sites; this ticket's acceptance criteria required deciding it
  in writing rather than leaving it open a fourth time — a fourth allowlist, whenever one appears,
  would otherwise face the identical unresolved question with three unexplained precedents behind it.

  **Decision: no extraction.** The three key shapes genuinely differ — a raw command string matched
  by exact text, a `(file, var)` tuple derived by a bash-fence/comment parser, and a bare name
  derived by a used-but-unassigned scan — and a shared helper that covers all three collapses to
  `assert set(known) <= live_set_from(arbitrary_compute_callable)`, an abstraction that carries the
  parameter-threading cost of genericity but no logic of its own; each site's real work (the parser,
  the filtering rule, the corpus) stays exactly as bespoke as it is today, just relocated behind an
  extra indirection. The value that actually generalizes across the three is not code, it's
  **discipline**: every hand-written allowlist/known-set in this repo gets (a) a liveness pin
  (`assert dead == []`, computed by re-running the site's own live-detection primitive unfiltered)
  and (b) a non-vacuity sabotage proof for that pin, holding one key constant and varying only the
  fixture's *content* between the live and dead assertions (never two differently-named fixtures —
  that shape passes on a name mismatch and proves nothing, lode-e49j's own measured finding, repeated
  independently for the `land/SKILL.md` guard-coverage pin by lode-7zap since it had shipped without a
  sabotage counterpart at all). This entry — not a shared module — is what codifies that discipline
  for the next allowlist a future ticket adds. Full sabotage-proof rationale for the *existing* two
  precedents:
  `tests/test_skill_bash_state.py::test_every_allowlist_entry_is_provably_checked_by_sabotage`
  (lode-e49j) and its `_KNOWN_ENV_VARS` sibling (lode-rscn); the third,
  `tests/test_land_skill_guard_coverage.py::test_every_allowlist_entry_is_provably_checked_by_sabotage`,
  is lode-7zap's own addition (it lived in `tests/test_assert_main_checkout.py` until lode-2thl
  split that module's text-gate half out).

- **The beads passive-export relpath list (`.beads/issues.jsonl`,
  `.beads/interactions.jsonl`) is canonicalized into a plain text file, not
  left as three independent hardcoded copies (2026-08-05, lode-do3q).**
  Discovered mid-review of lode-qg6g, which added the third copy: the same
  two-path list was hardcoded independently in `scripts/worktree-gc-classify.sh`'s
  `wt_provably_clean()` dirty-tree guard (a git pathspec `:(exclude)` pair —
  the code lode-9owc had already moved out of `SKILL.md` into this script by
  the time this ticket was picked up, so the ticket's original `SKILL.md:1293`
  citation is stale; the script is now the live location), the `Stop` hook's
  command string in `.claude/settings.json`, and
  `tests/test_land_lock.py`'s `_STALL_HOOK_SCAN_EXCLUDED_RELPATHS` Python set.
  Three files, three syntaxes (bash pathspec, JSON-embedded shell, Python),
  with nothing keeping them in sync — a fourth candidate already exists and is
  live rather than hypothetical (`.beads/config.yaml`'s `events-export`,
  currently `false`, would add `.beads/events.jsonl`).

  **Decision: canonicalize**, rejecting the WONTFIX the ticket flagged as a
  live possibility. The ticket's own text argued a shared *code* artifact
  couldn't span all three languages — true, but the actual content that
  drifts is not code, it's a two-line list of relpaths, and a plain
  newline-delimited text file is readable natively by both consumers that
  matter (`bash`'s `read`/`mapfile`, Python's `.read_text().splitlines()`).
  The third consumer, the `Stop` hook, was the one genuine obstacle — a JSON
  string can't `source` a file — but this repo's own `settings.json` already
  established the fix for exactly this shape: every other hook in the file
  (`bd-deps-blocks-guard.sh`, `gh-write-guard.sh`, `sha-fabrication-guard.sh`)
  shells out to a script under `scripts/` rather than inlining logic in the
  JSON string. Applying that same pattern here — extracting the hook's one
  line of logic into `scripts/discard-beads-passive-export-churn.sh`, which
  itself reads the canonical list — removed the obstacle rather than forcing
  an abstraction across it.

  **Mechanism:** `scripts/beads-passive-exports.txt` (one relpath per line)
  is now the single canonical copy. `scripts/worktree-gc-classify.sh`'s
  `wt_provably_clean()` reads it to build its `:(exclude)` pathspec list;
  `scripts/discard-beads-passive-export-churn.sh` (new, called from the
  `Stop` hook) reads it to build its `git checkout HEAD --` argument list;
  `tests/test_land_lock.py`'s `_STALL_HOOK_SCAN_EXCLUDED_RELPATHS` reads it
  directly into the set. **Adding** a passive export is now a one-file edit.
  No behaviour changed at any of the three sites — each still excludes exactly
  the same two relpaths it did before, verified by building the pathspec list
  standalone and diffing it against the retired literals.

  **Scope, stated precisely so the next reader is not misled:** what is
  canonical is the *exclusion-list* trio above, not every mention of these
  paths in the repo. A separate cluster still names `.beads/issues.jsonl`
  literally, under a different verb (`git restore --staged --worktree`, i.e.
  *unstage before merging*, not *exclude from a judgment*) and naming only the
  one path: `scripts/land-merge-one.sh`, the executable bash blocks in
  `.claude/skills/land/SKILL.md` and `.claude/skills/release/SKILL.md`, and the
  command-string allowlist entry in `tests/test_assert_main_checkout.py`. So a
  *rename* is NOT yet a one-file edit. Bringing that cluster on is deliberately
  left out of scope here — it is a different operation with a different failure
  mode — and is filed separately.

  **What canonicalizing cost, since it is not free.** It replaced three
  self-contained literals with an indirection chain
  (`settings.json` → script → data file) in which every link swallows its own
  errors: the `Stop` hook ends in `; true` and its script always exits 0. That
  is right for best-effort hygiene, but it means a rename or deletion anywhere
  along the chain leaves the hook a permanent no-op with nothing red — a
  failure mode the inline copies could not have had. Two mitigations, both
  added during technical review rather than left to the next incident:
  `scripts/worktree-gc-classify.sh` is a *gate*, so it fails LOUD (exit 2) on
  an unreadable or empty list instead of degrading to an empty exclude set,
  which would silently invert lode-bns3; and `tests/test_beads_passive_exports.py`
  pins the whole chain — the list is non-empty and well-formed, the `Stop` hook
  still names an existing executable script, both bash consumers still read the
  canonical file, and no consumer has re-inlined a literal copy.
- **2026-08-05 (lode-k4as) — KEPT: `scripts/gate-lib.sh`'s header narrative (history + rejected
  alternatives) stays in the header, not moved to `docs/agents-workflow.md`.** Filed while
  technically reviewing `lode-nwqb` (its entry is above; search `lode-nwqb`), which had just added
  18 more comment lines to an already-long header. Measured at this entry's date (no line numbers
  cited — they rot, `lode-pxyt`): roughly 170 lines of header comment sit above the first executable
  line, for roughly 50 lines of code. Each increment was individually justified; the concern is the
  aggregate — a reader tracing what the positional arguments do wades through two mechanisms that no
  longer exist (the retired ordering convention, the rejected `lode-nwqb` prefix-binding
  alternative) before reaching the one that does.
  - **What this has to answer for**: `lode-ysr6`'s entry (above, just before `lode-nwqb`'s) declared
    the `GATE_ADVISORY` section the *operative* record of the contract, corrected in place, precisely
    so the contract could not drift from the code it governs. Moving the narrative out from under
    that declaration would reverse it, so this decision reckons with it explicitly rather than
    silently — see reason 1.
  - **Decided: no move.** Three reasons:
    1. **The split introduces exactly the divergence risk the operative-record declaration exists to
       prevent, with no mechanism proposed to replace it.** `lode-ysr6`'s discipline is "the header IS
       the contract, correct it in place" — a scheme with nothing to keep in sync. Moving the
       "WEIGHED AND REJECTED" and "THIS USED TO BE AN ORDERING CONVENTION" paragraphs to
       `docs/agents-workflow.md` creates two documents that must be edited together every time the
       mechanism changes again, and nothing mechanical (no test, no lint) would catch the header and
       the doc drifting apart — the exact failure class `lode-ysr6` was written to close, reopened one
       level up.
    2. **The rejected-alternative paragraphs are not incidental history — they are what stops the
       mechanism being re-litigated at the point someone would reach for the shortcut.** The
       `lode-nwqb` entry says so directly — it was recorded both here and in the header "so this is
       not rediscovered a third time." That purpose is served by sitting next to the code a future
       editor is about to change, not by living a `grep` away in a different file a reader has to
       know to check before "simplifying" `gate-lib.sh`.
    3. **Length is not itself a defect.** The ticket that raised this said so explicitly, and nothing
       in this decision found a concrete cost the length imposes beyond "it reads as long" — no test
       failure, no maintenance incident, no reader confusion beyond the hypothetical traced above.
       "Long but correct and load-bearing" does not clear the bar for moving an operative record.
  - **Not a permanent bar on ever splitting this file.** If a future increment pushes the header
    materially past this size, or a concrete reading-cost incident turns up (not merely "it's long"),
    that is grounds to revisit — but the revisit has to name the specific cost and the specific
    anti-drift mechanism the split would use, the same bar this entry just failed to find a reason to
    clear. Until then this is not re-raised as a defect each time a new paragraph lands in the header.
  - No code or test changes: `scripts/gate-lib.sh` and `tests/test_gate_lib.py` are unchanged by this
    ticket.

- **A repo-wide shared job-row factory for tests, or file-local helpers?** Many `tests/` modules
  still write raw `INSERT INTO jobs` inline; a single row factory in `tests/conftest.py` is the
  fully-consolidated end state. Leaning **all-or-nothing**: a partial hoist covering only the
  files a cleanup ticket happens to touch, while the rest keep inline INSERTs, is a worse end
  state than either "all local" or "all shared" — it adds a second convention for a future editor
  to choose between without retiring the raw-SQL pattern anywhere else. Revisit as its own ticket
  scoped to every such file at once, never as an incidental expansion riding on a narrower
  cleanup. **Applied-for-`lode-3en5`:** folding `tests/test_enrich.py`'s `_insert_done_enrich_job`
  into the sibling `_insert_enrich_job` (hoisted to that file's Helpers section by `lode-z1e7`)
  stayed file-local on this leaning.
