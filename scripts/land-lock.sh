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
# that re-stamps the token as the pass progresses lets the window be both
# smaller and safer; it is not implemented on THIS branch, which was built
# strictly against a trunk that had none. lode-m87j has since landed and
# trunk now has one -- see the MERGE NOTE below before reconciling the two.
#
# CAVEAT 2 -- the stale-lock RECLAIM is now ATOMIC (lode-ao95; previously
# `rm` then create, two steps, OBSERVED admitting two winners 3/40 rounds at
# 8-way contention).
#
# READ THE EXPOSURE FIRST, or the rest of this section will read as far more
# urgent than it is. Under the documented operating convention -- ONE
# `/loop 5m /land` on one machine -- acquires are issued SERIALLY, and a pass
# that is still running holds a FRESH lock. So the reclaim branch is
# crash-recovery only: reaching it concurrently at all needs two independent
# /land invocations arriving within milliseconds of each other, 30+ minutes
# after a crash. The contention figures quoted below (8-way, 32-way) are
# STRESS-TEST conditions chosen to make a rare interleaving reproducible in
# seconds -- they are not an observed production failure rate, and nothing
# here should be read as "landing is losing races today". This is hardening
# against the convention being violated, which is exactly why it is worth
# keeping the mechanism as simple as it can be rather than as clever.
#
# The fix serializes the destructive part of a reclaim
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
# forever, since nothing ever removes it -- leaving landing no better off
# than under the inert trap-based lock this whole script replaced. The fix
# bounds that risk with a SECOND, much smaller staleness window scoped to
# the gate itself (`RECLAIM_GATE_STALE_SECONDS`, a small constant, not an
# env override -- it only needs to cover "the two lines of actual reclaim
# work" plus scheduler jitter, not a whole /land pass): a later acquire that
# finds an abandoned gate older than that window clears it and retries the
# `mkdir` once. The gate-taken branch also DATES an untimestamped gate it
# finds, so that a reclaimer killed in the gap between `mkdir` and writing
# the stamp cannot leave behind a gate nothing is ever able to age out --
# without that, the permanent wedge above returns through a one-syscall
# window (OBSERVED: three consecutive ticks skipping, gate still present,
# against a lock 100000s stale).
#
# The gate's winner also RE-VALIDATES `$LOCK` immediately before touching it,
# and reclaims ONLY on positive proof that it is still the same stale record
# (present, parseable, still past the window). Winning the gate guarantees
# exclusivity among would-be reclaimers; it says nothing about whether
# reclaiming is still the right call. Two readings in particular must abort
# rather than proceed -- an ABSENT file (a reclaim already in flight) and an
# UNPARSEABLE one -- and getting that backwards is itself a two-winner bug,
# measured at 11 of 60 rounds at 32-way contention. The reasoning is at the
# check itself, in the reclaim loop below; it is the subtlest part of this
# script, so read it there before changing it.
#
# WHAT THIS DOES *NOT* CLOSE -- the gate self-heal can still admit two
# winners, and the claim it cannot is false: OBSERVED, 2 winners, with a gate
# holder that is ALIVE but stalled past RECLAIM_GATE_STALE_SECONDS between
# passing re-validation and its `rm`+write. A later tick judges that gate
# abandoned, clears it, wins a new one, re-validates (the lock is still the
# original stale record -- the stalled holder has not written yet), and
# reclaims; the stalled holder then resumes and completes its own reclaim on
# top. Both exit 0. Re-validation cannot help: the displaced holder passed it
# BEFORE stalling. This is a bounded-risk tradeoff, not a closed hole, and it
# is the price of self-healing at all -- the alternative (never clear a gate)
# is the permanent wedge above, which is strictly worse because it needs no
# race to trigger. The window is deliberately generous: the guarded critical
# section is a handful of forks, so 30s of stall means a machine already in
# serious trouble. Do NOT reduce RECLAIM_GATE_STALE_SECONDS to "tighten" this
# -- a smaller window makes displacing a live holder MORE likely, not less.
# Closing it properly needs the reclaimer to verify it still owns the gate
# immediately before the destructive `rm`, i.e. the same owner-token check
# lode-q9pm exists to add (see below).
#
# `acquire`'s own O_EXCL create (the FRESH-lock path, not the reclaim path)
# was always atomic and remains so, unchanged (`write_lock`'s `noclobber`).
#
# A remaining, deliberately-accepted gap: this fix makes `acquire` alone
# atomic, but says nothing about a WRITER that already thinks it holds the
# lock and later re-stamps it blindly (the `heartbeat` subcommand, lode-m87j
# -- see the MERGE NOTE below). If a pass's lock is ever reclaimed out from
# under it by another racer, that original pass -- unaware it lost the lock
# -- could still re-stamp the NEW holder's record, making a genuine overlap
# look like one continuous, legitimate holder (self-concealing rather than
# prevented). That is not hypothetical: in the stalled-gate-holder case
# above, the surviving record names the DISPLACED holder, not the racer that
# reclaimed after it -- the file already lies about who holds the lock, and
# a blind heartbeat would keep it looking healthy indefinitely. Closing that
# needs the re-stamping code to check an OWNER TOKEN before writing, not
# just a timestamp. This script records one (5th record field, see
# `lock_record` below) precisely so that check has something to compare
# against; implementing the check is lode-q9pm.
#
# MERGE NOTE (read before resolving a conflict in this file). This branch was
# built against a trunk with no `heartbeat`; lode-m87j has since LANDED, so
# trunk now has one and this file conflicts with it. The conflict is
# SEMANTIC, not just textual, and resolving it by taking either side wholesale
# is wrong in a way no current test catches:
#   * trunk's `heartbeat` calls `lock_record` with NO argument. This branch's
#     `lock_record` reads the owner token from `$1`. Under `set -u` that call
#     fails outright -- taking this side wholesale BREAKS heartbeat.
#   * Taking trunk's 4-field `lock_record` wholesale drops the owner token and
#     silently reverts the reclaim to the non-atomic `rm`-then-create form.
#   * The correct resolution keeps this branch's reclaim gate AND trunk's
#     `heartbeat`, and must make `heartbeat` PRESERVE the existing token
#     rather than regenerate or blank it -- a heartbeat that mints a fresh
#     token every tick destroys the one thing lode-q9pm needs to compare
#     against, turning the field into decoration while looking correct.
# Whoever resolves it: add a test pinning "a heartbeat leaves field 5
# unchanged" in the same commit, or the invariant has no gate behind it.
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
  # base image this repo runs nox on, so there is no fallback branch: one
  # that fired only where `od` is missing would hard-fail at the same
  # `set -euo pipefail` anyway, while minting a SECOND token shape that
  # lode-q9pm's comparison would then have to tolerate for no gain.
  od -An -N8 -tx1 /dev/urandom | tr -d ' \n'
}

lock_record() {
  # The one definition of the record's shape. Field order is load-bearing:
  # the reclaim path below reads field 3 (epoch) and nothing else, so fields
  # 1-4 (pid, host, epoch, ISO stamp) keep their original positions -- only
  # field 5 (owner token) is new, appended rather than inserted, so nothing
  # that reads field 3 needs to change. Fields 1, 2 and 4 are for a human
  # reading the file by hand; field 5 is for a future consumer (lode-q9pm),
  # not read back by anything in this script.
  #
  # The token is a MANDATORY positional, and that is deliberate -- see the
  # MERGE NOTE in the header. Defaulting it (`${1:-...}`) would let trunk's
  # argument-less `heartbeat` call keep working through a merge while either
  # blanking the token or minting a fresh one every tick, silently destroying
  # the ownership continuity the field exists to provide, with trunk's five
  # heartbeat tests still green. Requiring it makes that merge fail loudly
  # instead. Do not "fix" this by adding a default.
  printf '%s %s %s %s %s\n' "$$" "$(hostname)" "$(date -u +%s)" "$(date -u +%FT%TZ)" "$1"
}

epoch_of() {
  # Field 3 of a lock record if it is a legible epoch, empty otherwise
  # ("age unknown"). Both readers below parse identically -- only their
  # VERDICTS differ, and each keeps its own at its own call site. Confining
  # the positional-param split to a function also keeps it from clobbering
  # the script's own "$@" twice at top level.
  # shellcheck disable=SC2086  # deliberate word-split of the record
  set -- $1
  case "${3:-}" in
    ''|*[!0-9]*) ;;
    *) printf '%s' "$3" ;;
  esac
}

skip_lock_still_held() {
  # The lock is present and not (yet) reclaimable. One wording, two callers.
  echo "land-lock: another /land appears to still be running on this machine" \
    "(lock: $1) -- skipping this tick." >&2
  exit 1
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
RECORDED_EPOCH="$(epoch_of "$RECORD")"

if [ -z "$RECORDED_EPOCH" ]; then
  skip_lock_still_held "$RECORD"
fi

AGE=$(( $(date -u +%s) - RECORDED_EPOCH ))
if [ "$AGE" -lt "$STALE_SECONDS" ]; then
  skip_lock_still_held "$RECORD"
fi

# Stale -- attempt an ATOMIC reclaim (CAVEAT 2). Exactly two attempts: once
# outright, and once more after clearing a gate abandoned by a reclaimer that
# crashed between winning it and finishing its rm+write. Every attempt's
# actual decision is a bare `mkdir`, so no number of attempts by THIS racer
# can produce a second winner alongside another racer that is keeping its
# gate (the case that CAN, an alive-but-stalled holder, is the documented
# residual in the header -- it is not created by retrying here).
for _ in 1 2; do

  if mkdir "$RECLAIM_GATE" 2>/dev/null; then
    date -u +%s > "$RECLAIM_GATE/created" 2>/dev/null || true

    # Re-validate before touching $LOCK: winning the gate only guarantees
    # exclusivity among reclaimers, not that reclaiming is still CORRECT.
    # Reclaim only on POSITIVE proof that $LOCK is still the same stale
    # record this tick set out to reclaim -- present, parseable, and still
    # past the window. Every other reading aborts, in the same conservative
    # direction as the outer staleness check above:
    #
    #   * FILE ABSENT is NOT "nothing to protect" -- it means a reclaim is
    #     already IN FLIGHT: some other racer's `rm -f "$LOCK"` has landed
    #     and its `write_lock` has not. Proceeding here is not merely
    #     redundant, it IS the two-winner bug: this racer's own `rm -f` then
    #     deletes whichever record lands in that gap -- including the
    #     legitimate one a top-level `write_lock` is creating at that very
    #     moment, since the FRESH path needs no gate and is racing the same
    #     window. That racer exits 0 believing it holds a lock this one has
    #     already destroyed and overwritten. OBSERVED, not derived: 11 of 60
    #     rounds at 32-way contention, traced to exactly this interleaving,
    #     before this check was made conservative.
    #   * UNPARSEABLE is the same "age unknown" the outer check refuses to
    #     guess at. Holding the gate is not extra information about the
    #     record's age, so it must not turn "unknown" into "safe to destroy".
    CUR_RECORD=""
    read -r CUR_RECORD < "$LOCK" 2>/dev/null || true
    CUR_EPOCH="$(epoch_of "$CUR_RECORD")"
    if [ -z "$CUR_EPOCH" ] \
    || [ "$(( $(date -u +%s) - CUR_EPOCH ))" -lt "$STALE_SECONDS" ]; then
      rm -rf "$RECLAIM_GATE" 2>/dev/null || true
      echo "land-lock: the lock is no longer the stale record this tick set" \
        "out to reclaim (now: ${CUR_RECORD:-<absent>}) -- another /land has" \
        "reclaimed it or is mid-reclaim; skipping this tick." >&2
      exit 1
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
  # small bound above.
  GATE_EPOCH="$(cat "$RECLAIM_GATE/created" 2>/dev/null || true)"
  case "$GATE_EPOCH" in
    ''|*[!0-9]*) GATE_EPOCH="" ;;
  esac
  if [ -z "$GATE_EPOCH" ]; then
    # No legible creation stamp. Either the winner has not written it yet
    # (microseconds old -- the normal case) or it was killed in the gap
    # between `mkdir` and that write and never will. Skipping outright would
    # make the second case a PERMANENT WEDGE: nothing but this branch ever
    # removes a gate, so every later tick would read the same missing stamp,
    # skip again, and report "another /land appears to still be running"
    # about a lock that is by then hours stale -- landing blocked until a
    # human deletes .git/land.lock.reclaiming by hand. That is precisely the
    # wedge this gate's staleness bound exists to rule out, so leaving one
    # reachable here would defeat it (OBSERVED before this branch existed:
    # three consecutive ticks, gate still present, stale lock untouched).
    # Date it ourselves instead. A live winner finishes long before the
    # window elapses and re-stamps it a moment later anyway (harmless); a
    # dead one now leaves a gate that a later tick can age out normally.
    date -u +%s > "$RECLAIM_GATE/created" 2>/dev/null || true
    break
  fi
  GATE_AGE=$(( $(date -u +%s) - GATE_EPOCH ))
  if [ "$GATE_AGE" -lt "$RECLAIM_GATE_STALE_SECONDS" ]; then
    break
  fi
  rm -rf "$RECLAIM_GATE" 2>/dev/null || true
  # loop back for the second (and final) attempt
done

# Fell out of the loop: the lock IS reclaimable, but another racer holds the
# reclaim gate. Deliberately NOT the "another /land appears to still be
# running" wording the two checks above use -- that names $LOCK, and an
# operator who reads it while the real obstruction is a leftover
# $LOCK.reclaiming goes and inspects the wrong file. lode-aps3's own rule:
# "lock was not held" must be observable, never silent or misattributed.
echo "land-lock: the lock is reclaimable (age ${AGE}s >= ${STALE_SECONDS}s)" \
  "but another /land holds the reclaim gate $RECLAIM_GATE -- skipping this" \
  "tick. If this repeats for more than ${RECLAIM_GATE_STALE_SECONDS}s the" \
  "gate is abandoned and the next tick clears it automatically." >&2
exit 1
