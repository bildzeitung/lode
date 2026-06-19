---
name: code-parallel
description: Run several lode coding tasks at once, reliably — fan out N `coding` subagents that build in isolated worktrees in parallel, then land their branches into trunk one at a time from the orchestrator (merge --no-ff → close → single push). Use when you have multiple independent ready tickets to clear in one batch. Examples — "/code-parallel lode-123 lode-456 lode-789", "/code-parallel 3" (top 3 independent items from `bd ready`), "/code-parallel" (all independent ready items).
---

# code-parallel

The `/code` skill runs **one** `coding` subagent for **one** ticket. This skill runs **several at
once** without them stepping on each other.

## Why a separate skill — the reliability model

A lone `coding` agent does its own **land** step: it merges `--no-ff` into the shared `trunk`
checkout, commits the `.beads` export there, and pushes. Run several of those fully-autonomous
agents in parallel and they **race** on the one thing they all share — the main checkout's git
index, the `.beads/*.jsonl` export, the Dolt store, and `git push`. That race is exactly what makes
naive parallelism flaky.

So this skill splits the cycle:

- **Build in parallel.** Each agent gets its **own** isolated worktree (`isolation: "worktree"`) and
  builds + passes gates + commits **to its own branch** — fully concurrent, zero shared state.
- **Land in series.** Every write to shared state — claiming, merging into `trunk`, closing,
  pushing, `bd dolt push` — is done by the **orchestrator (this session), one at a time.** The
  agents never touch the main checkout, `.beads`, or `trunk`.

Concentrating all the racy operations in the single orchestrator is what makes the batch reliable.

## When to use / when not

- **Use** when you have **multiple independent** ready tickets — no dependency edges between them,
  and ideally disjoint file footprints — and want them cleared in one batch.
- **Don't** parallelize **dependent** work. During phase-a (the walking skeleton, before
  `lode-6w1.1` closes) most ready work is sequential; `bd ready` deliberately withholds deepening
  tasks until the skeleton lands. If only one item is genuinely independent, just use `/code`.

## What to do when invoked

### 1. Resolve the ticket set

From the argument:

- **Explicit IDs** (`lode-123 lode-456 …`) → that is the set.
- **A bare number `N`** → the **top N independent** items from `bd ready`.
- **No argument** → **all independent** items currently in `bd ready` (cap at a sane fan-out, ~4–5;
  if there are more, take the frontier and say so).

```bash
rtk bd ready --json        # the actionable frontier; parse JSON, don't scrape
```

### 2. Independence check (do this before dispatching)

The set must be **safe to build in parallel and merge in any order**:

- **No dependency edges among them.** `rtk bd show <id>` for each (or read the `bd ready --json`
  deps) — drop any ticket that depends on another in the set; it isn't parallel-safe.
- **Prefer disjoint file footprints.** You can't always know these up front; the serialized
  merge in step 5 is the backstop — a true overlap surfaces as a merge conflict and is reported,
  not silently mis-merged.

If the requested set isn't fully independent, **tell the user which ones you dropped and why**, and
proceed with the safe subset (or fall back to `/code` for a single one).

### 3. Claim the whole set **upfront, in the orchestrator**

Claim every ticket here, serially, **before** dispatching any agent — so all bd/Dolt writes stay in
one session and the parallel phase does **zero** bd writes (no Dolt write contention):

```bash
rtk bd update <id> --claim     # repeat for each ticket in the set
```

This dirties `.beads/*.jsonl` in the **main checkout**. Leave it uncommitted for now; it's committed
once in step 5.

### 4. Dispatch the agents **in parallel** — build only, no land

Issue one `coding` Agent call **per ticket, all in the same response block** (so they run
concurrently), each with `subagent_type: "coding"` **and `isolation: "worktree"`** (required — a
subagent is pinned at the repo root and cannot make its own worktree). Do **not** set
`run_in_background` unless you specifically want to interleave other work; a single parallel block is
simplest.

Scope each agent to **steps 3–7 of its cycle only** — its issue is already claimed, and it must
**not** land. Prompt template (one per ticket):

> **PARALLEL BATCH RUN — you are one of several `coding` agents running at once.** Issue `<id>` is
> **already claimed**; do **not** claim or re-claim it. Implement it **in your worktree only**: read
> the issue (description + acceptance + `--design`), build the simplest thing that works, run the
> quality gates (`nox -t fix`, `nox -s tests`; `scripts/validate-mermaid.sh` if a docs diagram
> changed), and **commit your work to your worktree branch.** Then **STOP.** Do **not** close the
> issue, **not** merge into `trunk`, **not** touch the main checkout or `.beads`, **not** push — the
> orchestrator lands every branch serially to avoid racing on `trunk`. **Report exactly:** issue id,
> worktree path (`pwd`), branch name (`git rev-parse --abbrev-ref HEAD`), final commit SHA
> (`git rev-parse HEAD`), gate results (fix / tests → pass|fail), and a one-line summary. If a gate
> fails or the issue is ambiguous, **stop and report that** instead — do not merge.

Because each agent **commits** to its branch, that branch ref survives the agent's exit even if the
worktree directory is auto-cleaned — so the orchestrator can still merge it in step 5.

### 5. Land the branches **serially**, from the orchestrator

Collect every agent's report. The **landed set** is the agents that reported **green gates and a
real commit SHA**. Drop the rest (see step 6). Let `<main>` be the main checkout (the
`git worktree list` entry **not** under `.claude/worktrees/`).

**a. Commit the upfront-claim export first** so the tree is clean before any merge:

```bash
rtk git -C <main> add .beads/issues.jsonl .beads/interactions.jsonl
rtk git -C <main> commit -m "bd: export batch claims — passive jsonl

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

**b. Merge each landed branch, one at a time.** Merges don't touch `.beads`, so the tree stays clean
through the whole loop:

```bash
rtk git -C <main> merge --no-ff <branch> -m "Merge <branch> (<id>) — <one-line summary>

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

If a merge **conflicts** (true file overlap that the independence check missed):

```bash
rtk git -C <main> merge --abort
```

…**drop that ticket from the landed set**, leave its issue `in_progress`, and record it for the
report as "needs manual rebase" — then continue with the rest. One bad merge never blocks the others.

**c. Close all merged issues at once, then commit that export once:**

```bash
rtk bd close <id1> <id2> ... --suggest-next      # only the branches that actually merged
rtk git -C <main> add .beads/issues.jsonl .beads/interactions.jsonl
rtk git -C <main> commit -m "bd: export batch closes — passive jsonl

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### 6. Single sync — push once, then beads

Work isn't done until it's pushed. One push for the whole batch:

```bash
rtk git -C <main> pull --rebase
rtk git -C <main> push
rtk git -C <main> status        # MUST read "up to date with origin"
rtk bd dolt push                # beads: authoritative sync over refs/dolt/data
```

### 7. Clean up the worktrees and branches

For each **merged** branch, remove its (now redundant) worktree and delete the branch:

```bash
rtk git -C <main> worktree remove --force <worktree-path>   # if it still exists
rtk git -C <main> branch -d <branch>
```

Leave the worktree/branch **intact** for any ticket that failed a gate or hit a merge conflict — its
work is still on the branch for follow-up.

### 8. Relay a per-ticket summary

The agents' final messages aren't shown to the user. Give one line per ticket:

- **Landed** — issue id, merged `--no-ff`, gates green.
- **Held back** — issue id + why (gate failure / ambiguous issue / merge conflict), and that its
  branch was kept for follow-up.

Then state the batch outcome: pushed to origin (or exactly where it stopped), and `bd dolt push`
done.

## Notes

- This skill, like `/code`, is a **sanctioned** way to spawn coding agents from the main session
  (which is otherwise told not to spawn unprompted) — invoking `/code-parallel` **is** the user
  asking. It deliberately overrides `/code`'s "one task per session" convention for the explicit
  batch case.
- It also **overrides the `coding` agent's own land step**: the dispatch prompt tells each agent to
  stop after committing to its branch. That override is intentional and is what keeps the shared
  `trunk` checkout race-free — the orchestrator owns every shared-state write.
- If a requested set is ambiguous (an arg that looks like an ID but resolves to no issue, or a set
  that's actually all interdependent), **ask the user** before dispatching rather than guessing.
- Keep fan-out modest (~4–5). The bottleneck is the serial land + single push, so beyond a handful
  the marginal benefit drops while the chance of an overlap conflict rises.
