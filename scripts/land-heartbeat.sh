#!/usr/bin/env bash
#
# Re-stamp (or release) the single-lander lock using THIS pass's own token.
# (lode-s9xe.1)
#
# WHY THIS EXISTS. The token read-back was six lines of boilerplate repeated at
# seven sites in .claude/skills/land/SKILL.md -- read the token file, warn if
# empty, call land-lock.sh -- and a coverage gate had to check that every one of
# the seven carried its warning, because paraphrasing one silently dropped it
# out of coverage. One script means one copy to get right.
#
# The token lives in a FILE beside the lock, not a variable: no shell state
# survives between the skill's separate Bash invocations, and it sits outside
# $STATE_DIR because Section 1's per-pass scratch wipe would otherwise delete it
# mid-pass. It is lock state, not per-pass scratch.
#
# Usage:
#   scripts/land-heartbeat.sh                  # re-stamp (best effort, never fails the pass)
#   scripts/land-heartbeat.sh --release        # release at a normal end-of-pass exit
#
# Exit codes: always 0 on the heartbeat path -- lock bookkeeping must never stop
# a pass that is otherwise fine. --release propagates land-lock.sh's own status
# so a caller can report a failed release, but no caller is required to act.
set -u

MODE="heartbeat"
[ "${1:-}" = "--release" ] && MODE="release"

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || TOP=""
GITDIR="$(git rev-parse --git-dir 2>/dev/null)" || GITDIR=""
if [ -z "$TOP" ] || [ -z "$GITDIR" ]; then
  echo "land-heartbeat: WARNING -- not inside a git repository; no $MODE performed" >&2
  exit 0
fi

TOKEN="$(cat "$GITDIR/land-lock-token" 2>/dev/null || true)"

# An absent token is NOT fatal and NOT silently ignored. land-lock.sh refuses a
# blind call outright rather than acting on an unowned lock, so the honest
# outcome is: say so, do nothing, let the staleness window handle it.
if [ -z "$TOKEN" ]; then
  if [ "$MODE" = "release" ]; then
    echo "land: WARNING -- no own-token available; land-lock ownership check is DISABLED for this" \
      "call -- land-lock.sh REFUSES a blind release, so the lock stays held until it ages out" >&2
  else
    echo "land: WARNING -- no own-token available; land-lock ownership check is DISABLED for this" \
      "call -- so this heartbeat simply does not fire" >&2
  fi
  exit 0
fi

if [ "$MODE" = "release" ]; then
  exec "$TOP/scripts/land-lock.sh" release "$TOKEN"
fi

"$TOP/scripts/land-lock.sh" heartbeat "$TOKEN" || true
exit 0
