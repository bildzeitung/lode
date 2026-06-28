---
name: code
description: Build one or more lode tasks as PRODUCERS in two phases — dispatch the `coding` subagent (Sonnet) to claim a bd issue, build in an isolated worktree, pass the quality gates, push its branch to origin, and hand off at ready-for-code-review; then dispatch the `code-reviewer` subagent (Opus) to enter that worktree, run the technical review (/code-review + /simplify), re-gate, and swap the ticket to ready-for-land. Producers never merge/close/push trunk; a separate `/land` lander does. `/code <id>` (or `/code --single`) is one producer; bare `/code` / `/code --all-ready` / `/code <id> <id> …` fans out N parallel producers across the ready frontier. Use for any task that changes the lode repo (code, docs, configs). Examples — "/code" (fan out across `bd ready`), "/code lode-1 lode-2 lode-3", "/code lode-123", "/code --single" (top one item from `bd ready`), "/code add a --json flag to the search CLI".
---

# code

`/code` is lode's **sole producer** entry point, and it runs each task in **two dispatched phases**:

1. **Build — `coding` subagent (Sonnet):** **claim → worktree → working code → green gates → branch
   pushed to `origin/land/<id>` → ticket marked `ready-for-code-review` → keep the worktree → stop.**
   The builder does **not** review its own work.
2. **Technical review — `code-reviewer` subagent (Opus):** enters the builder's worktree by path, runs
   **`/code-review --fix` + `/simplify`**, re-gates, re-pushes `land/<id>`, and swaps the ticket to
   **`ready-for-land`** (or escalates).

Splitting build (cheap) from review (Opus) means the technical review is done by an agent that didn't
write the code — and the lander's later semantic review is too, so *neither* review of a branch is its
author's. A producer **never** merges to `trunk`, closes the ticket, pushes `trunk`, or writes the
main checkout — a single `/land` lander owns every write to `trunk` and lands reviewed branches.

There is **no separate `/code-parallel`.** Once landing left the producer, building one task and
building five became the same act — a producer just leaves a green branch on origin, and a *new*
branch ref doesn't race `trunk`, so N producers run safely in parallel. `/code` covers both:

- **bare `/code`** (no argument), **`/code --all-ready`**, or **`/code <id> <id> …`** — **N producers
  in parallel**, each in its own isolated worktree. Bare `/code` is the **default**: it fans out
  across the whole independent, unblocked ready frontier.
- **`/code <id>`** or **`/code --single`** — **one** producer.

Both subagents already own *how producer work flows* in lode (they honor `CLAUDE.md` / `AGENTS.md`,
`docs/agents-workflow.md`, and the phase-a skeleton order) — this skill's only job is to launch them
correctly **in order, build then review**, one task at a time, and relay what came back.

## What to do when invoked

1. **Resolve the task set** from the argument:
   - **No argument** (the **default**), or **`--all-ready`** → read `bd ready` and **fan out** across
     the **independent, unblocked** frontier (honoring the dependency graph and phase-a ordering).
     Don't dispatch a ticket whose blocker is also in the batch — surface that instead of guessing the
     order.
   - **`--single`** (no ID) → one producer; tell the agent to pick the **top unblocked item from `bd
     ready`**. Do **not** pick the issue yourself — the subagent does that. (This is the former
     bare-`/code` behavior, now opt-in.)
   - **One bd issue ID** (e.g. `lode-ai1`) → one producer; tell the agent to claim and implement it.
   - **Several bd issue IDs** → **fan-out**: one producer per ID. Only dispatch IDs that are
     genuinely **independent** (no unmet dependency between them); if two share a dependency, say so
     and let the human sequence them rather than racing.
   - **Free-text** (e.g. "add a --json flag to search") → one producer; tell the agent that is the
     task — it files the bd issue itself before coding, per its own rules.

2. **Phase 1 — dispatch one `coding` builder per task** via the Agent tool with
   `subagent_type: "coding"` **and `isolation: "worktree"`**. The isolation is required: a subagent is
   pinned at the repo root and **cannot** call `EnterWorktree` to *create* its own, so the harness must
   hand each builder a worktree at dispatch — `isolation: "worktree"` launches it already cwd'd inside
   `.claude/worktrees/agent-<hash>` on its own branch off `trunk` HEAD.

   - **Solo** (`/code <id>`, `/code --single`, free-text): dispatch **exactly one** builder in the
     foreground.
   - **Fan-out** (bare `/code`, `--all-ready`, `/code <id> <id> …`): dispatch **one builder per ticket,
     concurrently** (`run_in_background: true`), and collect each result as it finishes. Each builds,
     pushes `origin/land/<its-id>`, **keeps its worktree**, and marks its own ticket
     `ready-for-code-review` independently; one builder's escalation must **not** block its siblings.

   Pass the resolved task in each prompt, e.g.:

   > Implement lode-ai1 as a producer following your cycle (claim → worktree → gates → push
   > `origin/land/<id>` → mark `ready-for-code-review`, recording `review_worktree`/`review_head` →
   > keep the worktree → stop). Do **not** review your own work, merge, close, or push trunk. Stop and
   > escalate (revert to green, annotate `land-escalated`, don't hand off) if a clarifying decision is
   > needed during the build; stop and report if a gate fails.

   (For the `--single` case: *"Pick the top ready item from `bd ready` and produce it…"*)

3. **Phase 2 — dispatch a `code-reviewer` per built ticket** (only those that came back
   **`ready-for-code-review`**; skip any that escalated). Use the Agent tool with
   `subagent_type: "code-reviewer"` **and `isolation: "worktree"`** — the isolation gives it a cwd off
   the repo root so it can legally `EnterWorktree` (`path` form) into the builder's worktree. Match the
   build cadence: one reviewer in the foreground for a solo build; one reviewer per ticket
   concurrently (`run_in_background: true`) for a fan-out, each dispatched as its builder returns.

   Pass the ticket id, e.g.:

   > Technically review lode-ai1 (it is `ready-for-code-review`): read `review_worktree`/`review_head`
   > from bd, `EnterWorktree` into that worktree, run `/code-review --fix` + `/simplify`, re-gate,
   > re-push `land/<id>`, and swap the ticket to `ready-for-land`. Do **not** merge, close, or push
   > trunk. Escalate (revert to green, swap to `land-escalated`, don't mark ready) only on a clarifying
   > decision or "making it worse."

4. **Relay each result to the user.** Agent final messages aren't shown to the user — surface what
   matters per ticket across **both** phases: that the build gates passed and the technical review +
   re-gate passed, the **`land/<id>`** branch and head SHA, and that it reached **`ready-for-land`**
   (so `/land` can pick it up). If a **builder** escalated, say so — it reverted to green, pushed,
   applied `land-escalated`, did **not** hand off, and a human owes a build decision. If a **reviewer**
   escalated, likewise — green branch pushed, `land-escalated` set, not landable until the human
   decides. For a fan-out, give a per-ticket roll-up: which reached ready-for-land, which are still in
   review, which escalated (at build or review) and why.

## Notes

- This skill is the **only** sanctioned way to spin up coding work from the main session, which is
  otherwise told not to spawn agents unprompted — invoking `/code` *is* the user asking. Fan-out is
  the *only* sanctioned way to run several producers at once (there is no `/code-parallel`).
- **Two phases, in order: build then review.** Phase 1 (`coding`, Sonnet) builds and **keeps its
  worktree**; phase 2 (`code-reviewer`, Opus) enters that same worktree by path and runs the technical
  review. The review must run on the *built* branch, so always dispatch the reviewer *after* its
  builder returns `ready-for-code-review` — never in parallel with its own build. Don't dispatch a
  reviewer for a ticket that escalated at build time.
- Producers do all repo mutation inside `isolation: "worktree"` worktrees and push to
  `origin/land/<id>`; **nothing merges and nothing the author wrote is reviewed by its author.** The
  main session stays on `trunk` and never edits files here. Landing those branches is `/land`'s job,
  not this skill's.
- If an argument is genuinely ambiguous (looks like it might be an ID but isn't one that exists, or a
  fan-out set with hidden dependencies), ask the user before dispatching rather than guessing.
