#!/usr/bin/env bash
#
# Check the just-fetched land branch out under this launch worktree's unique
# local name (lode-qh1m, lode-em6v).
#
# The local name is `land/<id>--<worktree directory name>` so two launch
# worktrees can never collide on a bare `land/<id>`, and `/code`'s reclaim
# derives the worktree from that suffix -- so the suffix must be exactly this
# worktree's own directory name. Deriving it here replaces an agent copying it
# by hand from `pwd`'s output (a plain-call sequence cannot carry it, lode-fvg0).
#
# Precondition: `git fetch origin land/<id>` just ran, so FETCH_HEAD is that
# branch's tip. Run it from the worktree root.
#
# Usage: scripts/checkout-land-branch.sh <ticket-id>
#
# Exit 0 -> checked out; prints the local branch name.
# Exit 1 -> cwd is not inside a `.claude/worktrees/` launch worktree; nothing
#           was touched.
# Exit 2 -> the CALL is wrong (argument count).
# Other  -> git's own failure status from `checkout -B`.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "checkout-land-branch.sh: usage: scripts/checkout-land-branch.sh <ticket-id>" \
    "(got $# args) -- a broken CALL, nothing was touched." >&2
  exit 2
fi

ID="$1"
TOP="$(git rev-parse --show-toplevel)"

case "$TOP" in
  */.claude/worktrees/*) ;;
  *)
    echo "checkout-land-branch.sh: $TOP is not a .claude/worktrees/ launch worktree" \
      "-- refusing to check out here." >&2
    exit 1
    ;;
esac

BRANCH="land/${ID}--$(basename "$TOP")"
git checkout -B "$BRANCH" FETCH_HEAD
echo "$BRANCH"
