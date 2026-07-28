#!/usr/bin/env bash
#
# Main-checkout identity guard for /land's Section 1 (lode-pcee).
#
# THE BUG THIS REPLACES: Section 1 used to run its `git checkout -f trunk`
# through `git -C "$(git rev-parse --show-toplevel)" checkout -f trunk`, on
# the theory that the `-C` pinned the command to the main checkout. It did
# not: `--show-toplevel` resolves relative to CWD, so the value it produces
# is always wherever you already are -- in the main checkout that's a no-op
# (redundant, harmless), and in a worktree it resolves to THAT WORKTREE's own
# root, never the main checkout. A `-C` computed from cwd cannot redirect a
# command away from cwd; it can only restate it. That reads as a safety guard
# and is not one. Worse, the genuinely destructive line two lines later --
# `git reset --hard origin/trunk` -- carried no `-C` at all, so run from a
# worktree it would hard-reset THAT WORKTREE's own branch, discarding any
# uncommitted work there that no `git reflog` recovers (discarded commits are
# recoverable; discarded uncommitted work is not). `/land` is defined to run
# only in the main checkout (see the top of land/SKILL.md), so this was
# latent, not live, when filed -- but a guard that looks like protection and
# provides none is worse than no guard, because nobody double-checks it.
#
# THE FIX: an IDENTITY check, not a redirect. `git rev-parse --git-common-dir`
# returns the one `.git` directory every worktree of a repo shares, main
# checkout included -- so the main checkout's own toplevel is that
# directory's parent, and a linked worktree's toplevel never is (a linked
# worktree's PRIVATE gitdir is `<common>/.git/worktrees/<name>`, but its
# COMMON dir -- what this reads -- is still the shared `<common>/.git`).
# Unlike `--show-toplevel`, this value does NOT depend on which worktree you
# happen to be standing in, which is exactly what makes it usable to
# DISTINGUISH the two rather than just restate wherever cwd already is.
#
# This script only ASSERTS; it never redirects a command to a different
# directory and never repairs anything. On a mismatch the only sanctioned
# response is to STOP the /land pass and report -- not to retry against a
# guessed-correct directory, and not to `cd` there and continue, since a
# harness that dispatched /land into the wrong directory in the first place
# is a bug an operator needs to see, not something to route around silently.
#
# Usage: assert-main-checkout.sh
#   (no arguments -- this is a pure precondition, nothing to parametrize)
#
# Exit 0 -- cwd's toplevel IS lode's main checkout. Safe to proceed.
# Exit 1 -- cwd's toplevel is NOT the main checkout (e.g. a linked worktree).
#           A diagnostic is already printed to stderr. STOP AND REPORT --
#           /land is defined to run only in the main checkout; do not retry
#           from here.
# Exit 2 -- usage error, OR the repository layout is not the standard
#           non-bare shape this script assumes (`--git-common-dir` not
#           ending in `/.git`, e.g. a bare repo or an unusual $GIT_DIR).
#           Caller bug or an unsupported layout, never a location verdict --
#           do not treat this as "not the main checkout".

set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "usage: $0 (no arguments)" >&2
  exit 2
fi

common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
case "$common_dir" in
  */.git)
    main_checkout="${common_dir%/.git}"
    ;;
  *)
    echo "assert-main-checkout: unsupported repository layout -- --git-common-dir" \
         "returned '$common_dir', which does not end in /.git (a bare repo, or an" \
         "unusual \$GIT_DIR). Cannot safely derive the main checkout path. This is a" \
         "MACHINE FAULT, not a location verdict -- do not treat it as running in the" \
         "wrong directory." >&2
    exit 2
    ;;
esac

cwd_toplevel="$(git rev-parse --show-toplevel)"

if [ "$cwd_toplevel" != "$main_checkout" ]; then
  echo "NOT RUNNING IN THE MAIN CHECKOUT (lode-pcee): cwd resolves to '$cwd_toplevel'," \
       "but the main checkout is '$main_checkout'. /land is defined to run only in the" \
       "main checkout, on trunk -- every command that follows (up to and including" \
       "'git reset --hard') assumes that and carries no -C of its own. Run from here," \
       "checkout would switch THIS directory's branch to trunk and reset --hard would" \
       "destroy any of its uncommitted work, with nothing in reflog to recover it. STOP" \
       "AND REPORT to the operator; do not retry without understanding why cwd was" \
       "wrong." >&2
  exit 1
fi

exit 0
