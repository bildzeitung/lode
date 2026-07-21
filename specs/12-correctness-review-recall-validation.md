# FIND-recall reliability validation (lode-p5gf)

This is a **runbook**, not a feature spec. It exists so a human (or the maintainer's own
interactive session) can finish the acceptance bar on **lode-p5gf** that no dispatched
`coding`/`code-reviewer` subagent can do, and then record the result on lode-p5gf. Delete
it once the result is recorded there.

## Status going in

- `.claude/workflows/correctness-review.js` (currently on branch **`land/lode-905v` @
  `3a0d6a4`** merged forward with lode-p5gf's own commits on top, NOT yet on trunk — see
  lode-p5gf's `builds_on` metadata) was changed to run each dimension's FIND prompt
  `FIND_ROUNDS` times (2, a starting default) and union near-duplicate findings via a
  shared `mergeNearDuplicates` helper, instead of a single FIND pass per dimension.
- The **known reliability problem** this mitigates: across three prior runs of the
  identical gpzn.13 retrospective (refRange `77960d8...22e4341`, baseline `b70be43`) with
  the *pre-mitigation* (single-round) script — run 1 found both baseline bugs, run 2
  missed the tombstone bug in all six finders, run 3 (with lode-905v's other fixes but
  still single-round FIND) found it in four of six. Recall varies run-to-run; a single
  pass can silently under-report.
- **What is validated so far: nothing empirically.** The mitigation's logic (the merge
  function itself — same-dimension round union, cross-dimension REPORT dedup, severity
  and provenance bookkeeping) was hand-verified against synthetic scripted `agent()`
  responses in a throwaway harness (not committed — this script has no project-owned test
  runner; see lode-p5gf's hand-off for what was checked). That is NOT the same as
  measuring real recall against a real, stochastic model. This runbook is that
  measurement.
- **`FIND_ROUNDS = 2` is a starting default, not a validated optimum.** If this runbook's
  data says 2 rounds isn't enough (or that more would clearly help at an acceptable token
  cost), retune the constant in the script and re-run before declaring the ticket solved.

## Prerequisites

1. **A Workflow-capable session.** Run this from your normal interactive Claude Code
   session (the main session), NOT a dispatched subagent — only the main session has the
   `Workflow` tool (verified empirically, lode-905v; re-confirmed by lode-p5gf's own
   build, which could not reach it either). Sanity check before starting: ask the session
   to confirm `Workflow` resolves (`ToolSearch` with `select:Workflow`). If it does NOT
   resolve even here, STOP and record that on lode-p5gf — that is itself the finding.
2. **The mitigated script on disk.** It lives on lode-p5gf's own branch
   (`land/lode-p5gf`, built on top of `land/lode-905v`), not trunk. Extract it to a stable
   temp path (Step 0) rather than checking the branch out, so you can stay on whichever
   branch you're actually reviewing for other work.
3. **The SAME gpzn.13 range used by lode-905v's own retrospective**, so results are
   directly comparable to the three prior runs cited above: `77960d8...22e4341`.

## Step 0 — set up

```bash
mkdir -p specs/p5gf-recall-results

# Extract the mitigated script from lode-p5gf's pushed branch to a stable temp path.
git fetch -q origin land/lode-p5gf
git show origin/land/lode-p5gf:.claude/workflows/correctness-review.js \
  > specs/p5gf-recall-results/correctness-review.js

# Confirm the retrospective range is exactly the one lode-905v's own runs used:
git log --oneline 77960d8...22e4341
```

## Step 1 — run the retrospective N≥5 times

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

(If a run throws at load, that is a defect — capture the error and stop; do not "fix" it
inline to make it pass.)

## Step 2 — score per-run recall against the known baseline

The two baseline findings (from lode-905v's own retrospective against the real
`/code-review`-era commit `b70be43`) are:

1. The **tombstone** finding (a hard-delete leaving a tombstone reachable) — treated as
   Low/latent-defensive in the human baseline.
2. The **missing regression test** finding — Medium.

For each of the N runs, record:

- Did the tombstone finding survive refutation? (yes/no, and which dimension(s)/rounds
  flagged it per `flaggedByDims`/`foundInRounds` on the surviving entry)
- Did the missing-test finding survive refutation? (yes/no)
- `stats.totalRaw` / `stats.falsePositiveRate` for that run, for a sanity check that the
  mitigation isn't trading recall for a flood of noise.

Produce a table: recall-per-run for both findings, and the aggregate "found in ≥1 of N
runs" figure (the bar lode-905v's own retrospective used), but the number that actually
answers lode-p5gf's question is the **per-run** figure — report both.

## Step 3 — verdict, and record it on lode-p5gf

- **If per-run recall is now consistently high** (materially better than the pre-mitigation
  baseline of "0/6 finders in one run"), the mitigation is validated at the K=2 default.
  Record the full per-run table on lode-p5gf, close the ticket's empirical-validation gap,
  and reassess whether it can move toward landing.
- **If recall still varies unacceptably at K=2**, that is itself the finding — do not
  paper over it. Consider whether the data supports bumping `FIND_ROUNDS` (re-run this
  same procedure at the higher K to confirm before committing to it — do not guess), and
  say so plainly on lode-p5gf. Leave it open rather than declaring it solved on partial
  evidence — the acceptance criteria says exactly this.
- Either way: append the full result (the per-run table, the verdict, and whatever
  `FIND_ROUNDS` value the data supports) to lode-p5gf.
- Delete `specs/p5gf-recall-results/` and this spec (`specs/12-...`) once the result is on
  lode-p5gf — they are transient working artifacts; the durable record is the ticket.
