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
# LAND_LOCK_STALE_SECONDS (default 1800s / 30min). This staleness reclaim is
# the SOLE mechanism that is guaranteed to release an abandoned lock: it
# needs no cooperation from any particular exit site, so it cannot be
# silently broken by a future editor adding a new "stop the pass" exit to
# SKILL.md and forgetting to release. SKILL.md also calls `release`
# explicitly at two sites (Section 1's empty-queue exit, the end of Section
# 4) purely to keep the common `/loop 5m /land` cadence tight; MANY other
# exits reach neither and wait out the TTL instead -- see the two caveats
# below, and docs/agents-workflow.md's single-lander-lock bullet, which is
# the design home for this mechanism and for LAND_LOCK_STALE_SECONDS.
#
# CAVEAT 1 -- the TTL measures the age of the ACQUISITION, not idle time.
# Nothing refreshes the token mid-pass, so LAND_LOCK_STALE_SECONDS must
# exceed the TOTAL wall-clock duration of the longest legitimate pass (N
# land-review Opus dispatches, a combined re-gate, per-branch isolation
# replay on red, `validate-mermaid.sh`'s docker run, `lock_currency`'s
# network resolve) -- not merely the longest gap between two Bash calls. A
# pass that genuinely runs longer than the window has its own lock judged
# stale and reclaimed by the next tick, mid-`trunk`-merge. That is the
# DANGEROUS direction, and it is why 1800s is not reduced: the opposite
# failure (an abandoned lock blocking landing for up to 30min / ~6 skipped
# ticks) only DELAYS landing, which is not latency-critical. A heartbeat
# that re-stamps the token as the pass progresses would let the window be
# both smaller and safer; it is not implemented (it needs a call site in
# every section, i.e. the rot this design avoids). See lode-m87j.
#
# CAVEAT 2 -- `acquire` is atomic, the RECLAIM path is not. `write_lock`'s
# O_EXCL create genuinely admits one winner; the reclaim below is `rm` THEN
# create, two steps, so two racers that both see the same stale lock can
# both proceed (one deletes the fresh lock the other just wrote). OBSERVED,
# not theoretical: 3 of 40 rounds at 8-way contention against a stale lock
# ended with two winners. Unreachable under the documented operating
# convention (ONE `/loop 5m /land` on one machine issues acquires serially,
# and a still-running pass holds a FRESH lock, so the reclaim branch is
# crash-recovery only) -- and strictly better than the inert lock this
# replaces, which admitted every overlap. Closing it properly needs a
# nested TTL of its own (an O_EXCL reclaim token wedges landing permanently
# if its holder dies in the two lines after creating it). See lode-ao95.
#
# Usage: scripts/land-lock.sh acquire
#        scripts/land-lock.sh release
#
# acquire: exit 0 -> lock acquired (fresh, or reclaimed from a stale prior
#                     holder). Caller proceeds with its /land pass.
#                     Diagnostic (if any) on stdout.
#          exit 1 -> another /land is still (plausibly) running on this
#                     machine, or the lock file could not be created at all.
#                     Caller must skip this tick cleanly (exit 0 of its OWN,
#                     per the "single lander" convention) -- do not queue, do
#                     not run in parallel. Diagnostic on STDERR.
#          exit 2 -> usage error (a caller bug, never a lock verdict).
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
  # That guarantee covers THIS function only -- the reclaim path below calls
  # it after an `rm`, which is not atomic as a pair (header, caveat 2).
  # Fields: pid and the ISO stamp are for a human reading the file by hand;
  # only field 3 (epoch) is ever read back programmatically.
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

# `write_lock` failed. Distinguish the two reasons, because they need opposite
# remedies and the misleading one blocks landing indefinitely: the lock file
# genuinely exists (another pass, or an abandoned one), OR it could not be
# created at all (unwritable/missing .git, a full disk). `write_lock` discards
# its own stderr to keep the ordinary "already exists" case quiet, so the file
# itself is the only signal left -- name the machine fault explicitly rather
# than reporting it as "another /land is running" for as long as it persists
# (lode-aps3's own notes: "lock was not held" must be observable, never silent).
if [ ! -e "$LOCK" ]; then
  echo "land-lock: MACHINE FAULT -- cannot create $LOCK (unwritable or missing" \
    "git dir, or no space). This is not another lander; landing stays blocked" \
    "until it is fixed. Skipping this tick." >&2
  exit 1
fi

# Read the recorded acquire time (3rd field: epoch seconds) to judge staleness
# -- never the PID (1st field), which is human-only (see header). One `read`
# serves both the staleness check and the diagnostics below. A malformed or
# unreadable record (truncated write, hand-edited, ...) is treated as "age
# unknown" rather than crashing: stay conservative and skip rather than guess.
RECORD=""
read -r RECORD < "$LOCK" || true
# shellcheck disable=SC2086  # deliberate word-split of the record into fields
set -- $RECORD
RECORDED_EPOCH="${3:-}"
case "$RECORDED_EPOCH" in
  ''|*[!0-9]*) RECORDED_EPOCH="" ;;
esac

if [ -n "$RECORDED_EPOCH" ]; then
  AGE=$(( $(date -u +%s) - RECORDED_EPOCH ))
  if [ "$AGE" -ge "$STALE_SECONDS" ]; then
    echo "land-lock: reclaiming stale lock (age ${AGE}s >= ${STALE_SECONDS}s)," \
      "previously held by: $RECORD"
    rm -f "$LOCK"
    if write_lock; then
      exit 0
    fi
    echo "land-lock: lost the race reclaiming the lock -- skipping this tick." >&2
    exit 1
  fi
fi

echo "land-lock: another /land appears to still be running on this machine" \
  "(lock: $RECORD) -- skipping this tick." >&2
exit 1
