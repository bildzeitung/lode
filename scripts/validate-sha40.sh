#!/usr/bin/env bash
#
# Validate that a value is a well-formed, full 40-character lowercase-hex git
# SHA -- the shape `metadata.land_head`/`metadata.review_head` must always
# have (lode-xdg3). Real `git rev-parse` output is always exactly 40
# lowercase hex characters; anything else (a truncated copy, an uppercase
# character, a short prefix) is malformed and must be reported as such rather
# than silently compared against a real branch tip and misread as DRIFT.
#
# BACKGROUND: during the /code fan-out of 2026-08-08, a rebase pickup
# (lode-r9z0) wrote a 39-character land_head into bd metadata -- one hex
# digit short of the real branch tip. Nothing in the pipeline would have
# caught it: /land's Section 2a precheck compares metadata.land_head against
# the actual `origin/land/<id>` tip purely to detect DRIFT (a push after the
# ticket was marked ready), and a malformed value never equals a real SHA
# either, so it would have been kicked back `needs-rebase` for no reason --
# a self-inflicted round trip on a branch that was already correct. The
# analogous read site is code-reviewer.md's own review_head comparison. This
# script gives both read sites (and any write site that wants to validate
# before writing) one shared, tested predicate rather than each re-deriving
# a regex inline.
#
# Usage: scripts/validate-sha40.sh <field-name> <value>
#
# Exit 0 -> <value> is exactly 40 lowercase hex characters. Prints nothing.
# Exit 1 -> <value> is NOT well-formed. Prints one diagnostic line to stderr
#           naming the field, the bad value, and why -- the caller's job is
#           to report this as MALFORMED, distinct from drift/mismatch, never
#           to silently fold it into a drift/needs-rebase/bounce path.
#
# Deliberately does NOT check that the value is a real, reachable git object
# -- that is a DIFFERENT question (drift: does it match the actual branch
# tip?) that the caller already asks separately. This script only asks "is
# this even shaped like a SHA".

set -euo pipefail

FIELD="${1:-}"
VALUE="${2:-}"

if [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
  echo "validate-sha40.sh: usage: scripts/validate-sha40.sh <field-name> <value>" >&2
  exit 1
fi

if [[ "$VALUE" =~ ^[0-9a-f]{40}$ ]]; then
  exit 0
fi

echo "validate-sha40.sh: MALFORMED: $FIELD=\"$VALUE\" is not exactly 40 lowercase hex characters" \
  "(got ${#VALUE} chars) -- this is a malformed SHA, not drift. Do not compare it against a real" \
  "branch tip and do not kick the branch back needs-rebase/bounce on that basis; re-derive the" \
  "value fresh (e.g. \`git rev-parse <ref>\`) and re-write the metadata field." >&2
exit 1
