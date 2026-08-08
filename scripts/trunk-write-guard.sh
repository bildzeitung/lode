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
# RULING 1 (docs/decisions.md, /sweep escalation walk-through 2026-08-08): do NOT attempt to
# disambiguate a dispatched subagent from the main session. The documented PreToolUse payload
# carries no agent-role field, and a stranded subagent and a legitimate main-session trunk edit
# both resolve to the same checkout root -- there is no way to tell them apart from inside the
# hook. Gate on the BRANCH instead, which IS derivable, and return permissionDecision "ask" -- NOT
# "deny". A human at the terminal can approve the prompt and proceed (the doc-only --no-verify path
# CLAUDE.md's "Workflow gotchas" section describes still works, at the cost of one confirmation); a
# dispatched subagent cannot approve anything and is stopped. Human presence becomes the
# discriminator without the payload ever encoding it.
#
# RULING 2: the STOP banner at the top of CLAUDE.md governs AUTHORING file changes; the "Workflow
# gotchas" section's "doc-only --no-verify" pattern describes MERGE/LAND mechanics (how to commit
# without dragging .beads/issues.jsonl along), not a parallel authoring path -- so there is no real
# conflict for this hook to resolve. It was never literally "NEVER": /land writes trunk every pass
# and /sweep pushes it.
#
# RULING 3: unlike the three other PreToolUse(Bash) guards in .claude/settings.json (bd-deps-blocks,
# gh-write, sha-fabrication), this guard needs NO jq -- it never parses tool_input, since branch
# name and cwd are read directly from the process environment via plain git. It deliberately adds
# NOTHING to the lode-oii9 deny-everything-when-jq-is-missing surface.
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

top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$top" ] || exit 0

branch="$(git -C "$top" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
[ -n "$branch" ] || exit 0

if [ "$branch" = "trunk" ]; then
  printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "lode-p8zl: this Edit/Write targets a checkout on trunk. CLAUDE.md'"'"'s STOP banner requires every file change to go through a worktree -- confirm this is a sanctioned exception (e.g. the doc-only --no-verify path CLAUDE.md'"'"'s Workflow gotchas section describes) before proceeding. A dispatched subagent should treat a prompt here as a signal it was not dispatched into an isolated worktree (lode-ska2/lode-jk44/lode-6wgc) and stop rather than approve; a human at the terminal can approve and continue."}}'
fi

exit 0
