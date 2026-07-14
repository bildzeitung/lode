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
  `debate/SKILL.md` (human-invoked/interactive, a failed push is observed), `.beads/README.md` and
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
  direct `bd close`). A `/debate` pass considered auto-closing an epic once every filed gap child had
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
  builder's local worktree after a clean land, so the builder keeps recording it. `/code`'s step-1
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
  `review_worktree` GC still finds it.

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
  ancestry** (`git merge-base --is-ancestor <HEAD-sha> trunk`) plus `unlocked` — no branch-name
  pattern is matched by the worktree sweep any more (see the lode-em6v update above, which
  describes the same unification). The **second backstop** decided here — deleting a dangling local
  `land/<id>` ref whose `origin/land/<id>` counterpart is gone — is a separate, bare-**ref** sweep,
  unaffected by that unification: it still matches `refs/heads/land/*` by name, and must, since
  `refs/heads/*` is shared with human branches. Mechanism of record:
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
  close), which is exactly why it is its own ticket.

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
