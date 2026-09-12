#!/usr/bin/env bash
#
# Decide whether a single bd ticket should be auto-selected by /code's ready-
# frontier filter, per lode-z1n5 part 2: skip a docs-nits collector whose own
# NIT count is still below the batch-worth-building threshold, so a lone,
# easy-to-fix nit doesn't burn a full /code pass the moment it lands. Shaped
# like scripts/epic-debate-gate.sh -- /code runs it on every candidate that
# already survived the existing human/epic filter, on the auto-select paths
# only (bare /code, --all-ready, --single); an explicitly-named id
# (/code lode-59da) is an operator override and is never gated.
#
# Usage: scripts/docs-nits-threshold-gate.sh <ticket-id>
#
# The threshold is LODE_DOCS_NITS_THRESHOLD (default 3), the same env-var
# mechanism as LODE_CODE_MAX_CONCURRENT_AGENTS, documented in
# docs/configuration.md (where CLAUDE.md routes tunables).
#
# The count is scripts/bd-docs-nit.sh count's own `^NIT` line count for THIS
# ticket only -- a rough batch-size signal (an addendum line counts as its own
# row too), not an exact nit tally; docs/configuration.md says so next to the
# knob. Do not add a second parser. An in_progress collector is never a
# bd-ready candidate to begin with, so this gate never needs to special-case
# one.
#
# Prints exactly one line to stdout:
#   BUILD <id>                                        -- not a docs-nits
#                                                          ticket, or its own
#                                                          count already meets
#                                                          the threshold
#   SKIP <id> docs-nits below threshold (n/N)          -- carries docs-nits
#                                                          and its own count
#                                                          n is below the
#                                                          threshold N
#
# Read-only: only ever calls `bd show` and scripts/bd-docs-nit.sh count
# (itself read-only), never a bd write.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

id="${1:?usage: docs-nits-threshold-gate.sh <ticket-id>}"
threshold="${LODE_DOCS_NITS_THRESHOLD:-3}"

ticket_json="$(bd show "$id" --json)"
has_label="$(printf '%s' "$ticket_json" | jq -r '(.[0].labels // []) | any(. == "docs-nits")')"

if [ "$has_label" != "true" ]; then
  echo "BUILD $id"
  exit 0
fi

count="$(
  "$SCRIPT_DIR/bd-docs-nit.sh" count \
    | awk -F'\t' -v id="$id" '$1 == id { print $3; found=1 } END { if (!found) print 0 }'
)"

if [ "$count" -lt "$threshold" ]; then
  echo "SKIP $id docs-nits below threshold ($count/$threshold)"
else
  echo "BUILD $id"
fi
