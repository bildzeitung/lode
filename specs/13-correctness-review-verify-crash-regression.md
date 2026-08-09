# Regression check: a crashed VERIFY agent must surface as `unverified`, never `refuted` (lode-wtwb)

> **RETIRED — NOT EXECUTABLE (2026-08-09, lode-blrl).** The workflow this runbook drives,
> `.claude/workflows/correctness-review.js`, has been **deleted from the tree**: `lode-rlyx` took it off
> the `/code` path on measured cost, and it recorded **zero** manual runs in the thirteen days that
> followed, so it was retired outright rather than kept as unreachable machinery. Every step below
> refers to a script that no longer exists; none of it can be run as written. **lode-wtwb is closed**,
> and its companion ticket `lode-dwtp` — filed to make this regression demonstrable on demand, which
> Step 3 below records as INCONCLUSIVE — was closed unbuilt in the same retirement. This file is kept
> only so `specs/` stays a gap-free numbered sequence — it is a historical record, not work to pick up.
> Full source if it is ever reconstructed:
> `git show 974f832246cd4d42ca002f5bc8e21c40ad2148a6:.claude/workflows/correctness-review.js`.
> Decision record: [`docs/decisions.md`](../docs/decisions.md).

This is a **runbook**, not a feature spec. It exists so a human (or the maintainer's own
interactive session) can finish the acceptance bar on **lode-wtwb** that no dispatched
`coding`/`code-reviewer` subagent can do — neither reaches the `Workflow` tool (verified
empirically, lode-905v; the same constraint `specs/11` and `specs/12` document for their own
tickets) — and then record the result on the ticket. Delete it once the result is recorded.

## What this checks

The full incident account is in [`docs/decisions.md`](../docs/decisions.md), under "Update
(lode-wtwb, 2026-07-24)" — that is the durable record; it is not repeated here. The facts this
runbook needs: the crashed run is `wf_9b60ff50-0c6`, it reviewed `b760b3d` (lode-ns3r), and the
bug it silently discarded was a High-severity SIGPIPE finding in `scripts/release-bump.sh`.

lode-wtwb's fix changes a verifier-produced-no-verdict outcome from "folded into `refuted`" to "a
third state, returned in its own `unverified` array, with `degraded: true` at the top level." This
runbook replays the exact failed run (via `resumeFromRunId`) against the FIXED script and confirms
the SIGPIPE finding now survives to the reviewer as `unverified` — never silently vanishes.

## Prerequisites

1. **A Workflow-capable session.** Run this from your normal interactive Claude Code session (the
   main session), NOT a dispatched subagent — only the main session has the `Workflow` tool
   (verified empirically, lode-905v). Sanity check before starting: ask the session to confirm
   `Workflow` resolves (`ToolSearch` with `select:Workflow`). If it does NOT resolve even here,
   STOP and record that on lode-wtwb — that is itself a finding.
2. The fixed script lives on `land/lode-wtwb`, not yet on `trunk` — Step 0 extracts it to a stable
   temp path rather than checking a branch out, so you can stay on whichever branch you're actually
   reviewing for other work.

## Step 0 — set up

```bash
mkdir -p specs/wtwb-verify-crash-results

# Extract the fixed script from lode-wtwb's pushed branch to a stable temp path.
git fetch -q origin land/lode-wtwb
git show origin/land/lode-wtwb:.claude/workflows/correctness-review.js \
  > specs/wtwb-verify-crash-results/correctness-review.js
```

## Step 1 — replay the crashed run against the fixed script

Resume the exact run that crashed (`wf_9b60ff50-0c6`, the lode-ns3r review over
`trunk...b760b3d`) using the fixed script:

```
Workflow({ scriptPath: "specs/wtwb-verify-crash-results/correctness-review.js",
           resumeFromRunId: "wf_9b60ff50-0c6" })
```

If `resumeFromRunId` isn't accepted this way, or the original run's crashed state can no longer be
resumed (e.g. it's aged out), fall back to a fresh run over the same range instead:

```
Workflow({ scriptPath: "specs/wtwb-verify-crash-results/correctness-review.js",
           args: { refRange: "b760b3d~1...b760b3d" } })
```

Note the range is pinned to the reviewed commit and its own parent, **not** `trunk...b760b3d` as
the original run used. `b760b3d` has since been merged into `trunk`, so it is now an ancestor of
it and the three-dot merge base of `trunk...b760b3d` *is* `b760b3d` — that range diffs to nothing,
which would hand every FIND agent an empty diff and drive Step 2's "appears in neither array"
branch to report a false regression while reviewing no code at all. `b760b3d~1...b760b3d` cannot
go stale that way regardless of what `trunk` absorbs later.

A fresh run won't reproduce the original session-limit crash (that was an infrastructure
condition, not a property of the diff), so it is a **much** weaker check — weak enough that it
cannot by itself satisfy criterion 3. If no verifier happens to crash, the changed `if (!v)`
branch never executes, and the SIGPIPE finding lands in `findings` exactly as the *unfixed* script
would have put it there. Treat the fallback as evidence the fixed script still runs end to end and
returns the new fields, nothing more. Note explicitly on lode-wtwb which path you took.

Save the returned result JSON to `specs/wtwb-verify-crash-results/result.json`.

## Step 2 — confirm the SIGPIPE finding survives, and how

Check the saved result:

- Does the SIGPIPE / `set -o pipefail` + `grep -q` finding (`scripts/release-bump.sh`) appear
  **anywhere** in the result — either `findings` (a survivor, if its verifier happened to complete
  this time) or `unverified` (if the verifier crashed again)? Either outcome is acceptable —
  the acceptance bar is that it is never silently absent from both.
- If it appears in `unverified`: confirm its `unverifiedReason` is set (not a `refutationReason`),
  and that `result.degraded` is `true`.
- If it appears in `findings`: confirm it was not spuriously downgraded — check `severity` and
  `severityNote`.
- If it appears in **neither** array: that is a regression — the fix did not close the gap. Do not
  declare the ticket's acceptance criteria met; reopen the investigation instead of forcing a pass.
- Separately, sanity-check `result.stats.falsePositiveRate` didn't spike — the fix should not have
  made confirmed refutations less trustworthy, only reclassified the crashed ones.

## Step 3 — record the result on lode-wtwb

Append to lode-wtwb: which replay path was used (resume vs. fresh), whether the SIGPIPE finding
appears in `findings` or `unverified`, the `degraded` flag's value, and `stats.falsePositiveRate`.

Then judge criterion 3 by the path actually taken — the two are **not** interchangeable:

- **Resume path (`resumeFromRunId`), finding present in either array** → criterion 3 is met. This
  is the only path that reproduces the no-verdict state the fix changes.
- **Fallback path, and at least one verifier produced no verdict** (`unverified` non-empty,
  `degraded: true`) → criterion 3 is met; the changed branch demonstrably executed.
- **Fallback path with no verifier crash** (`unverified` empty, `degraded: false`) → criterion 3 is
  **NOT** met, even though the SIGPIPE finding is sitting in `findings`. The unfixed script produces
  that same result, so it discriminates nothing. Record it as inconclusive and leave the criterion
  open rather than reading a green-looking result as a pass.
- **Absent from both arrays** → say so plainly and do not close the ticket on partial evidence.

## After the result is recorded

Delete `specs/wtwb-verify-crash-results/` and this spec
(`specs/13-correctness-review-verify-crash-regression.md`) once the result is on lode-wtwb — they
are transient working artifacts; the durable record is the ticket itself.
