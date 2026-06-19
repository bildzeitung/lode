---
name: code
description: Build one or more lode tasks as PRODUCERS by dispatching the `coding` subagent — each claims a bd issue, builds in an isolated worktree, passes the quality gates, runs a baked-in technical review, pushes its branch to origin, and marks the ticket ready-for-land. Producers never merge/close/push trunk; a separate `/land` lander does. `/code <id>` is one producer; `/code <id> <id> …` / `/code --all-ready` fans out N parallel producers. Use for any task that changes the lode repo (code, docs, configs). Examples — "/code lode-123", "/code lode-1 lode-2 lode-3", "/code --all-ready", "/code add a --json flag to the search CLI", "/code" (top item from `bd ready`).
---

# code

`/code` is lode's **sole producer** entry point. It dispatches the project's **`coding`** subagent
to carry a task through the producer cycle: **claim → worktree → working code → green gates →
baked-in technical review → branch pushed to `origin/land/<id>` → ticket marked `ready-for-land` →
stop.** A producer **never** merges to `trunk`, closes the ticket, pushes `trunk`, or writes the
main checkout — a single `/land` lander owns every write to `trunk` and lands reviewed branches.

There is **no separate `/code-parallel`.** Once landing left the producer, building one task and
building five became the same act — a producer just leaves a green branch on origin, and a *new*
branch ref doesn't race `trunk`, so N producers run safely in parallel. `/code` covers both:

- **`/code <id>`** — **one** producer.
- **`/code <id> <id> …`** or **`/code --all-ready`** — **N producers in parallel**, each in its own
  isolated worktree.

The subagent already owns *how producer work flows* in lode (it honors `CLAUDE.md` / `AGENTS.md`,
`docs/agents-workflow.md`, and the phase-a skeleton order) — this skill's only job is to launch it
correctly, one producer per task, and relay what came back.

## What to do when invoked

1. **Resolve the task set** from the argument:
   - **One bd issue ID** (e.g. `lode-ai1`) → one producer; tell the agent to claim and implement it.
   - **Several bd issue IDs** → **fan-out**: one producer per ID. Only dispatch IDs that are
     genuinely **independent** (no unmet dependency between them); if two share a dependency, say so
     and let the human sequence them rather than racing.
   - **`--all-ready`** → read `bd ready` and fan out across the **independent, unblocked** frontier
     (honoring the dependency graph and phase-a ordering). Don't dispatch a ticket whose blocker is
     also in the batch — surface that instead of guessing the order.
   - **Free-text** (e.g. "add a --json flag to search") → one producer; tell the agent that is the
     task — it files the bd issue itself before coding, per its own rules.
   - **No argument** → one producer; tell the agent to pick the **top unblocked item from `bd
     ready`**. Do **not** pick the issue yourself — the subagent does that.

2. **Dispatch one `coding` producer per task** via the Agent tool with `subagent_type: "coding"`
   **and `isolation: "worktree"`**. The isolation is required: a subagent is pinned at the repo root
   and **cannot** call `EnterWorktree` for itself (both the `name` and `path` forms are refused), so
   the harness must hand each producer a worktree at dispatch — `isolation: "worktree"` launches it
   already cwd'd inside `.claude/worktrees/agent-<hash>` on its own branch off `trunk` HEAD.

   - **Solo** (`/code <id>`, free-text, no-arg): dispatch **exactly one** producer in the foreground.
   - **Fan-out** (`/code <id> <id> …`, `--all-ready`): dispatch **one producer per ticket,
     concurrently** (`run_in_background: true`), and collect each result as it finishes. Each builds,
     reviews, pushes `origin/land/<its-id>`, and marks its own ticket `ready-for-land` independently;
     one producer's escalation must **not** block its siblings.

   Pass the resolved task in each prompt, e.g.:

   > Implement lode-ai1 as a producer following your cycle (claim → worktree → gates → technical
   > review → push `origin/land/<id>` → mark `ready-for-land` → stop). Do **not** merge, close, or
   > push trunk. Stop and escalate (revert to green, annotate, don't mark ready) if a clarifying
   > decision is needed or a refinement makes it worse; stop and report if a gate fails.

   (For the no-arg case: *"Pick the top ready item from `bd ready` and produce it…"*)

3. **Relay each result to the user.** The agent's final message is not shown to the user — surface
   what matters per producer: which ticket, that gates + technical review passed, the
   **`land/<id>`** branch and head SHA it pushed, and that it marked **`ready-for-land`** (so `/land`
   can pick it up). If a producer **escalated** (needs a clarifying decision, or judged it was making
   things worse), pass that through plainly — it reverted to green, pushed the branch, applied
   `land-escalated`, and did **not** mark ready; the human owes a decision before it can land. For a
   fan-out, give a per-ticket roll-up: which are ready-for-land, which escalated and why.

## Notes

- This skill is the **only** sanctioned way to spin up coding work from the main session, which is
  otherwise told not to spawn agents unprompted — invoking `/code` *is* the user asking. Fan-out is
  the *only* sanctioned way to run several producers at once (there is no `/code-parallel`).
- Producers do all repo mutation inside their harness-provided (`isolation: "worktree"`) worktrees
  and push to `origin/land/<id>`; **nothing merges in the build session.** The main session stays on
  `trunk` and never edits files here. Landing those branches is `/land`'s job, not this skill's.
- If an argument is genuinely ambiguous (looks like it might be an ID but isn't one that exists, or a
  fan-out set with hidden dependencies), ask the user before dispatching rather than guessing.
