#!/usr/bin/env bash
#
# Read metadata.review_head, shape-check it, and test it against HEAD in ONE
# process (lode-qh1m) -- the code-reviewer's drift check (lode-xdg3, lode-9b5n).
# Same reason as scripts/handoff-sha.sh: a dispatched agent cannot carry the
# value between plain calls except by copying it, and a copy can be mistyped.
#
# Usage: scripts/check-review-head.sh <ticket-id>
#   Run with the fetched land branch checked out (HEAD = the reviewed tip).
#
# Exit 0 -> FORWARD-ONLY: review_head is well-formed and HEAD or an ancestor
#           of it (a commit is its own ancestor). Not drift.
# Exit 1 -> MALFORMED/MISSING: scripts/validate-sha40.sh rejected the value
#           (its diagnostic is on stderr). Not drift.
# Exit 2 -> the CALL is wrong (argument count); says nothing about the field.
# Exit 3 -> UNREACHABLE: well-formed but not an ancestor of HEAD -- history
#           was rewritten. This is drift.
# Exit 4 -> `bd show` could not be read; says nothing about the field.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "check-review-head.sh: usage: scripts/check-review-head.sh <ticket-id>" \
    "(got $# args) -- a broken CALL, not a verdict about review_head." >&2
  exit 2
fi

ID="$1"
HERE="$(dirname "${BASH_SOURCE[0]}")"

if ! JSON="$(bd show "$ID" --json)"; then
  echo "check-review-head.sh: bd show $ID failed -- cannot read review_head." >&2
  exit 4
fi
VALUE="$(printf '%s' "$JSON" | jq -r '.[0].metadata.review_head // empty')"

rc=0
"$HERE/validate-sha40.sh" review_head "$VALUE" || rc=$?
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

if git merge-base --is-ancestor "$VALUE" HEAD; then
  exit 0
fi
echo "check-review-head.sh: UNREACHABLE: review_head $VALUE is not an ancestor of HEAD" \
  "-- history was rewritten since the hand-off (drift)." >&2
exit 3
