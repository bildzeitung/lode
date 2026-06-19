---
name: code
description: Implement one lode task end-to-end by dispatching the `coding` subagent — it claims a bd issue, builds in an isolated worktree, passes the quality gates, merges --no-ff into trunk, closes the issue, and pushes. Use for any task that changes the lode repo (code, docs, configs). Examples — "/code lode-123", "/code add a --json flag to the search CLI", "/code" (picks the top item from `bd ready`).
---

# code

This skill dispatches the project's **`coding`** subagent to carry one lode task through
its full orderly cycle: **claim → worktree → working code → green gates → `--no-ff`
merge → closed issue → pushed**. The subagent already owns *how work flows* in lode
(it honors `CLAUDE.md` / `AGENTS.md` and the phase-a skeleton order) — this skill's only
job is to launch it correctly and relay what came back.

## What to do when invoked

1. **Resolve the task** from the skill argument:
   - **A bd issue ID** (e.g. `lode-ai1`) → tell the agent to claim and implement that issue.
   - **Free-text** (e.g. "add a --json flag to search") → tell the agent that is the task;
     it files the bd issue itself before coding, per its own rules.
   - **No argument** → tell the agent to pick the **top unblocked item from `bd ready`**
     (honoring the dependency frontier / phase-a ordering). Do **not** pick the issue
     yourself — the subagent does that; its worktree comes from the harness (see step 2).

2. **Dispatch exactly one `coding` agent in the foreground** via the Agent tool with
   `subagent_type: "coding"` **and `isolation: "worktree"`**. The isolation is required: a
   subagent is pinned at the repo root and **cannot** call `EnterWorktree` for itself (both the
   `name` and `path` forms are refused), so the harness must hand it a worktree at dispatch —
   `isolation: "worktree"` launches it already cwd'd inside `.claude/worktrees/agent-<hash>` on its
   own branch off `trunk` HEAD. Do not set `run_in_background`. Do not spawn more than one; lode's
   convention is one task per session. Pass the resolved task in the prompt, e.g.:

   > Implement lode-ai1 end-to-end following your orderly cycle (claim → worktree → gates
   > → `--no-ff` merge → close → push). Stop and report if a quality gate fails or the
   > issue is ambiguous.

   (For the no-arg case: *"Pick the top ready item from `bd ready` and implement it
   end-to-end…"*)

3. **Relay the result to the user.** The agent's final message is not shown to the user —
   surface what matters: which issue was done, that gates passed, that it merged `--no-ff`
   and pushed (or exactly where it stopped and why). If the agent reports a failed gate,
   an ambiguous issue, or an empty `bd ready`, pass that through plainly rather than
   papering over it.

## Notes

- This skill is the **only** sanctioned way to spin up coding work from the main session,
  which is otherwise told not to spawn agents unprompted — invoking `/code` *is* the user
  asking.
- The subagent does all repo mutation inside its harness-provided (`isolation: "worktree"`)
  worktree, merging back into `trunk` with `git -C <main-checkout>`; the main session stays on
  `trunk` and never edits files directly here.
- If the argument is genuinely ambiguous (looks like it might be an ID but isn't one that
  exists), ask the user before dispatching rather than guessing.
