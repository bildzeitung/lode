# FIND-quality validation: recall reliability (lode-p5gf) + behavior-preserving blind spot (lode-eohb)

> **RETIRED — NOT EXECUTABLE (2026-08-09, lode-blrl).** The workflow this runbook drives,
> `.claude/workflows/correctness-review.js`, has been **deleted from the tree**: `lode-rlyx` took it off
> the `/code` path on measured cost, and it recorded **zero** manual runs in the thirteen days that
> followed, so it was retired outright rather than kept as unreachable machinery. Every step below
> refers to a script that no longer exists; none of it can be run as written. **lode-p5gf and lode-eohb
> are both closed.** This file is kept only so `specs/` stays a gap-free numbered sequence — it is a
> historical record of how FIND quality was to be validated, not work to pick up. Full source if it is
> ever reconstructed:
> `git show 974f832246cd4d42ca002f5bc8e21c40ad2148a6:.claude/workflows/correctness-review.js`.
> Decision record: [`docs/decisions.md`](../docs/decisions.md).

This is a **runbook**, not a feature spec. It exists so a human (or the maintainer's own
interactive session) can finish the acceptance bar on **lode-p5gf** and **lode-eohb** that no
dispatched `coding`/`code-reviewer` subagent can do (neither reaches the `Workflow` tool — verified
empirically by both tickets' own builds), and then record the results on each ticket. Delete it
once both results are recorded.

The two tickets are validated together because the setup (a Workflow-capable session, an extracted
script, saved run outputs) is identical — not because the fixes are the same thing. **Part A**
(lode-p5gf) measures whether K-round FIND union stabilizes recall across repeated runs of the SAME
diff. **Part B** (lode-eohb) measures whether a general FIND-prompt instruction — treat a diff's own
"behavior-preserving" self-claim as a claim to disprove, not a fact — recovers a specific, previously
**missed and inverted** finding (a Q&A timeout regression hidden behind an implicit SDK default).
Run either part independently, or both in one sitting; neither depends on the other's result.

## Prerequisites (both parts)

1. **A Workflow-capable session.** Run this from your normal interactive Claude Code session (the
   main session), NOT a dispatched subagent — only the main session has the `Workflow` tool
   (verified empirically, lode-905v; re-confirmed by both lode-p5gf's and lode-eohb's own builds,
   neither of which could reach it). Sanity check before starting: ask the session to confirm
   `Workflow` resolves (`ToolSearch` with `select:Workflow`). If it does NOT resolve even here, STOP
   and record that on the relevant ticket — that is itself the finding.
2. The scripts live on unlanded branches, not trunk — each part's Step 0 extracts the one it needs
   to a stable temp path rather than checking a branch out, so you can stay on whichever branch
   you're actually reviewing for other work.

---

## Part A — lode-p5gf: FIND recall is stochastic run-to-run

### Status going in

- `.claude/workflows/correctness-review.js` (currently on branch **`land/lode-905v` @ `3a0d6a4`**
  merged forward with lode-p5gf's own commits on top, NOT yet on trunk — see lode-p5gf's
  `builds_on` metadata) was changed to run each dimension's FIND prompt `FIND_ROUNDS` times (2, a
  starting default) and union near-duplicate findings via a shared `mergeNearDuplicates` helper,
  instead of a single FIND pass per dimension.
- The **known reliability problem** this mitigates: across three prior runs of the identical
  gpzn.13 retrospective (refRange `77960d8...22e4341`, baseline `b70be43`) with the
  *pre-mitigation* (single-round) script — run 1 found both baseline bugs, run 2 missed the
  tombstone bug in all six finders, run 3 (with lode-905v's other fixes but still single-round
  FIND) found it in four of six. Recall varies run-to-run; a single pass can silently under-report.
- **What is validated so far: nothing empirically.** The mitigation's logic (the merge function
  itself — same-dimension round union, cross-dimension REPORT dedup, severity and provenance
  bookkeeping) was hand-verified against synthetic scripted `agent()` responses in a throwaway
  harness (not committed — this script has no project-owned test runner; see lode-p5gf's hand-off
  for what was checked). That is NOT the same as measuring real recall against a real, stochastic
  model. This part is that measurement.
- **`FIND_ROUNDS = 2` is a starting default, not a validated optimum.** If this runbook's data says
  2 rounds isn't enough (or that more would clearly help at an acceptable token cost), retune the
  constant in the script and re-run before declaring the ticket solved.

### Step A0 — set up

```bash
mkdir -p specs/p5gf-recall-results

# Extract the mitigated script from lode-p5gf's pushed branch to a stable temp path.
git fetch -q origin land/lode-p5gf
git show origin/land/lode-p5gf:.claude/workflows/correctness-review.js \
  > specs/p5gf-recall-results/correctness-review.js

# Confirm the retrospective range is exactly the one lode-905v's own runs used:
git log --oneline 77960d8...22e4341
```

### Step A1 — run the retrospective N≥5 times

Run the workflow **at least 5 times** on the identical range, saving each result:

```
Workflow({ scriptPath: "specs/p5gf-recall-results/correctness-review.js",
           args: { refRange: "77960d8...22e4341" } })
```

After each run, save the returned result JSON:

```
specs/p5gf-recall-results/run1.json
specs/p5gf-recall-results/run2.json
specs/p5gf-recall-results/run3.json
specs/p5gf-recall-results/run4.json
specs/p5gf-recall-results/run5.json
... (run6+, if you go beyond the N=5 floor)
```

(If a run throws at load, that is a defect — capture the error and stop; do not "fix" it inline to
make it pass.)

### Step A2 — score per-run recall against the known baseline

The two baseline findings (from lode-905v's own retrospective against the real `/code-review`-era
commit `b70be43`) are:

1. The **tombstone** finding (a hard-delete leaving a tombstone reachable) — treated as
   Low/latent-defensive in the human baseline.
2. The **missing regression test** finding — Medium.

For each of the N runs, record:

- Did the tombstone finding survive refutation? (yes/no, and which dimension(s)/rounds flagged it
  per `flaggedByDims`/`foundInRounds` on the surviving entry)
- Did the missing-test finding survive refutation? (yes/no)
- `stats.totalRaw` / `stats.falsePositiveRate` for that run, for a sanity check that the mitigation
  isn't trading recall for a flood of noise.

Produce a table: recall-per-run for both findings, and the aggregate "found in ≥1 of N runs" figure
(the bar lode-905v's own retrospective used), but the number that actually answers lode-p5gf's
question is the **per-run** figure — report both.

### Step A3 — verdict, and record it on lode-p5gf

- **If per-run recall is now consistently high** (materially better than the pre-mitigation baseline
  of "0/6 finders in one run"), the mitigation is validated at the K=2 default. Record the full
  per-run table on lode-p5gf, close the ticket's empirical-validation gap, and reassess whether it
  can move toward landing.
- **If recall still varies unacceptably at K=2**, that is itself the finding — do not paper over it.
  Consider whether the data supports bumping `FIND_ROUNDS` (re-run this same procedure at the higher
  K to confirm before committing to it — do not guess), and say so plainly on lode-p5gf. Leave it
  open rather than declaring it solved on partial evidence — the acceptance criteria says exactly
  this.
- Either way: append the full result (the per-run table, the verdict, and whatever `FIND_ROUNDS`
  value the data supports) to lode-p5gf.

---

## Part B — lode-eohb: the "behavior-preserving" blind spot

### Status going in

- `.claude/workflows/correctness-review.js` (on branch **`land/lode-eohb`**, built directly on
  trunk's landed lode-905v version — NOT stacked on lode-p5gf's unlanded K-round mitigation; see
  "Combining with Part A" below if you want both fixes exercised in the same run) had a general
  instruction added to the FIND prompt (applies identically to all six dimensions, not a
  timeout-specific carve-out): a diff's own "behavior-preserving" / "no-op" / "pure refactor"
  self-description is a claim to **disprove**, not accept — establish the prior (base-of-range)
  behavior independently for every changed call, **including implicit library/SDK defaults**
  (timeouts, retries, pagination, …), before concluding a change is benign.
- **The defect this targets**, and its exact reproducible evidence: reviewing
  `land/lode-568v.2 @ fe31ecf` (the "behavior-preserving LLMProvider seam" refactor) live, a
  keystroke `/code-review high` flagged as its DOMINANT, highest-severity finding (raised 4x): the
  Q&A synthesis call's effective timeout silently dropped 600s → 120s (trunk relied on the
  Anthropic SDK's own default client timeout of 600s; the diff pins `llm_call_timeout_s=120` and
  extends it from `enrich.py`-only to also cover `qa.py`). **Across all 3 correctness-review runs
  against this exact diff, every FIND agent touched the same `qa.py` lines and reached the OPPOSITE
  conclusion** — asserting trunk had "no timeout / unbounded" and that the change was benign
  hang-protection — which then correctly got refuted as "just needs a test coverage", burying the
  real regression. Not stochastic (lode-p5gf's concern): every run made the identical
  mischaracterization from the diff's own framing, never checked the SDK's actual default.
- **Fixture note — the raw `/code-review` baseline text and the 3 saved workflow-run JSONs
  (`specs/905v-live-results/`) that lode-905v's own hand-off said would be "preserved" and
  "committed onto the FIND-quality follow-up branch" were searched for across every worktree on
  this machine and do not exist anywhere** — only the *scored*, finding-by-finding table survived,
  pasted verbatim into `bd show lode-905v`'s notes (reproduced below so this runbook is
  self-contained). Whoever runs this part is re-running the workflow against the *code*, not
  replaying saved model output, so this is not a blocker — but it means the original raw runs
  cannot be diffed against the new ones; only the scored baseline table below is available for
  comparison.

### The known baseline (from `bd show lode-905v`, reproduced here)

Reviewed range (verified reproducible — see Step B0): `51dc7c2...fe31ecf`, i.e. `trunk...HEAD` at
the time `land/lode-568v.2`'s tip was `fe31ecf`, before its later review-fix commit `be1594f`.

`/code-review high` baseline (5 findings):

| # | Location | Finding | Baseline verdict |
|---|---|---|---|
| B1 | `qa.py:188` | Q&A synthesis timeout cut 600s → 120s | CONFIRMED correctness (DOMINANT, flagged 4x) |
| B2 | `config.py:988` | `ModelTier` prints pydantic repr in `lode config` | CONFIRMED correctness |
| B3 | `llm_provider.py:345` | unguarded `RootModel` wrap aborts batch collection | PLAUSIBLE correctness |
| B4 | `enrich.py:811` | missing-tool_use error path diverged across 2 files | CONFIRMED cleanup (out of scope) |
| B5 | `llm_provider.py:261` | forced-tool-use params dict duplicated | CONFIRMED cleanup (out of scope) |

Pre-fix workflow recall (3 runs, single-round FIND): B1 **MISSED and INVERTED 0/3** (the finding
this ticket's fix targets); B2 hit 2/3; B3 hit 1/3 (thin, consistent with lode-p5gf's stochastic
recall, not this ticket's concern); B4/B5 out of scope (cleanup, not correctness). **Zero false
positives** across all 3 pre-fix runs — the refute stage correctly killed every non-bug raised.

B4 and B5 are cleanup findings, explicitly out of the correctness workflow's declared scope (that's
`/simplify`'s territory) — do not count them against recall either way.

### Step B0 — set up

```bash
mkdir -p specs/eohb-find-quality-results

# Extract the FIND-quality-fixed script from lode-eohb's pushed branch.
git fetch -q origin land/lode-eohb
git show origin/land/lode-eohb:.claude/workflows/correctness-review.js \
  > specs/eohb-find-quality-results/correctness-review.js

# Confirm the range reproduces the exact live-benchmark diff (should show the same 5 files
# B1-B5 cite: qa.py, config.py, llm_provider.py, enrich.py, worker.py, plus tests/docs):
git diff --stat 51dc7c2...fe31ecf
```

### Step B1 — dev-loop re-run against the saved fe31ecf baseline

Run the workflow **3×** (matching the original live benchmark's run count) on the identical range:

```
Workflow({ scriptPath: "specs/eohb-find-quality-results/correctness-review.js",
           args: { refRange: "51dc7c2...fe31ecf" } })
```

Save each result:

```
specs/eohb-find-quality-results/run1.json
specs/eohb-find-quality-results/run2.json
specs/eohb-find-quality-results/run3.json
```

(If a run throws at load, that is a defect — capture the error and stop; do not "fix" it inline to
make it pass.)

### Step B2 — score against the baseline table above

For each of the 3 runs, and in aggregate ("found in ≥1 of 3", the bar the original live benchmark
used), record:

- **B1 (the target of this fix)**: does the timeout finding now survive VERIFY, at `qa.py`, citing
  the 600s→120s SDK-default-vs-explicit-pin distinction (not a vague "add hang protection" framing,
  which is the pre-fix inverted conclusion)? This is the load-bearing number — if it's still
  0/3, the fix did not close the gap it was built for.
- **B2 / B3**: still recalled at a rate consistent with (or better than) the pre-fix baseline
  (2/3, 1/3)? A regression here (the general instruction somehow *suppressing* an unrelated finding)
  would be a real problem, not just a null result.
- **False positives**: is `stats.falsePositiveRate` still 0% shipped-survivor noise? The fix asks
  FIND agents to do more independent verification work per changed call — check this hasn't turned
  into speculative candidates that survive VERIFY without being real (the ticket's own stated
  tradeoff to watch).

### Step B3 — verdict, and record it on lode-eohb

- **If B1 is now recalled in ≥1 of 3 runs, with 0 new false positives and B2/B3 not regressed**,
  the fix is validated. Record the full per-run table on lode-eohb.
- **If B1 is still missed in all 3 runs**, the fix did not close the gap — say so plainly, and
  reconsider the prompt wording (or whether the underlying model simply won't check an implicit
  library default without a stronger nudge) rather than declaring success on a null result.
- Either way: append the result to lode-eohb, and only then reassess whether lode-eohb can move
  from `land-escalated` toward `ready-for-land`.

### Combining with Part A (optional, not required for either ticket's own acceptance)

Both tickets' acceptance criteria are satisfiable independently against their own script version
(p5gf's on top of 905v; eohb's on top of 905v, not stacked on p5gf). If you additionally want to see
both fixes exercised together in one script (closer to what production will eventually run once both
land), merge `land/lode-p5gf` into a scratch checkout of `land/lode-eohb` yourself, extract the
merged `.claude/workflows/correctness-review.js`, and re-run Part B's Step B1 against it — this is a
judgment call for whoever runs this part, not a hard requirement of either ticket, and is not a
substitute for the independent validations above.

---

## After both parts are recorded

Delete `specs/p5gf-recall-results/`, `specs/eohb-find-quality-results/`, and this spec
(`specs/12-correctness-review-recall-validation.md`) once both results are on their respective
tickets — they are transient working artifacts; the durable record is the tickets themselves.
