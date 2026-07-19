# lode — Open decisions (deferred, not forgotten)

*(§9)* Decisions deliberately left open, with the current leaning where there is one. Revisit each
when the build reaches the feature that forces it. The tunable parameters several of these reference
are catalogued in [configuration.md](configuration.md).

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
  [agents-workflow.md](agents-workflow.md#the-landing-loop--build-review-land-planned) — all landing
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
  branch is otherwise never touched by an automated sweep — only the three human-driven resolution
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
  builder's local worktree after a clean land, so the builder keeps recording it. **(Superseded by
  lode-h1vn, below: that GC loop is deleted, so `review_worktree` is now vestigial outright — recorded
  by the builder, read by nobody. The builder's worktree is still reclaimed after a clean land, but by
  the backstop sweep, which discovers it from `git worktree list` instead.)** `/code`'s step-1
  stranded-review guard is re-keyed onto `metadata.review_head` instead (the field the reviewer
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

  **Explicitly out of scope**, filed as a follow-up (lode-3ci): whether the builder still needs to
  *keep* its worktree at all now that neither the reviewer nor a rebase pickup opens it, and whether
  `/land`'s worktree GC should change as a result. **Resolved below — kept as-is.**

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
  `review_worktree` GC still finds it. **(Superseded by lode-h1vn, below: `/land` reclaims the
  builder's worktree via its backstop sweep now — the `review_worktree`-keyed loop is deleted. The
  guarantee is unchanged; only the mechanism is.)**

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
  exemptions) is unchanged. **Superseded for the matching *shape* (not the `jq` question) by the
  `lode-9mbt` entry below**, which inverts that surface from a denylist to an allowlist.

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
    evidence supports and is explicitly **deferred, not taken**. This kills the 3 existing name
    collisions: `tui/{ask,capture,reconcile}.py` vs. `tui/screens/{ask,capture,reconcile}.py`
    (disambiguated only by import path today).
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
