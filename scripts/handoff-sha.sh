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
# Usage: scripts/handoff-sha.sh <ticket-id> <review_head|land_head> [bd-update-args...]
#   Runs: bd update <ticket-id> --set-metadata <field>=<HEAD> [bd-update-args...]
#   Extra arguments (labels, land_summary, ...) pass through to `bd update`
#   untouched; this script owns only the SHA-valued field.
#
# Exit 0 -> HEAD was derived, validated and written.
# Exit 1 -> the derived value failed scripts/validate-sha40.sh (its own
#           diagnostic is on stderr); nothing was written.
# Exit 2 -> the CALL is wrong (argument count, unknown field); nothing was
#           written. Never a verdict about the content.
# Exit 3 -> the value was fine but `bd update` itself failed (its output is on
#           stderr); the field was not written.
# Other  -> git's own failure status from `rev-parse HEAD` (no repo, unborn
#           HEAD); nothing was written.

set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "handoff-sha.sh: usage: scripts/handoff-sha.sh <ticket-id> <review_head|land_head>" \
    "[bd-update-args...] (got $# args) -- a broken CALL, nothing was written." >&2
  exit 2
fi

ID="$1"
FIELD="$2"
shift 2

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

if ! bd update "$ID" --set-metadata "$FIELD=$SHA" "$@"; then
  echo "handoff-sha.sh: bd update failed for $ID; $FIELD was NOT written." >&2
  exit 3
fi
