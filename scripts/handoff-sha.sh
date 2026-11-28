#!/usr/bin/env bash
#
# Derive, validate and write a SHA hand-off field in ONE process (lode-qh1m).
#
# A dispatched agent cannot carry a value from one command into the next with
# a shell variable or `$(...)` -- the harness worktree-isolation classifier
# refuses both (lode-fvg0) -- so as separate plain calls the SHA would be
# copied by hand from `git rev-parse HEAD`'s output into `validate-sha40.sh`
# and again into `bd update`, and the validator would check one paste while
# the other got written: the lode-0a4n incident class (a 39-character
# review_head). A script is a plain call, so deriving, validating and writing
# inside one process guarantees the value validated is the value written.
#
# Usage: scripts/handoff-sha.sh <ticket-id> <review_head|land_head> [--push] [bd-update-args...]
#   Runs: bd update <ticket-id> --set-metadata <field>=<HEAD> [bd-update-args...]
#   Extra arguments (labels, land_summary, ...) pass through to `bd update`
#   untouched; this script owns only the SHA-valued field and `--push`.
#   --push (lode-fg7w7.5): after the write succeeds, run scripts/bd-dolt-push.sh,
#   saving the separate publish call. It is recognised only as the first
#   argument after the field, so a pass-through value is never mistaken for it.
#   Never use it where bd state is written between this call and the push (the
#   builder's escalation path adds land-escalated after it), because the push
#   would publish too early.
#
# Exit 0 -> HEAD was derived, validated and written.
# Exit 1 -> the derived value failed scripts/validate-sha40.sh (its own
#           diagnostic is on stderr); nothing was written.
# Exit 2 -> the CALL is wrong (argument count, unknown field); nothing was
#           written. Never a verdict about the content.
# Exit 3 -> the value was fine but `bd update` itself failed (its output is on
#           stderr); the field was not written.
# Exit 4 -> origin does not carry HEAD as the tip of land/<ticket-id> (never
#           pushed, push rejected, or a commit made after the push); nothing
#           was written. Push first, then re-run. Recording such a SHA reads
#           as drift to /land's 2a precheck, which bounces a correct branch
#           (lode-tx33). One `git ls-remote` read per call is accepted.
# Exit 5 -> `git ls-remote origin` itself failed (no origin, network); nothing
#           was written.
# Exit 6 -> `--push` only: the field WAS written, but scripts/bd-dolt-push.sh
#           failed (its own exit status, including exit 1 = its guard refused,
#           is NOT passed through, so it cannot be read as one of 1-5 and look
#           like a write that did not happen). Re-run scripts/bd-dolt-push.sh;
#           do not re-run this script. A `--push` call killed mid-publish (a
#           tool timeout) reports no status of its own, so the "written" line
#           printed before the push is what says which half is done.
# Other  -> git's own failure status from `rev-parse HEAD` (no repo, unborn
#           HEAD); nothing was written.

set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "handoff-sha.sh: usage: scripts/handoff-sha.sh <ticket-id> <review_head|land_head>" \
    "[--push] [bd-update-args...] (got $# args) -- a broken CALL, nothing was written." >&2
  exit 2
fi

ID="$1"
FIELD="$2"
shift 2

PUSH=0
if [ "${1-}" = "--push" ]; then
  PUSH=1
  shift
fi

case "$FIELD" in
  review_head | land_head) ;;
  *)
    echo "handoff-sha.sh: unknown field '$FIELD' (want review_head or land_head)" \
      "-- a broken CALL, nothing was written." >&2
    exit 2
    ;;
esac

HERE="$(dirname "${BASH_SOURCE[0]}")"
SHA="$(git rev-parse HEAD)"

# `exit $?`, never `exit 1`: the validator's own 1-vs-2 distinction passes through.
"$HERE/validate-sha40.sh" "$FIELD" "$SHA" || exit $?

if ! TIP="$(git ls-remote origin "refs/heads/land/$ID")"; then
  echo "handoff-sha.sh: git ls-remote origin failed; $FIELD was NOT written." >&2
  exit 5
fi
TIP="${TIP%%[[:space:]]*}"
if [ "$TIP" != "$SHA" ]; then
  echo "handoff-sha.sh: origin/land/$ID is '${TIP:-absent}', not HEAD $SHA" \
    "-- push HEAD first; $FIELD was NOT written." >&2
  exit 4
fi

if ! bd update "$ID" --set-metadata "$FIELD=$SHA" "$@"; then
  echo "handoff-sha.sh: bd update failed for $ID; $FIELD was NOT written." >&2
  exit 3
fi

[ "$PUSH" -eq 1 ] || exit 0

echo "handoff-sha.sh: $FIELD written for $ID; publishing with bd-dolt-push.sh" \
  "-- if this call is cut short, run scripts/bd-dolt-push.sh alone (not this script)." >&2
if ! "$HERE/bd-dolt-push.sh"; then
  echo "handoff-sha.sh: $FIELD WAS written for $ID but bd-dolt-push.sh failed" \
    "-- re-run scripts/bd-dolt-push.sh (not this script)." >&2
  exit 6
fi
