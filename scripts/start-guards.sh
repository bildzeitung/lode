#!/usr/bin/env bash
#
# Start-of-cycle guard wrapper (lode-fg7w7.4; lode-htkr3 proposal 2): one tool call for the two
# guards every dispatched agent ran back to back as separate calls.
#
# Runs scripts/isolation-guard.sh, then scripts/recycled-worktree-guard.sh "<context-message>",
# and stops at the first one that exits nonzero. The two stay distinct sibling scripts with their
# own tests (lode-ska2): this wrapper only sequences them, it re-implements neither predicate and
# weakens neither. The isolation guard must run first -- the recycled guard's reset + clean is
# only safe once isolation has been confirmed (its own `case` is the second line of defense).
#
# Usage: start-guards.sh <context-message>
#   <context-message> is passed through verbatim as recycled-worktree-guard.sh's argument.
#
# Exit 0 -- both guards passed (the recycled guard may have repaired the worktree; see its header).
# Exit 2 -- usage error (wrong argument count). Nothing ran.
# Exit 3 -- isolation-guard.sh exited nonzero (its own status is named in the diagnostic, and its
#           own message is printed above). The recycled guard did not run. STOP AND REPORT.
# Exit 4 -- isolation-guard.sh passed but recycled-worktree-guard.sh exited nonzero (its status is
#           named in the diagnostic). STOP AND REPORT.
#
# 3 and 4 are chosen to avoid 1 and 2, which both guards use, so the caller can tell WHICH guard
# refused from the status alone. A wrapper that cannot be found or run exits 127 / 126 as for any
# script: a bootstrap gap, also STOP AND REPORT.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <context-message>" >&2
  exit 2
fi
context_message="$1"

here="$(dirname "${BASH_SOURCE[0]}")"

status=0
"$here/isolation-guard.sh" || status=$?
if [ "$status" -ne 0 ]; then
  echo "start-guards: isolation-guard.sh REFUSED (exit $status) -- recycled-worktree-guard.sh did" \
       "not run. STOP AND REPORT." >&2
  exit 3
fi

status=0
"$here/recycled-worktree-guard.sh" "$context_message" || status=$?
if [ "$status" -ne 0 ]; then
  echo "start-guards: recycled-worktree-guard.sh REFUSED (exit $status) -- isolation-guard.sh had" \
       "passed. STOP AND REPORT." >&2
  exit 4
fi
