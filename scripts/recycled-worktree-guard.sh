#!/usr/bin/env bash
#
# Recycled-worktree guard (lode-nt98), extracted per lode-ivth out of FOUR
# duplicated inline copies that had already started to drift
# (.claude/agents/coding.md x2, .claude/agents/code-reviewer.md x1,
# .claude/agents/land-review.md x1): shell embedded in a markdown fence gets
# neither shellcheck nor a unit test, and this repo has already shipped
# silent, months-long bugs from exactly that pattern twice before
# (scripts/epic-children-closed.sh, scripts/code-concurrency-cap.sh).
#
# WHAT THIS ASSERTS: the harness's `isolation: "worktree"` dispatch is
# *supposed* to hand a fresh agent a worktree branched off local `trunk`
# HEAD, with zero commits of its own. That assumption has been observed
# FALSE in production (lode-eshl's technical review): a dispatched agent got
# a RECYCLED worktree still checked out on a previous ticket's build branch,
# carrying that ticket's unreviewed commits, rather than a fresh branch off
# `trunk`. A branch-name check alone cannot catch this -- the recycled
# branch still looks like a normal `worktree-agent-...` name -- so this
# script asserts the actual commit graph instead: `HEAD` must be an ancestor
# of local `trunk`.
#
# On failure it repairs the worktree: tags the current (foreign) HEAD as
# `rescue/recycled-<sha>` (the ref `reset --hard` is about to rewind belongs
# to ANOTHER ticket -- if that ticket had committed but not pushed, this tag
# is the only thing standing between its work and oblivion), then
# `git reset --hard trunk`. `git clean -fd` then runs unconditionally right
# after, pass or fail (lode-3v1p) -- see the DIRT-AXIS GAP note below.
#
# Usage: recycled-worktree-guard.sh <context-message>
#   <context-message> is a short clause describing what happens next in the
#   caller's own cycle, e.g. "before doing any work" or "before my own
#   fetch+checkout" -- it is appended to the reported reason so a human
#   reading the transcript knows which cycle's guard fired. This is the one
#   thing that legitimately varies across the four call sites; everything
#   else about the guard is identical, which is the whole point of
#   extracting it to one place.
#
# Exit 0 -- clean: either HEAD was already an ancestor of trunk (nothing to
#           do), or it wasn't and this script just repaired it. Either way
#           the caller's own HEAD is now a clean ancestor of trunk and it is
#           safe to proceed.
# Exit 1 -- REFUSED to run the destructive remediation because the current
#           directory is not inside an isolated launch worktree
#           (`.claude/worktrees/...`). This is the one case a caller must
#           STOP and report rather than proceed -- it means `isolation:
#           "worktree"` did not take at all, and this script will not touch
#           the main checkout. The diagnostic is already printed to stderr.
# Exit 2 -- usage error (wrong argument count). Caller bug, not a worktree
#           problem.
#
# DIRT-AXIS GAP, CLOSED (lode-3v1p): the ancestor check alone cannot detect a
# worktree recycled onto a `land/<id>` branch that has since landed -- HEAD
# is already an ancestor of trunk in that case, so the check above passes
# trivially, exactly as it would for a genuinely fresh worktree. Harmless on
# the ANCESTRY axis (what the guard misses in that case already satisfies
# /land's own worktree-reclaim predicate), but not on the DIRT axis: any
# untracked leftovers from the recycled worktree would otherwise survive,
# since the remediation branch below is the only place `git clean -fd` used
# to run. Closed by running `git clean -fd` unconditionally, right after the
# ancestor check either way (see below) -- a no-op on a genuinely fresh
# worktree (nothing untracked to remove, and it never touches `.gitignore`d
# build state like `venv/`), and it clears exactly the leftover dirt on an
# undetected recycle. Full reasoning: docs/decisions.md (search "lode-3v1p").
#
# BOOTSTRAP GAP, unavoidable, mitigate at the call site (lode-ivth): the
# guard must run FIRST, in a possibly-contaminated worktree, so this very
# script is read FROM that tree. A worktree recycled from a branch cut
# before this script landed on trunk would not have it on disk at all. The
# call site must anchor the path to `$(git rev-parse --show-toplevel)`
# (never the caller's cwd) and treat a missing/non-executable script as a
# reason to STOP AND REPORT -- never as license to silently skip the guard
# and proceed. See the four .claude/agents/*.md call sites for the wrapper
# that does this.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <context-message>" >&2
  exit 2
fi
context_message="$1"

top="$(git rev-parse --show-toplevel)"
case "$top" in
  */.claude/worktrees/*) ;;    # an isolated launch worktree -- safe to repair
  *)
    echo "NOT in an isolated launch worktree ($top): refusing to reset. STOP and report." >&2
    exit 1
    ;;
esac

if ! git merge-base --is-ancestor HEAD trunk; then
  head_sha="$(git rev-parse --short HEAD)"
  echo "CONTAMINATED LAUNCH WORKTREE (lode-nt98): HEAD ($head_sha) is NOT an ancestor of trunk --" \
       "this worktree carries commit(s) foreign to trunk (recycled from a previous ticket's build" \
       "rather than freshly branched off trunk HEAD). Resetting onto current local trunk HEAD" \
       "$context_message." >&2
  git branch "rescue/recycled-$head_sha" HEAD   # keep the evidence -- another ticket's ref
  git reset --hard trunk
fi
git clean -fd   # unconditional (lode-3v1p) -- runs after the `case` either way, still worktree-scoped
