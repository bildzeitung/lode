#!/usr/bin/env bash
#
# Isolation guard (lode-ska2 / lode-jk44) -- a DIFFERENT harness failure from
# scripts/recycled-worktree-guard.sh (lode-nt98), and deliberately not folded
# into it (lode-ska2 asked for these two to stay documented as distinct
# siblings, not merged).
#
# WHAT THIS ASSERTS: `isolation: "worktree"` is supposed to guarantee a
# dispatched agent's cwd starts inside a fresh launch worktree under
# `.claude/worktrees/`. That assumption has been observed FALSE in production
# for a `code-reviewer` dispatch (lode-ska2, discovered while technically
# reviewing lode-b2bf; the same fault reached again via `/code`'s step-1
# stranded-review sweep, lode-jk44): the agent's cwd was pinned to the MAIN
# CHECKOUT at the repo root, checked out on `trunk` -- not a RECYCLED
# worktree (lode-nt98's failure mode, which is a worktree, just the wrong
# one), but NO WORKTREE AT ALL. Both documented self-rescue routes were
# refused by the harness itself: `EnterWorktree(name=...)` -> "cannot create
# a worktree from a subagent with a cwd override"; `EnterWorktree(path=...)`
# -> "the current working directory is the repository root, not an isolated
# worktree". Nothing MECHANICAL then stopped the dispatched agent from
# running Edit/Write/`nox -t fix` directly against the main checkout ON
# TRUNK -- only an English "if my cwd is trunk, STOP" instruction held
# (confirmed to have actually held that one time; the incident's own agent
# then invented an unsanctioned `git worktree add` + `git -C` workaround
# instead of stopping, which is the exact behavior this script forecloses).
#
# This closes the gap the same way lode-ivth closed lode-nt98's: a single,
# lint-checked, unit-tested precondition, run as the FIRST executable action
# of the cycle -- before bd show, before EnterWorktree is even considered,
# before anything else.
#
# NOT a merge with recycled-worktree-guard.sh: that script's job is "is my
# worktree the RIGHT one" (is HEAD an ancestor of trunk), and on failure it
# performs a destructive REPAIR (reset + clean) because a wrong-but-real
# worktree can safely be reset onto trunk. This script's job is the
# logically prior question -- "do I have an isolated worktree AT ALL" -- and
# it never repairs anything: there is no safe way to fabricate an isolated
# worktree from inside a non-isolated context, so the only sanctioned
# response to failure is a hard stop, never a self-provisioned fallback.
#
# THE JUDGMENT CALL THIS SCRIPT ENCODES (lode-ska2): `git worktree add` +
# `git -C` is NOT a sanctioned fallback, even though the incident's
# code-reviewer invented it mid-flight and completed its review correctly
# that one time. Per CLAUDE.md's "simplest solution first", and because
# auto-recovering from a broken dispatch would hide a harness bug an
# operator needs visibility into (and because self-provisioning a worktree
# under fan-out risks name collisions and orphaned state nobody owns
# cleaning up), the only response to a failed isolation check is to STOP and
# report -- never to self-provision. A caller must not treat this script's
# exit 1 as an invitation to try EnterWorktree or `git worktree add`
# afterward; both are documented dead ends (see the call sites in
# .claude/agents/coding.md, .claude/agents/code-reviewer.md, and
# .claude/agents/land-review.md) and inventing a further workaround is
# exactly the behavior lode-ska2 exists to close off.
#
# Usage: isolation-guard.sh
#   (no arguments -- this is a pure precondition, nothing to parametrize)
#
# Exit 0 -- cwd is inside an isolated launch worktree (`.claude/worktrees/...`).
#           Safe to proceed to the next step of the cycle -- typically
#           `scripts/recycled-worktree-guard.sh`, which asserts the
#           NARROWER "is it the right worktree" question.
# Exit 1 -- cwd is NOT inside an isolated launch worktree. The diagnostic is
#           already printed to stderr. STOP AND REPORT -- do not attempt
#           EnterWorktree, `git worktree add`, or any other self-rescue.
# Exit 2 -- usage error (an argument was given). Caller bug, not a worktree
#           problem.

set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "usage: $0 (no arguments)" >&2
  exit 2
fi

top="$(git rev-parse --show-toplevel)"
case "$top" in
  */.claude/worktrees/*)
    exit 0
    ;;
  *)
    echo "NOT DISPATCHED INTO AN ISOLATED WORKTREE (lode-ska2 / lode-jk44): cwd resolves to" \
         "'$top', not a path under .claude/worktrees/. \`isolation: \"worktree\"\` did not take" \
         "for this dispatch. STOP AND REPORT to the operator now -- do NOT attempt" \
         "EnterWorktree (both forms are documented dead ends: 'cannot create a worktree from a" \
         "subagent with a cwd override' / 'the current working directory is the repository" \
         "root, not an isolated worktree'), do NOT attempt 'git worktree add' as a" \
         "self-rescue, and do NOT run Edit/Write/nox against this checkout. This is a hard" \
         "stop, not a decision point." >&2
    exit 1
    ;;
esac
