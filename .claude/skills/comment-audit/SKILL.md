---
name: comment-audit
description: Sequence a comment quality pass on lode source — comment-auditor (report-only findings) → human veto seam → comment-groomer (applies surviving findings, comment-lines-only). Thin orchestrator; the taxonomy and rubric live solely in the agent files. Examples — "/comment-audit" (current branch's diff), "/comment-audit land/lode-abc", "/comment-audit lode/tui/", "/comment-audit docs/".
---

# comment-audit

I am a thin dispatch skill, modeled on `/code`'s pattern: I **sequence** two existing agents — I
never audit or groom a comment myself, and the two agents never call each other directly.

1. **comment-auditor** (Opus, report-only) — produces a findings block.
2. **A veto seam** — a human strikes false positives / adjusts actions, or (unattended) a fixed
   threshold does.
3. **comment-groomer** (Sonnet, worktree) — applies only the findings that survived the seam.

The taxonomy, smell list, and rubric are **not restated here** — they live solely in
[`comment-auditor.md`](../../agents/comment-auditor.md) and
[`comment-groomer.md`](../../agents/comment-groomer.md), which both already carry the
`docs/conventions.md` Comments fiat via `CLAUDE.md`'s `@import`. I would only fork the record by
copying any of it into this file.

## 1. Parse the target

- **`/comment-audit <branch>`** — a branch target (e.g. `land/lode-abc`, or any local/remote ref).
  Audit is **diff-scoped**: `trunk...<branch>` hunks, plus comments adjacent to the changed code,
  per the auditor's own drift rule. This is the common case — a branch already in flight through
  the normal producer/reviewer cycle.
- **`/comment-audit <path>`** — a path target (a file or a directory tree). Audit is a **whole-tree
  sweep** over everything under that path, not diff-scoped.
- **Bare `/comment-audit`** — defaults to the **current branch's diff** against `trunk`, same
  scoping as the branch-target case above.

If the argument is ambiguous between a ref and a path (rare — a path that happens to also be a
valid ref name), prefer treating it as a path only when no branch/ref by that name resolves;
otherwise ask.

## 2. Dispatch comment-auditor, surface its findings verbatim

Dispatch `subagent_type: "comment-auditor"` at the resolved target (branch diff or path). No
call-site `isolation` option — its own frontmatter (`isolation: worktree`) is sufficient, same
convention as every other agent dispatch in this repo.

When it returns, **relay its full findings block to the user verbatim** — file:line anchors,
taxonomy classification, and suggested action per finding, unedited. I add no commentary of my
own on individual findings; I am not qualified to second-guess the rubric, only to sequence it.

If the auditor reports zero findings, say so plainly and stop — there is nothing for the groomer
to do.

## 3. The veto seam — non-negotiable

I **never** hand the auditor's full findings list straight to the groomer. Every invocation stops
here for a disposition pass before grooming proceeds:

- **Interactive (a human is available):** present the findings block and let the human strike
  false positives and adjust suggested actions, finding by finding. What remains after the human's
  edits is the surviving set.
- **Autonomous (no user available to respond — e.g. a `/loop` invocation):** apply a fixed
  threshold — only findings at **high severity AND confidence ≥ 0.9** survive automatically. Every
  finding below that bar is **not applied**; it is reported as pending a human, the same shape as a
  build-time escalation elsewhere in this repo (surfaced, never silently dropped or silently
  applied).

This threshold is a starting point, tunable by whoever operates the autonomous path — but the seam
itself, the fact that *something* always stands between the auditor's raw output and the groomer's
input, is not. A run that skips this step has skipped the whole point of splitting audit from
apply.

If the surviving set is empty (everything vetoed, or nothing cleared the autonomous threshold),
say so and stop — do not dispatch the groomer over nothing.

## 4. Dispatch comment-groomer with the surviving findings only

Dispatch `subagent_type: "comment-groomer"`, passing exactly the surviving findings block (not the
auditor's original output) and the same target it was scoped against. Its own non-negotiables —
comment-lines-only diff, the fiat's untouchable exemptions, re-verifying each finding itself
before applying, re-gating with `nox -t fix` + the `tests` bucket's `unit` view, committing as its
own commit — are unchanged by being dispatched through me; I add nothing to its contract.

**Where the groomer's output goes depends on the target type — decided and documented here so it
never has to guess:**

- **Branch target (including the bare/current-branch case):** the branch is already an in-flight
  `land/<id>` ticket somewhere in the normal producer → reviewer → lander pipeline. The groomer
  works in a worktree checked out on **that same branch**, commits its comment-only change as an
  additional commit, and pushes straight back to `origin/land/<id>` (an ordinary, non-force push —
  it only appends). It does **not** touch the ticket's label; that branch's existing
  `ready-for-code-review`/`ready-for-land` state is owned by whichever stage is already driving it,
  not by this sweep.
- **Path target (a whole-tree sweep with no branch already in flight):** there is no existing
  ticket to fold into — this is new, standalone work. I file a bd issue for the sweep first
  (`bd create --type=task --title="comment-audit sweep: <path>" --description="…" `), then dispatch
  the groomer the same way a `coding` producer would build any other task: its own worktree, its
  own `land/<id>` branch, ending at **`ready-for-code-review`** for the normal
  `code-reviewer` → `/land` pipeline to pick up. I never mark it `ready-for-land` and never touch
  `trunk` myself.

Either way, **trunk writes stay `/land`'s alone** — nothing in this skill merges, closes a ticket,
or pushes `trunk`.

## 5. Report

Close every run with:

- What was **applied** (the groomer's own commit, and which findings it covered).
- What was **skipped or reverted** by the groomer itself (a finding it couldn't re-confirm against
  the code, or a hunk that touched executable bytes).
- What is still **pending a human** — every finding vetoed in an interactive run, or that missed
  the autonomous threshold — so nothing the auditor found silently vanishes from view.

## Constraints

- **Thin wrapper only.** I carry no audit logic, no taxonomy, no rubric of my own — see the top of
  this file. If a rule needs stating twice, it belongs in the agent files, not here.
- **Concurrency.** If a `/comment-audit` run might overlap with an active `/code` fan-out, it
  shares the same concurrency budget those producers draw from — see the memory note on avoiding
  concurrent `/code` pressure (`lode-pzr`/`lode-2cf`). Don't stack a sweep on top of a running
  `/code` invocation without accounting for it.
- **No self-review.** comment-auditor and comment-groomer never call each other, and I never
  apply a finding myself outside the groomer's own mechanical verification.
