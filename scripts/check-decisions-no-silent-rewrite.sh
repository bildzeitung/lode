#!/usr/bin/env bash
#
# Silent-in-place-rewrite guard for docs/decisions.md (lode-rl6s).
#
# tests/test_decisions_supersession_markers.py already guards the two
# marker-SHAPE defects lode-ur6o found (an off-pattern lead-in, a
# line-wrapped id). Both scans key on an artifact a *marker* leaves behind.
# A silent in-place rewrite of an EXISTING entry -- editing or deleting
# previously-committed text instead of appending a new entry or a correctly
# shaped `**Update (<id>...)` marker, the exact thing docs/decisions.md's own
# preamble forbids -- leaves nothing for a marker-shaped scan to key on
# (lode-nlk6's documented limitation; confirmed to actually bite once by
# lode-hg49, whose reviewer had to hand-restore a rewritten entry).
#
# This script closes that gap with git's own diff, not a marker-shape scan:
# between two points in history, did any PRE-EXISTING, non-blank line of
# docs/decisions.md disappear? git diff already answers that precisely --
# no heuristic needed at this scope.
#
# SCOPE, DELIBERATELY: base..head, not the whole repository history. A
# full-history replay was tried and rejected -- even with a word-set
# heuristic meant to tolerate ordinary paragraph rewrapping (a later append
# widening a paragraph shifts word-wrap boundaries, "removing" and
# "re-adding" the same words on different lines), dozens of commits made
# since the append-only convention itself was established (lode-ur6o) still
# flag. Full-history replay is not viable without a much heavier per-entry
# content-hash mechanism the ticket's own acceptance criteria treats as a
# separate, costlier option. A single branch's diff against its merge base is
# not reflow-prone the way 250 historical commits are, so the strict, no-heuristic
# form is the right size for the check actually needed: catching a rewrite
# INSIDE ONE REVIEW'S diff, at review/land time.
#
# Usage: scripts/check-decisions-no-silent-rewrite.sh <base-ref> [<head-ref>]
#   <head-ref> defaults to HEAD.
#
# Exit 0 -> no pre-existing non-blank line of docs/decisions.md was removed
#           between <base-ref> and <head-ref>. Prints nothing.
# Exit 1 -> at least one was. Prints each offending removed line, prefixed
#           with "REMOVED: ", to stdout.
# Exit 2 -> USAGE/MACHINE fault (missing arg, not a git repo, bad ref, git
#           itself failing). Diagnostic to stderr. Per lode-9i2p's rule
#           (validate-mermaid.sh, merge-precheck.sh's own exit 2): this is a
#           MACHINE fault, not a verdict on the content -- callers must not
#           treat it as "no rewrite found."

set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 <base-ref> [<head-ref>]" >&2
  exit 2
fi

BASE_REF="$1"
HEAD_REF="${2:-HEAD}"

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "check-decisions-no-silent-rewrite.sh: not inside a git repository" >&2
  exit 2
fi

if ! git rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null; then
  echo "check-decisions-no-silent-rewrite.sh: base ref '${BASE_REF}' does not resolve to a commit" >&2
  exit 2
fi

if ! git rev-parse --verify --quiet "${HEAD_REF}^{commit}" >/dev/null; then
  echo "check-decisions-no-silent-rewrite.sh: head ref '${HEAD_REF}' does not resolve to a commit" >&2
  exit 2
fi

DIFF_OUTPUT="$(git diff "${BASE_REF}" "${HEAD_REF}" -- docs/decisions.md)"

if [ -z "${DIFF_OUTPUT}" ]; then
  exit 0
fi

# A removed line is any diff line starting with a single '-' (not the '---'
# file-header line), whose content (stripped) is non-empty. Diff hunk headers
# ('@@ ... @@') and context/added lines are not scanned.
OFFENDERS="$(printf '%s\n' "${DIFF_OUTPUT}" | awk '
  /^--- / { next }
  /^-/ {
    line = substr($0, 2)
    gsub(/^[ \t]+|[ \t]+$/, "", line)
    if (length(line) > 0) {
      print "REMOVED: " substr($0, 2)
    }
  }
')"

if [ -n "${OFFENDERS}" ]; then
  printf '%s\n' "${OFFENDERS}"
  exit 1
fi

exit 0
