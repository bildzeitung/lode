# Live benchmark: correctness-review workflow vs a real `/code-review` (lode-905v)

> **RETIRED — NOT EXECUTABLE (2026-08-09, lode-blrl).** The workflow this runbook drives,
> `.claude/workflows/correctness-review.js`, has been **deleted from the tree**: `lode-rlyx` took it off
> the `/code` path on measured cost, and it recorded **zero** manual runs in the thirteen days that
> followed, so it was retired outright rather than kept as unreachable machinery. Every step below
> refers to a script that no longer exists; none of it can be run as written. **lode-905v is closed.**
> This file is kept only so `specs/` stays a gap-free numbered sequence — it is a historical record of
> how the workflow was to be validated, not work to pick up. Full source if it is ever reconstructed:
> `git show 974f832246cd4d42ca002f5bc8e21c40ad2148a6:.claude/workflows/correctness-review.js`.
> Decision record: [`docs/decisions.md`](../docs/decisions.md).

This is a **runbook**, not a feature spec. It exists so a human can finish the one
acceptance item on **lode-905v** that no model context can do, and then hand the
captured outputs to a fresh agent session to score. Delete it once the result is
recorded on lode-905v.

## Status going in

- lode-905v built `.claude/workflows/correctness-review.js` — a multi-agent
  correctness gate (FIND per dimension → refute-biased VERIFY → REPORT). It lives on
  branch **`land/lode-905v` @ `3a0d6a4`**, NOT on trunk (not yet landed).
- The **retrospective** acceptance comparison is **DONE and green**: 2/2 recall
  against the real `/code-review`-era baseline `b70be43` (gpzn.13,
  `refRange 77960d8...22e4341`). That is the stronger of the two required tests.
- The **live head-to-head** is the ONLY remaining acceptance item. It needs a human
  keystroke because Claude Code 2.1.215 removed model invocation of `/code-review`
  (lode-axyq) — no agent can type it.
- Known reliability caveat, filed separately as **lode-p5gf** (do not re-file):
  FIND recall is **stochastic run-to-run** — one earlier run missed a real bug in all
  six finders. That is why Step 2 runs the workflow **3×**, not once.

## Prerequisites

1. **A Workflow-capable session.** Run this from your normal interactive Claude Code
   session (the main session), NOT a dispatched subagent — only the main session has
   the `Workflow` tool. Sanity check before starting: ask the session to confirm
   `Workflow` resolves (it can try `ToolSearch` with `select:Workflow`). If it does
   NOT resolve even here, STOP and record that on lode-905v — that is itself the
   finding, and the whole approach needs rethinking.
2. **The workflow script on disk.** It is on `land/lode-905v`, not trunk. Extract it
   to a temp path (Step 0) rather than checking the branch out, so you can stay on the
   branch you actually want to review.
3. **A real in-flight branch with a genuine CODE diff vs trunk** (not docs-only). The
   more real correctness surface the diff has, the more meaningful the test.

## Step 0 — set up

```bash
# From the repo root, on the branch you want to review live.
mkdir -p specs/905v-live-results

# Extract the (fixed) workflow script from the land branch to a stable temp path.
# Using origin/ so you get the pushed 3a0d6a4 tip regardless of local state.
git fetch -q origin land/lode-905v
git show origin/land/lode-905v:.claude/workflows/correctness-review.js \
  > specs/905v-live-results/correctness-review.js

# Confirm the range you will review is non-empty and is real code:
git diff --stat trunk...HEAD
```

If `git diff --stat trunk...HEAD` is empty or docs-only, switch to a branch with a
real code diff before continuing.

## Step 1 — capture the `/code-review` baseline (KEYSTROKE — only you can do this)

In the interactive session, type by hand:

```
/code-review high trunk...HEAD
```

When it finishes, have the session write its findings **verbatim** to a file:

```
specs/905v-live-results/code-review-baseline.md
```

(Ask the session: "save the full `/code-review` output above verbatim to
`specs/905v-live-results/code-review-baseline.md`.") This is the gold standard the
workflow is measured against.

## Step 2 — run the workflow on the IDENTICAL range, 3×

Run the workflow tool three times (stochasticity, per lode-p5gf), saving each result:

```
Workflow({ scriptPath: "specs/905v-live-results/correctness-review.js",
           args: { refRange: "trunk...HEAD" } })
```

After each run, save the returned result JSON to:

```
specs/905v-live-results/workflow-run1.json
specs/905v-live-results/workflow-run2.json
specs/905v-live-results/workflow-run3.json
```

(The result is the `findings` / `refuted` / `stats` object the tool returns. If a run
throws at load, that is a defect — capture the error and stop; do not "fix" it inline
to make it pass.)

## Step 3 — deliver the outputs / reconvene

You do NOT score it yourself. Start a **fresh** Claude Code session and say:

> Read `specs/11-correctness-review-live-benchmark.md` and the files under
> `specs/905v-live-results/`. Produce the finding-by-finding comparison per the
> "Scoring bar" section and append the result to lode-905v. Do not declare parity on
> partial evidence.

The fresh session has everything it needs: this spec, the baseline, and the 3 runs.
That is how "we see the full picture together after" — the comparison is reproducible
from the saved files, not from anyone's memory of the run.

## Scoring bar (what the fresh session must apply)

The bar is **recall of real bugs at an acceptable false-positive rate — NOT
finding-count parity.** A workflow that reports MORE than `/code-review` is not
thereby better; unverified noise is the failure mode the refute stage exists to guard.

Produce a table:

- **Recall.** For each `/code-review` baseline finding: did ANY of the 3 workflow runs
  surface it? (found-in-run-N). Report recall as "baseline findings hit by ≥1 run" and
  also note per-run recall, since a single pass can miss a real bug (lode-p5gf).
- **False positives.** For each workflow finding NOT in the baseline: is it a real bug
  or noise? Judge it on its merits (read the cited code AT the reviewed commit, never
  the working tree — same lesson that the retrospective exposed).
- **Severity sanity.** Note any finding where the workflow's severity is clearly off.

Then a verdict: does live recall clear the bar? Append the whole thing to lode-905v.

## After it is recorded

- **If recall clears the bar:** lode-905v's acceptance is met. Move it out of
  `land-escalated` → `ready-for-land` so `/land` takes `land/lode-905v @ 3a0d6a4`.
  Heads-up: the moment it lands, `/code` Phase 2 starts running this gate on every
  pass — as an **additive backstop** (it hands findings to the code-reviewer, whose
  own hand-reasoned pass runs regardless), so it can only add findings, never suppress
  the reviewer's own work.
- **If recall underperforms:** say so plainly on lode-905v and keep it open — do not
  ship a weaker gate. The likely culprit is FIND stochasticity (lode-p5gf); that is the
  ticket to pick up next.
- Either way: delete `specs/905v-live-results/` and this spec (`specs/11-...`) once the
  result is on lode-905v — they are transient working artifacts, and the durable record
  is the ticket.
