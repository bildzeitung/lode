#!/usr/bin/env bash
#
# Fetch `land/<id>` (and `trunk`) from origin, then check the fetched branch
# out under this launch worktree's unique local name (lode-qh1m, lode-em6v).
#
# The local name is `land/<id>--<worktree directory name>` so two launch
# worktrees can never collide on a bare `land/<id>`, and `/code`'s reclaim
# derives the worktree from that suffix -- so the suffix must be exactly this
# worktree's own directory name. It is derived here because a dispatched
# agent's plain-call sequence cannot carry it from `pwd` except by hand copy
# (lode-fvg0).
#
# The fetch runs in this process (lode-n7g4), so the FETCH_HEAD read below is
# always the one this call wrote: a successful fetch replaces the file and a
# failed one exits 4 before any checkout, so a stale same-ticket entry left by
# an earlier cycle in a recycled worktree can never be checked out.
# `trunk` rides along so origin/trunk is current for what runs AFTER the
# checkout (the rebase pickup's `git merge origin/trunk`); the guards that run
# before this script read origin/trunk unfetched on purpose. The tip is taken
# from FETCH_HEAD's own `land/<id>` line, never from `FETCH_HEAD` as a rev, so
# the pick does not depend on the order the fetch lists its refs. Run it from
# the worktree root.
#
# Usage: scripts/checkout-land-branch.sh <ticket-id>
#
# Exit 0 -> checked out; prints the local branch name.
# Exit 1 -> cwd is not inside a `.claude/worktrees/` launch worktree; nothing
#           was fetched or checked out.
# Exit 2 -> the CALL is wrong (argument count).
# Exit 3 -> FETCH_HEAD carries no `land/<id>` entry after a successful fetch;
#           nothing was checked out.
# Exit 4 -> `git fetch origin land/<id> trunk` failed (e.g. no such remote
#           branch, network); nothing was checked out.
# Exit 5 -> the fetch succeeded but `git checkout -B` refused (e.g. local
#           changes it would overwrite); git's error is printed above, and
#           HEAD and the local branch are left as they were.
# Other  -> git's own failure status from `rev-parse --show-toplevel` (128:
#           cwd is not in a git work tree); nothing was fetched or checked out.

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

if ! git fetch -q origin "land/$ID" trunk; then
  echo "checkout-land-branch.sh: \`git fetch origin land/$ID trunk\` failed" \
    "-- nothing was checked out." >&2
  exit 4
fi

# A FETCH_HEAD line is `<sha>\t<not-for-merge flag>\tbranch '<name>' of <url>`.
# `|| SHA=""` so awk's own status (2 on a read error) cannot surface as this
# script's exit 2, which means a broken call.
FETCH_HEAD_FILE="$(git rev-parse --git-path FETCH_HEAD)"
SHA=""
if [ -r "$FETCH_HEAD_FILE" ]; then
  SHA="$(awk -F'\t' -v want="branch 'land/$ID' of " \
    'index($3, want) == 1 { print $1; exit }' "$FETCH_HEAD_FILE")" || SHA=""
fi
if [ -z "$SHA" ]; then
  echo "checkout-land-branch.sh: FETCH_HEAD has no entry for land/$ID after" \
    "a successful fetch. Nothing was checked out." >&2
  exit 3
fi

BRANCH="land/${ID}--$(basename "$TOP")"
if ! git checkout -q -B "$BRANCH" "$SHA"; then
  echo "checkout-land-branch.sh: \`git checkout -B $BRANCH\` failed" \
    "-- the fetch succeeded; the checkout was refused." >&2
  exit 5
fi
echo "$BRANCH"
