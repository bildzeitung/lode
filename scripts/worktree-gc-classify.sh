#!/usr/bin/env bash
#
# Pure, side-effect-free classification of ONE `.claude/worktrees/` candidate
# into the bucket `/land`'s Section 4 worktree-GC backstop sweep uses to
# decide what to do with it (lode-9owc).
#
# WHY THIS EXISTS: lode-yrtu added TWO new safety-critical predicates to that
# sweep in one change and treated them differently -- the stale-lock check was
# extracted to scripts/worktree-lock-stale.sh with a real unit-test suite
# (tests/test_worktree_lock_stale.py, 9 cases against real processes and real
# /proc, no mocking), matching the shape of scripts/recycled-worktree-guard.sh
# (lode-ivth) and scripts/isolation-guard.sh -- but the DIR-ONLY RECLAIM
# predicate (branch shape + age-since-last-commit + the lode-9hgu dirty-tree
# guard, gating a `git worktree remove --force` that DESTROYS a directory) was
# left as inline bash in a markdown fence, with no mechanical coverage at all.
# lode-ivth's own rationale applies verbatim here: shell embedded in a
# markdown fence gets neither shellcheck nor a unit test. This script gives
# the whole per-candidate decision -- not just the dir-only arm -- exactly
# that coverage; see tests/test_worktree_gc_classify.py.
#
# SCOPE: this script decides; it never acts. The two DESTRUCTIVE calls
# (`git worktree remove --force`, `git branch -D`) stay inline in
# `.claude/skills/land/SKILL.md` Section 4, which is also the only place that
# ever resolves a STALE lock (via scripts/worktree-lock-stale.sh +
# `git worktree unlock`) -- a real mutation, so it cannot live in a script
# whose entire contract is "no side effects." By the time SKILL.md calls this
# script, any stale lock has already been resolved and unlocked; the <locked>
# argument below reflects that resolution, not the raw porcelain bit.
#
# Usage:
#   worktree-gc-classify.sh <worktree-path> <head-sha> <locked:0|1> \
#                            <branch-name> <min-age-seconds>
#
#   <worktree-path>    the candidate's path, e.g. .claude/worktrees/agent-abc
#                       (as `git worktree list --porcelain` printed it).
#   <head-sha>         its HEAD commit sha.
#   <locked:0|1>        1 if `git worktree lock` currently, GENUINELY holds it
#                       -- i.e. the caller has already run it through
#                       scripts/worktree-lock-stale.sh and, if stale, has
#                       already `git worktree unlock`-ed it and is passing 0
#                       here instead. This script performs no staleness check
#                       and no unlock itself.
#   <branch-name>       the branch checked out in the worktree, or "" if the
#                       worktree is DETACHED. Passed as its own argv element
#                       (never parsed out of a shared tab-delimited string),
#                       so an empty branch can never shift a later field --
#                       the hazard SKILL.md's own field-order comment warns
#                       about for its porcelain `read` is structurally absent
#                       here.
#   <min-age-seconds>   the dir-only reclaim age floor -- SKILL.md derives
#                       this once from `${LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS
#                       :-21600}` and passes the resolved number through, so
#                       the tunable's one documented home
#                       (docs/agents-workflow.md, not configuration.md per
#                       that page's own scope note) is unchanged.
#
# Must run with cwd inside the git repo whose `trunk` and `origin/*` refs are
# being judged -- exactly where /land's Section 4 loop already runs from (the
# main checkout root).
#
# Prints exactly one bucket name to stdout and exits 0:
#
#   full-reclaim    HEAD is CAPTURED -- merged into `trunk`, or (for a
#                   branch-attached worktree) an ancestor of its own
#                   `origin/<branch>` counterpart (lode-amif; `${br%%--*}`
#                   strips the lode-em6v worktree-uniqueness suffix first) --
#                   AND the tree is provably clean. Caller may
#                   `git worktree remove --force` AND `git branch -D` the
#                   branch.
#   dir-only        NOT captured, but <branch-name> matches `worktree-agent-*`
#                   (a builder's own branch, never pushed to origin -- lode-
#                   yrtu), its last commit is at least <min-age-seconds> old,
#                   and the tree is provably clean. Caller may remove the
#                   DIRECTORY only -- never the branch ref, which is the only
#                   place its commits stay reachable.
#   keep-locked     <locked> was 1. Caller must not touch this worktree.
#   keep-notmerged  NOT captured, and either <branch-name> doesn't match
#                   `worktree-agent-*` at all (a `land/<id>`-branched
#                   reviewer/rebase-pickup worktree, a detached worktree --
#                   empty <branch-name> can never match the glob, anything
#                   else) or it does but its last commit is too young (or its
#                   timestamp is unreadable) -- fails SAFE, keep. Caller must
#                   not touch it.
#   keep-dirty      Otherwise eligible for full-reclaim or dir-only, but the
#                   tree is not PROVABLY clean -- including when
#                   `git -C <worktree-path> status --porcelain` itself errors
#                   (a vanished directory, corrupt worktree admin, ...),
#                   which fails CLOSED into this bucket rather than ever
#                   risking `--force` on a worktree that could not even be
#                   read. Caller must not touch it.
#
# Exit 2 -- usage error (wrong argument count). Never a verdict about the
#           worktree.
#
# NO BEHAVIOUR CHANGE (lode-9owc acceptance criterion): every branch below is
# a direct port of SKILL.md Section 4's own inline logic -- same ancestry
# arms, same dirty-tree exclude list, same age floor, same branch-shape
# `case`. Do not "improve" the ordering or the exclude list here without
# updating the loop's own long-standing comments alongside it (they still
# hold the fuller historical rationale for each choice: lode-h1vn, lode-amif,
# lode-9hgu, lode-yrtu, lode-bns3).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -ne 5 ]; then
  echo "usage: $0 <worktree-path> <head-sha> <locked:0|1> <branch-name> <min-age-seconds>" >&2
  exit 2
fi
wt="$1"
sha="$2"
locked="$3"
br="$4"
min_age_seconds="$5"

if [ "$locked" = "1" ]; then
  echo "keep-locked"
  exit 0
fi

# lode-9hgu's dirty-tree guard, defined ONCE -- the single most safety-
# critical test in this whole classification, so the `:(exclude)` pathspec
# list must not exist in two copies that can drift apart (this is now the
# ONLY copy; SKILL.md's former inline copy is retired since every reclaim
# decision funnels through here). Returns 0 only when the tree is PROVABLY
# clean. The two-step `st=$(...)` then `[ -z "$st" ]` is load-bearing and must
# stay two steps: `status --porcelain` prints nothing BOTH when the tree is
# clean AND when the command itself errors (missing dir, corrupt worktree
# admin, ...), and an assignment inherits its command substitution's exit
# code -- so success and emptiness are tested SEPARATELY, making an error
# skip exactly like a dirty tree instead of failing OPEN. `local st` is its
# own line for the same reason: `local st=$(...)` would mask the
# substitution's exit code behind `local`'s own.
#
# lode-bns3: the passive bd export is EXCLUDED from this judgment, so a
# staged/modified `.beads/*.jsonl` can never read as "dirty" and zero out the
# sweep -- it is BY INVARIANT never real work (import.auto: false, lode-6ra).
#
# lode-do3q: the excluded relpaths are READ from the canonical
# scripts/beads-passive-exports.txt rather than spelled out here (docs/decisions.md
# has the why). Built ONCE at load, and FAIL-LOUD if the list is unreadable or
# empty: unlike the Stop hook's best-effort sibling
# (scripts/discard-beads-passive-export-churn.sh), this is a gate, and an empty
# exclude list would silently INVERT lode-bns3 -- passive-export churn would read
# as "dirty" and zero out the sweep with nothing red.
#
# lode-xlcm: the load+validate+":(exclude)" transform itself is owned by the
# sourced helper scripts/beads-passive-exports.sh (this script keeps its own
# fail-loud exit-2 semantics on top of the helper's plain return code).
# shellcheck source=beads-passive-exports.sh
if ! . "$SCRIPT_DIR/beads-passive-exports.sh"; then
  echo "$0: cannot source $SCRIPT_DIR/beads-passive-exports.sh" >&2
  exit 2
fi
if ! load_beads_passive_exports "$SCRIPT_DIR/beads-passive-exports.txt"; then
  exit 2
fi

wt_provably_clean() {
  local st
  st=$(git -C "$1" status --porcelain -- . "${BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS[@]}" 2>&1) && [ -z "$st" ]
}

# WIDENED PREDICATE (lode-amif): "merged into trunk" is a PROXY for "this
# worktree's content already exists safely elsewhere." A branch-attached
# worktree also counts as captured once its HEAD is an ancestor of its own
# `origin/<branch>` counterpart -- reached regardless of which locally-
# suffixed name (lode-em6v) the branch was checked out under.
#
# Kept as the ONE `||`-chained condition SKILL.md itself used, character for
# character (only the variable names are lowercased), so "is this still a
# direct port?" stays answerable by reading the two side by side rather than
# by re-deriving an intermediate flag's truth table. Note the ASYMMETRIC
# stderr: the origin arm silences it (an absent `origin/<branch>` is the
# ROUTINE case for a builder's never-pushed branch -- one `fatal:` per
# candidate, per pass, saying nothing), while the trunk arm deliberately does
# NOT, because the only way IT can error is a missing or broken `trunk` --
# never routine, and the one diagnostic worth surfacing on a sweep that ends
# in `--force`.
if git merge-base --is-ancestor "$sha" trunk \
  || { [ -n "$br" ] && git merge-base --is-ancestor "$sha" "origin/${br%%--*}" 2>/dev/null; }; then
  if wt_provably_clean "$wt"; then
    echo "full-reclaim"
  else
    echo "keep-dirty"
  fi
  exit 0
fi

# NOT captured. A builder's OWN worktree-agent-* branch is never pushed to
# origin (coding.md), so its directory can leak forever once its ticket is
# abandoned, bounced, or its build simply dies (lode-yrtu). Reclaim the
# DIRECTORY ONLY once its last commit is old enough and the tree is clean,
# keeping the branch ref so its commits stay reachable. Every OTHER
# not-merged shape (a `land/<id>`-branched reviewer/rebase-pickup worktree, a
# detached worktree -- empty $br can never match the case pattern below --
# anything else) is unchanged: kept, full stop.
case "$br" in
  worktree-agent-*)
    # AGE GUARD, deliberately NOT the lock start-token check used for the
    # LOCKED bucket: that token only exists while the worktree is actually
    # locked, and a build unlocks right after its first commit (lode-oqr)
    # while continuing, unlocked, for the rest of its cycle -- so age of the
    # last commit is the only signal left, and it fails SAFE (a build still
    # cycling has a recent HEAD commit almost by construction; the branch ref
    # is kept either way, so no commit is ever lost).
    last_commit_ts=$(git -C "$wt" log -1 --format=%ct 2>/dev/null) || last_commit_ts=""
    now=$(date +%s)
    # CLAMP A NEGATIVE AGE TO 0 (lode-o7rt): `now` and the commit's committer
    # timestamp are each whole-second reads of the SAME wall clock, taken a
    # few milliseconds apart by two different processes (this script's `date`
    # call runs strictly after the commit that produced $last_commit_ts), so
    # ordinarily `now - last_commit_ts` cannot go negative. It can under a
    # genuine backward step of the wall clock between those two reads (an NTP
    # correction, or -- suspected here, flaky specifically under `-n 8` -- a
    # CPU-saturated guest VM catching up its clock with a small backward
    # correction after being descheduled by the hypervisor under load). The
    # unclamped comparison only ever tests a negative age against
    # $min_age_seconds via `-ge`, which fails even when $min_age_seconds is 0
    # -- exactly the age floor
    # test_dir_only_reclaim_removes_the_directory_but_keeps_the_ref sets to
    # mean "no floor at all", turning a routine, real commit into a spurious
    # keep-notmerged. A commit whose apparent age reads negative is, for
    # classification purposes, a commit that just happened -- age 0, never
    # "impossibly not old enough" -- so clamp before comparing. This changes
    # behavior only when $min_age_seconds is 0: an unclamped negative already
    # failed any -ge check for a wider floor, same as a clamped age of 0
    # would. With the production default (21600s) a clock-skewed commit still
    # correctly reads as too young to reclaim.
    age=$((now - last_commit_ts))
    if [ -n "$last_commit_ts" ] && [ "$age" -lt 0 ]; then
      age=0
    fi
    if [ -n "$last_commit_ts" ] && [ "$age" -ge "$min_age_seconds" ]; then
      if wt_provably_clean "$wt"; then
        echo "dir-only"
      else
        echo "keep-dirty"
      fi
    else
      echo "keep-notmerged"    # too young (or commit ts unreadable) -- fail safe, keep
    fi
    ;;
  *)
    echo "keep-notmerged"
    ;;
esac
