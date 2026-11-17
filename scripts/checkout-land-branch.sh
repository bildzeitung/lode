#!/usr/bin/env bash
#
# Check the just-fetched land branch out under this launch worktree's unique
# local name (lode-qh1m, lode-em6v).
#
# The local name is `land/<id>--<worktree directory name>` so two launch
# worktrees can never collide on a bare `land/<id>`, and `/code`'s reclaim
# derives the worktree from that suffix -- so the suffix must be exactly this
# worktree's own directory name. It is derived here because a dispatched
# agent's plain-call sequence cannot carry it from `pwd` except by hand copy
# (lode-fvg0).
#
# Precondition: `git fetch origin land/<id>` ran in this worktree (alone or
# with other refs, in any order). The tip is taken from FETCH_HEAD's own
# `land/<id>` line, never from `FETCH_HEAD` as a rev: that names only the
# FIRST ref of the last fetch, which is another branch after a fetch that
# listed `trunk` first, and another ticket's in a recycled worktree whose own
# fetch never ran. Run it from the worktree root.
#
# Usage: scripts/checkout-land-branch.sh <ticket-id>
#
# Exit 0 -> checked out; prints the local branch name.
# Exit 1 -> cwd is not inside a `.claude/worktrees/` launch worktree; nothing
#           was touched.
# Exit 2 -> the CALL is wrong (argument count).
# Exit 3 -> FETCH_HEAD carries no `land/<id>` entry (the fetch did not run, or
#           fetched a different ticket); nothing was touched.
# Other  -> git's own failure status.

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

# A FETCH_HEAD line is `<sha>\t<not-for-merge flag>\tbranch '<name>' of <url>`.
FETCH_HEAD_FILE="$(git rev-parse --git-path FETCH_HEAD)"
SHA=""
if [ -r "$FETCH_HEAD_FILE" ]; then
  SHA="$(awk -F'\t' -v want="branch 'land/$ID' of " \
    'index($3, want) == 1 { print $1; exit }' "$FETCH_HEAD_FILE")"
fi
if [ -z "$SHA" ]; then
  echo "checkout-land-branch.sh: FETCH_HEAD has no entry for land/$ID -- run" \
    "\`git fetch origin land/$ID\` in this worktree first. Nothing was touched." >&2
  exit 3
fi

BRANCH="land/${ID}--$(basename "$TOP")"
git checkout -B "$BRANCH" "$SHA"
echo "$BRANCH"
