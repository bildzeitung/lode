#!/usr/bin/env bash
#
# Single-lander lock for /land (lode-aps3), extracted out of an inline
# Section 0 snippet in .claude/skills/land/SKILL.md that relied on
# `trap 'rm -f "$LOCK"' EXIT` to release the lock at the end of a /land pass.
#
# THE BUG THIS REPLACES: an agent running this skill executes every fenced
# `bash` block as its own, separate Bash tool invocation -- nothing carries
# over between them (the governing rule at the top of SKILL.md: "no fenced
# block may depend on shell state from another", lode-sfnb). A `trap ... EXIT`
# set inside Section 0's block fires the instant THAT block's shell exits --
# which is before Section 1 even runs, not at the end of the pass. The lock
# was therefore held for the duration of one Bash call, not the pass it was
# supposed to serialize (VERIFIED LIVE 2026-07-27, bd show lode-aps3).
#
# It failed doubly open: the old reclaim path checked `kill -0 $OWNER_PID`
# against the PID recorded by whichever block last wrote the lock file. In
# this per-block-invocation architecture that check is not merely broken, it
# is STRUCTURALLY MEANINGLESS -- a Bash tool invocation's shell process has,
# by definition, already exited by the time a LATER invocation runs (control
# only returns to the caller once the shell exits), so `$OWNER_PID` recorded
# by any prior block is *always* dead by the time a later block reads it,
# whether or not a /land pass is still genuinely in progress (just running a
# later block/section). PID liveness cannot distinguish "the pass is still
# running, just between Bash calls" from "the pass crashed" here.
#
# THE FIX: no trap, no PID-liveness check. Liveness is instead a wall-clock
# STALENESS TOKEN -- the lock records when it was acquired, and a later
# `acquire` reclaims it only once that timestamp is older than
# LAND_LOCK_STALE_SECONDS (default 1800s / 30min: generous enough that a
# genuinely still-running pass -- dispatching several land-review subagents,
# then a combined re-gate -- is never mistaken for dead; same order of
# magnitude as the existing "stuck job" reclaim convention in
# docs/configuration.md's "Stale-running reclaim timeout" row, 900s/15min,
# for the same kind of judgment: "this looks abandoned, not merely slow").
#
# This staleness reclaim is the SOLE mechanism that is guaranteed to release
# an abandoned lock -- it needs no cooperation from any particular exit site
# and cannot be silently broken by a future editor adding a new "stop the
# pass" exit to SKILL.md and forgetting to release. SKILL.md additionally
# calls `release` explicitly at the two points a normal pass is guaranteed to
# reach (the empty-queue exit in Section 1, and the end of a full pass in
# Section 4) purely as an optimization, to keep the common `/loop 5m /land`
# cadence tight rather than waiting out the staleness window on every routine
# tick. Every OTHER exit (a machine-fault stop, an internal assertion
# failure) relies on the staleness reclaim rather than a release call --
# deliberately: threading an explicit release through the many "stop the
# pass" exits scattered through SKILL.md would reproduce the exact bug class
# this script exists to fix (easy to add one later and forget the release),
# where a single, unconditional TTL cannot rot the same way. Documented
# tradeoff, not an oversight: an aborted pass can leave the lock held for up
# to LAND_LOCK_STALE_SECONDS before the next tick can proceed.
#
# Usage: scripts/land-lock.sh acquire
#        scripts/land-lock.sh release
#
# acquire: exit 0 -> lock acquired (fresh, or reclaimed from a stale prior
#                     holder). Caller proceeds with its /land pass.
#          exit 1 -> another /land is still (plausibly) running on this
#                     machine. Caller must skip this tick cleanly (exit 0 of
#                     its OWN, per the "single lander" convention) -- do not
#                     queue, do not run in parallel. Diagnostic on stdout.
# release: always exit 0 -- `rm -f` is idempotent, and a caller that never
#           held the lock (e.g. it just skipped the tick above) must be able
#           to call this harmlessly too.
#
# Lock file lives under .git/ (per-machine, never committed) -- same path the
# snippet this replaces used: $(git rev-parse --git-dir)/land.lock.

set -euo pipefail

if [ "$#" -ne 1 ] || { [ "$1" != "acquire" ] && [ "$1" != "release" ]; }; then
  echo "usage: $0 acquire|release" >&2
  exit 2
fi
cmd="$1"

LOCK="$(git rev-parse --git-dir)/land.lock"
STALE_SECONDS="${LAND_LOCK_STALE_SECONDS:-1800}"

write_lock() {
  # Atomic create: `set -o noclobber` makes the `>` redirect fail if the file
  # already exists, so two concurrent attempts can't both think they got it.
  ( set -o noclobber
    printf '%s %s %s %s\n' "$$" "$(hostname)" "$(date -u +%s)" "$(date -u +%FT%TZ)" \
      > "$LOCK" ) 2>/dev/null
}

if [ "$cmd" = "release" ]; then
  rm -f "$LOCK"
  exit 0
fi

# acquire
if write_lock; then
  exit 0
fi

# Lock file already exists. Read its recorded acquire time (3rd field: epoch
# seconds) to judge staleness -- never the PID (1st field), which is kept in
# the file purely for a human reading it by hand, not for any liveness logic
# (see header). A malformed or unreadable lock file (truncated write,
# hand-edited, ...) is treated as "age unknown" rather than crashing this
# script: stay conservative and skip this tick rather than guess.
RECORDED_EPOCH="$(awk '{print $3}' "$LOCK" 2>/dev/null || true)"
case "$RECORDED_EPOCH" in
  ''|*[!0-9]*) RECORDED_EPOCH="" ;;
esac

if [ -n "$RECORDED_EPOCH" ]; then
  NOW_EPOCH="$(date -u +%s)"
  AGE=$(( NOW_EPOCH - RECORDED_EPOCH ))
  if [ "$AGE" -ge "$STALE_SECONDS" ]; then
    echo "land-lock: reclaiming stale lock (age ${AGE}s >= ${STALE_SECONDS}s)," \
      "previously held by: $(cat "$LOCK" 2>/dev/null)"
    rm -f "$LOCK"
    if write_lock; then
      exit 0
    fi
    echo "land-lock: lost the race reclaiming the lock -- skipping this tick." >&2
    exit 1
  fi
fi

echo "land-lock: another /land appears to still be running on this machine" \
  "(lock: $(cat "$LOCK" 2>/dev/null)) -- skipping this tick." >&2
exit 1
