#!/usr/bin/env bash
#
# /code's per-ticket reclaim of a finished reviewer/rebase-pickup LAUNCH
# worktree (lode-7ndi), extracted out of the inline shell in
# .claude/skills/code/SKILL.md's "reclaim" block (lode-vs7g) so the
# destructive calls it performs -- `git worktree unlock`, `git worktree
# remove --force`, `git branch -D` -- get the same shellcheck + real-repo
# pytest coverage as scripts/worktree-gc-sweep.sh / worktree-gc-classify.sh,
# rather than living ungated in a markdown fence (lode-mh9g).
#
# WHY THIS EXISTS ON TOP OF THE EXISTING SINGLE-`--force` RECLAIM: that
# reclaim's premise was "the harness unlocks a launch worktree the moment
# its agent exits, so a still-locked worktree just means the agent hasn't
# returned yet -- wait, don't force." Observed FALSE in production: one of
# two finished code-reviewer dispatches stayed locked for 10+ minutes after
# its completion notification, and a 60-second retry loop of the single
# `--force` never cleared it (this ticket's own description). The lock
# recorded is PER-SESSION (the orchestrator's pid + starttime), not
# per-agent -- already measured independently, lode-yrtu -- so while the
# orchestrating session stays alive, nothing downstream (this script
# included, and /land's sweep via worktree-lock-stale.sh) can ever prove
# that pid dead. But the ORCHESTRATOR holds strictly better information
# than any liveness probe: it holds the completion notification for that
# exact agent, and the lock's pid is its own session. So on that positive
# evidence -- the ticket id names a worktree whose lock reason names ITS
# OWN directory -- this script unlocks explicitly before the single-force
# remove, rather than waiting on a lock nothing will ever clear.
#
# SCOPE, DELIBERATELY NARROW:
#   * Only worktrees on a `land/<id>--*` branch are ever considered -- the
#     lode-em6v/lode-vs7g derivation every reviewer/rebase-pickup already
#     uses. A builder's own `worktree-agent-*` branch never matches this
#     glob, so the lode-oqr pre-first-commit lock is untouched by
#     construction, not by a separate check.
#   * A locked worktree is unlocked ONLY when its lock reason contains that
#     worktree's own `agent-<dirname>` token -- the harness's own launch
#     lock, naming the very directory it locked. A lock recorded under any
#     OTHER reason (a human's own `git worktree lock`, or -- structurally
#     impossible today, but not asserted against -- a reason naming a
#     different worktree) is left alone and reported, never unlocked.
#   * Still a single `--force`, never `-f -f` -- explicit unlock-on-evidence
#     replaces the wait-for-the-harness assumption; it does not license
#     overriding a lock this script cannot explain.
#   * `git worktree remove` runs BEFORE `git branch -D`, same order as the
#     existing reclaim -- git refuses to delete a branch that is still
#     checked out anywhere.
#
# TWO DELIBERATE ABSTENTIONS, both matching scripts/worktree-gc-sweep.sh:
#   * Not sourced from scripts/gate-lib.sh, though the "GATE COULD NOT RUN"
#     banner below is that library's -- gate-lib.sh's exit 2 means "could not
#     judge the CONTENT", and this is a reclaim with a precondition guard, not
#     a content gate.
#   * scripts/worktree-gc-classify.sh is NOT consulted. Its keep-dirty /
#     keep-notmerged arms exist for a BUILDER worktree, whose uncommitted work
#     may be the only copy. A launch worktree's content is already on
#     `origin/land/<id>` by the time its agent returns (SKILL.md's "safe on
#     both outcomes"), so there is nothing here for those arms to protect --
#     which is also why this reclaim predates the classifier rather than
#     diverging from it.
#
# Usage: scripts/code-reclaim-launch-worktree.sh <ticket-id>
#
# For every worktree whose branch matches `land/<ticket-id>--*`:
#   - unlocked                         -> remove + delete branch (unlocked-and-clean case)
#   - locked, reason names own dirname -> unlock, remove + delete branch
#   - locked, reason names something else -> KEPT, reported on stdout
#   - locked with NO reason at all        -> KEPT, reported on stdout
#   - branch is `worktree-agent-*`     -> never matched by the glob; untouched
#
# No matching worktree at all is a silent no-op (exit 0) -- most tickets
# never had a reviewer/pickup dispatch.
#
# Exit codes: 0 always, on both a successful sweep and a kept-lock skip --
# this mirrors the existing inline reclaim's own contract (best-effort
# housekeeping, not a gate). Exit 2 only for a usage error / not a git repo.
set -u

if [ "$#" -ne 1 ] || [ -z "${1:-}" ]; then
  echo "usage: $0 <ticket-id>" >&2
  exit 2
fi
ID="$1"

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "GATE COULD NOT RUN: not inside a git repository" >&2
  exit 2
}

# Run from a worktree, this would enumerate that worktree's own siblings --
# same precondition, same guard, as scripts/worktree-gc-sweep.sh.
"$TOP/scripts/assert-main-checkout.sh" || exit 2

RECLAIMED=0
KEPT_LOCKED=0

# ONE porcelain pass supplies path, branch and lock state together. IFS is an
# explicit tab, not whitespace, so an empty field keeps its own column instead
# of shifting the ones after it -- a detached worktree's missing branch is safe
# here even though it is no longer the trailing field.
#
# The "L" prefix on the lock field is what separates "locked" from "locked WITH
# a reason": a bare `git worktree lock` records no reason at all, but it is
# still a lock, and an unprefixed empty string would read as unlocked -- such a
# worktree would then be silently skipped by a remove that quietly fails on the
# lock, instead of being kept and reported.
while IFS=$'\t' read -r WT BR LOCK_INFO; do
  case "$BR" in
    "land/$ID--"*) ;;
    *) continue ;;
  esac

  DIRNAME="${WT##*/}"

  if [ -n "$LOCK_INFO" ]; then
    LOCK_REASON="${LOCK_INFO#L}"
    case "$LOCK_REASON" in
      *"$DIRNAME"*)
        # Positive evidence: the lock names THIS worktree's own directory --
        # the harness's launch lock -- so explicit unlock before the single
        # -force remove, rather than waiting on a per-session lock nothing
        # will ever clear while the orchestrator stays alive.
        git -C "$TOP" worktree unlock "$WT" 2>/dev/null || true
        ;;
      *)
        echo "kept (lock is not this worktree's own launch lock): $WT (reason: ${LOCK_REASON:-<none recorded>})"
        KEPT_LOCKED=$((KEPT_LOCKED + 1))
        continue
        ;;
    esac
  fi

  # Single --force still: it fails safe if the unlock above raced or failed --
  # never `-f -f`.
  if git -C "$TOP" worktree remove --force "$WT" 2>/dev/null; then
    git -C "$TOP" branch -D "$BR" >/dev/null 2>&1 || true
    RECLAIMED=$((RECLAIMED + 1))
  fi
done < <(git -C "$TOP" worktree list --porcelain | awk '
  /^worktree /{p=$2; b=""; l=""}
  /^branch /  {b=$2; sub("refs/heads/","",b)}
  /^locked/   {l="L" substr($0,8)}
  /^$/        {if (p!="") print p"\t"b"\t"l; p=""}
  END         {if (p!="") print p"\t"b"\t"l}')

echo "code-reclaim-launch-worktree($ID): reclaimed=$RECLAIMED kept-locked=$KEPT_LOCKED"
exit 0
