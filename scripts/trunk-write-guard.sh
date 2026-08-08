#!/usr/bin/env bash
#
# PreToolUse(Edit|Write) guard body (lode-p8zl): ask for confirmation before an Edit or Write
# tool call whose cwd resolves to a checkout on `trunk`.
#
# This is the structurally-correct-altitude follow-up lode-6wgc deferred (see
# docs/agents-workflow.md, "Isolation guard: mid-session re-assertion (lode-6wgc)"): that ticket's
# mitigation is markdown checkpoints in .claude/agents/coding.md and .claude/agents/code-reviewer.md
# that re-run scripts/isolation-guard.sh at two points in each cycle -- both of which depend on the
# agent choosing to run them, and neither of which can cover the window between a checkpoint and the
# tool call it guards. A PreToolUse hook fires on every matching tool call with no agent cooperation.
#
# CONTRACT (the three maintainer rulings this implements are recorded once, in docs/decisions.md
# under "2026-08-08 ... lode-p8zl" -- not restated here, so an amended ruling has one home, not two):
#   - Gate on the BRANCH, never on an attempt to tell a dispatched subagent from the main session
#     (the PreToolUse payload gives no reliable way to, ruling 1).
#   - Decide "ask", never "deny" (ruling 1) -- a human at the terminal can approve and continue; a
#     dispatched subagent cannot approve anything and is stopped. Human presence is the
#     discriminator, without the payload ever encoding it.
#   - Use NO jq (ruling 3) -- this guard parses no tool_input at all, so unlike the three
#     PreToolUse(Bash) guards it adds nothing to the lode-oii9 jq-missing surface.
#
# Full rulings and rationale: docs/decisions.md (search "lode-p8zl"),
# docs/agents-workflow.md#isolation-guard-mid-session-re-assertion-lode-6wgc.
#
# Usage: scripts/trunk-write-guard.sh
#   (no arguments -- this guard reads no tool_input at all)
#
# Prints nothing (allow -- fall through to normal permission handling) or one PreToolUse
# hookSpecificOutput JSON object with permissionDecision "ask". ALWAYS exits 0: a PreToolUse hook
# exiting non-zero is itself a defect.

set -euo pipefail

# One git call, on the hot Edit/Write path: --abbrev-ref HEAD already fails outside a repo (and on
# an unborn HEAD), so a separate --show-toplevel probe buys nothing. Measured cost of the whole
# guard, bash spawn included: ~10ms on the WSL2 dev machine, down from ~13ms with the second git
# call (measured over 20 runs at technical review; lode-p8zl acceptance, "open risk to check").
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"

if [ "$branch" = "trunk" ]; then
  printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "lode-p8zl: this Edit/Write targets a checkout on trunk. The STOP banner in CLAUDE.md requires every file change to go through a worktree -- confirm this is a sanctioned exception (e.g. the doc-only --no-verify path the Workflow gotchas section of CLAUDE.md describes) before proceeding. A dispatched subagent should treat a prompt here as a signal it was not dispatched into an isolated worktree (lode-ska2/lode-jk44/lode-6wgc) and stop rather than approve; a human at the terminal can approve and continue."}}'
fi

exit 0
