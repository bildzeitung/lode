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
# both smaller and safer; it is not implemented here (it needs a call site
# in every section, i.e. the rot this design avoids) -- see lode-m87j, which
# is ready-for-land but NOT YET merged into trunk as of this fix (fetched
# and read for design context only, never merged into this branch).
#
# CAVEAT 2 -- the stale-lock RECLAIM is now ATOMIC (lode-ao95; previously
# `rm` then create, two steps, OBSERVED admitting two winners 3/40 rounds at
# 8-way contention). The fix serializes the destructive part of a reclaim
# (`rm -f "$LOCK"` + a fresh `write_lock`) behind an `mkdir`-based gate,
# `$LOCK.reclaiming`: `mkdir` is a single atomic syscall, so exactly one
# racer's `mkdir` can ever succeed for that path, no matter how many other
# racers attempt it at once or how many earlier attempts preceded it. Only
# the gate's winner ever touches `$LOCK` in the reclaim path, which is what
# closes the original race.
#
# The OBVIOUS version of this (an O_EXCL token, once, no self-heal) wedges
# landing PERMANENTLY if its winner dies between creating the gate and
# clearing it: every later tick would see the gate already exists and skip
# forever, since nothing ever removes it. That is why this replaces the
# inert trap-based lock with a mechanism no better than what it replaced.
# The fix bounds that risk with a SECOND, much smaller staleness window
# scoped to the gate itself (`RECLAIM_GATE_STALE_SECONDS`, a small constant,
# not an env override -- it only needs to cover "the two lines of actual
# reclaim work" plus scheduler jitter, not a whole /land pass): a later
# acquire that finds an abandoned gate older than that window clears it and
# retries the `mkdir` once. The *decision* at every retry is still a bare
# `mkdir`, atomic regardless of how many stale-gate cleanups preceded it, so
# this self-heal cannot itself produce two winners -- it only ever lets a
# NEW single winner emerge after a crash, never a second one alongside an
# existing legitimate holder.
#
# The gate's winner also RE-VALIDATES that `$LOCK` is still stale immediately
# before touching it (re-reads its epoch fresh, rather than trusting the
# staleness observation from before the gate was won). This closes a second,
# more subtle race: by the time a racer wins the gate, a DIFFERENT racer may
# already have completed its own reclaim (via an earlier, already-cleared
# gate) and written a brand-new, currently-FRESH lock -- winning the gate
# only guarantees exclusivity among would-be reclaimers, it says nothing
# about whether reclaiming is still the right call. Without re-validating,
# the second racer would blindly destroy that legitimate fresh lock.
#
# `acquire`'s own O_EXCL create (the FRESH-lock path, not the reclaim path)
# was always atomic and remains so, unchanged (`write_lock`'s `noclobber`).
#
# A remaining, deliberately-accepted gap: this fix makes `acquire` alone
# atomic, but says nothing about a WRITER that already thinks it holds the
# lock and later re-stamps it blindly (a `heartbeat` subcommand, lode-m87j,
# not present on this branch). If a pass's lock is ever reclaimed out from
# under it by another racer, that original pass -- unaware it lost the lock
# -- could still re-stamp the NEW holder's record, making a genuine overlap
# look like one continuous, legitimate holder (self-concealing rather than
# prevented). Closing that needs the reclaiming/re-stamping code to check an
# OWNER TOKEN before writing, not just a timestamp. This script now records
# one (5th record field, see `lock_record` below) precisely so that future
# check has something to compare against, but does not implement the check
# itself: `heartbeat` does not exist here, and inventing a parallel one that
# does not match what actually ships via lode-m87j risks a confusing,
# hand-reconciled merge for no benefit. See lode-q9pm (the follow-up that
# wires an ownership check into heartbeat once both lode-ao95 and lode-m87j
# are on trunk) and docs/agents-workflow.md's single-lander-lock bullet.
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

# The mkdir-based gate serializing a reclaim's destructive rm+write (CAVEAT
# 2), and the small, fixed staleness bound on the GATE itself (never the
# `LAND_LOCK_STALE_SECONDS` env var -- that window governs the MAIN lock and
# must stay large; this one only has to outlast a couple of shell builtins).
RECLAIM_GATE="$LOCK.reclaiming"
RECLAIM_GATE_STALE_SECONDS=30

new_token() {
  # An opaque, effectively-unique identifier for THIS acquisition -- not a
  # secret, just distinct across concurrent acquirers -- so a future
  # ownership check (lode-q9pm) can tell "the record's current owner" from
  # "was I the one who wrote it". /dev/urandom + od are both part of every
  # base image this repo runs nox on.
  if [ -r /dev/urandom ]; then
    od -An -N8 -tx1 /dev/urandom | tr -d ' \n'
  else
    printf '%s-%s-%s' "$$" "$(date -u +%s)" "$RANDOM"
  fi
}

lock_record() {
  # The one definition of the record's shape. Field order is load-bearing:
  # the reclaim path below reads field 3 (epoch) and nothing else, so fields
  # 1-4 (pid, host, epoch, ISO stamp) keep their original positions -- only
  # field 5 (owner token) is new, appended rather than inserted, so nothing
  # that reads field 3 needs to change. Fields 1, 2 and 4 are for a human
  # reading the file by hand; field 5 is for a future consumer (lode-q9pm),
  # not read back by anything in this script.
  printf '%s %s %s %s %s\n' "$$" "$(hostname)" "$(date -u +%s)" "$(date -u +%FT%TZ)" "$1"
}

write_lock() {
  # Atomic create: `set -o noclobber` makes the `>` redirect fail if the file
  # already exists, so two concurrent FRESH attempts can't both think they
  # got it. Takes the new owner token as $1. This guarantee covers THIS
  # function only -- the reclaim path below never calls it without first
  # winning the exclusive gate (CAVEAT 2), which is what extends the same
  # single-winner property to the reclaim case.
  ( set -o noclobber
    lock_record "$1" > "$LOCK" ) 2>/dev/null
}

if [ "$cmd" = "release" ]; then
  rm -f "$LOCK"
  exit 0
fi

# acquire
TOKEN="$(new_token)"
if write_lock "$TOKEN"; then
  echo "land-lock: acquired (token $TOKEN)"
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

if [ -z "$RECORDED_EPOCH" ]; then
  echo "land-lock: another /land appears to still be running on this machine" \
    "(lock: $RECORD) -- skipping this tick." >&2
  exit 1
fi

AGE=$(( $(date -u +%s) - RECORDED_EPOCH ))
if [ "$AGE" -lt "$STALE_SECONDS" ]; then
  echo "land-lock: another /land appears to still be running on this machine" \
    "(lock: $RECORD) -- skipping this tick." >&2
  exit 1
fi

# Stale -- attempt an ATOMIC reclaim (CAVEAT 2). At most two attempts: once
# outright, and once more after clearing a gate abandoned by a reclaimer that
# crashed between winning it and finishing its rm+write. Every attempt's
# actual decision is a bare `mkdir`, so no number of attempts (by this racer
# or any other) can ever produce two winners.
attempt=0
while [ "$attempt" -lt 2 ]; do
  attempt=$((attempt + 1))

  if mkdir "$RECLAIM_GATE" 2>/dev/null; then
    date -u +%s > "$RECLAIM_GATE/created" 2>/dev/null || true

    # Re-validate before touching $LOCK: winning the gate only guarantees
    # exclusivity among reclaimers, not that reclaiming is still correct.
    # Another racer may already have reclaimed (via a separately-won,
    # already-cleared gate) and written a brand-new, currently-FRESH lock in
    # the interim -- re-read it fresh rather than trusting the staleness
    # this racer observed before it entered this loop.
    if [ -e "$LOCK" ]; then
      CUR_RECORD=""
      read -r CUR_RECORD < "$LOCK" || true
      # shellcheck disable=SC2086
      set -- $CUR_RECORD
      CUR_EPOCH="${3:-}"
      case "$CUR_EPOCH" in
        ''|*[!0-9]*) CUR_EPOCH="" ;;
      esac
      if [ -n "$CUR_EPOCH" ]; then
        CUR_AGE=$(( $(date -u +%s) - CUR_EPOCH ))
        if [ "$CUR_AGE" -lt "$STALE_SECONDS" ]; then
          rm -rf "$RECLAIM_GATE" 2>/dev/null || true
          echo "land-lock: another /land already reclaimed this lock and it" \
            "is now fresh (lock: $CUR_RECORD) -- skipping this tick." >&2
          exit 1
        fi
      fi
    fi

    echo "land-lock: reclaiming stale lock (age ${AGE}s >= ${STALE_SECONDS}s)," \
      "previously held by: $RECORD"
    rm -f "$LOCK"
    if write_lock "$TOKEN"; then
      rm -rf "$RECLAIM_GATE" 2>/dev/null || true
      echo "land-lock: acquired via reclaim (token $TOKEN)"
      exit 0
    fi
    rm -rf "$RECLAIM_GATE" 2>/dev/null || true
    echo "land-lock: lost the race reclaiming the lock -- skipping this tick." >&2
    exit 1
  fi

  # Gate already taken -- either a reclaim is genuinely in progress right now
  # (near-instant; clears within microseconds under normal operation) or a
  # prior reclaimer crashed between winning the gate and clearing it,
  # abandoning it. Self-heal only once the gate itself is older than the
  # small bound above; an unreadable/missing timestamp (a racer that just won
  # the gate a moment ago, about to write it) is treated conservatively as
  # "still in progress", never as abandoned.
  GATE_EPOCH="$(cat "$RECLAIM_GATE/created" 2>/dev/null || true)"
  case "$GATE_EPOCH" in
    ''|*[!0-9]*) GATE_EPOCH="" ;;
  esac
  if [ -z "$GATE_EPOCH" ]; then
    break
  fi
  GATE_AGE=$(( $(date -u +%s) - GATE_EPOCH ))
  if [ "$GATE_AGE" -lt "$RECLAIM_GATE_STALE_SECONDS" ]; then
    break
  fi
  rm -rf "$RECLAIM_GATE" 2>/dev/null || true
  # loop back for the second (and final) attempt
done

echo "land-lock: another /land appears to still be running on this machine" \
  "(lock: $RECORD) -- skipping this tick." >&2
exit 1
