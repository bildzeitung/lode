#!/usr/bin/env bash
#
# Determine the conventional-commit SemVer bump (breaking|feat|fix|none) for a
# git log range, for /release's Section 2 derivation
# (.claude/skills/release/SKILL.md#2-derive-the-proposal).
#
# Extracted per lode-ns3r: the inline snippet this replaces read each commit's
# full message via `git log RANGE --format='%B%x00'` +
# `while IFS= read -r -d '' MSG`, then took the commit's subject as
# `SUBJECT=$(printf '%s' "$MSG" | head -1)`. git inserts a newline AFTER each
# record's `%B` expansion, BEFORE the `%x00` delimiter -- so the NUL-delimited
# stream is actually "body1\n\x00body2\n\x00...", not
# "body1\x00body2\x00...". Every record from the SECOND onward is therefore
# captured WITH A LEADING NEWLINE, so `head -1` on it returns an EMPTY first
# line, and the subject regexes (^feat/^fix/^...!:) never match anything but
# the newest commit in the range. Concrete case (lode-905v): v1.1.0..trunk
# during the v1.2.0 release contained 2 feat(...) commits, neither of them the
# newest commit in the range -- the inline snippet computed BUMP=none (would
# have proposed v1.1.1 instead of the correct v1.2.0). Only a manual re-scan
# by the operator caught it before the release shipped.
#
# Fixed per the ticket's "simplest" fix option: subjects are read from a
# SEPARATE `git log RANGE --format='%s'` stream, one full line per commit --
# a commit subject is, by git's own convention, always exactly one line, so
# there is no record-splitting to get wrong and no NUL/newline-ordering
# footgun left to trip over. The BREAKING-CHANGE-in-body check still needs
# the full message body (the marker can appear anywhere in a footer, not just
# the subject), so it scans a separate `--format='%B'` stream for the literal
# marker -- but that scan only ever needs to know "did ANY commit in range
# contain this marker", never WHICH commit, so it is immune to the same
# per-record-boundary bug by construction (no per-commit split is needed at
# all for a whole-stream substring search).
#
# Usage: scripts/release-bump.sh <range>       # e.g. "v1.1.0..HEAD"
#
# Exit 0 -> prints exactly one of: breaking | feat | fix | none
# Exit 2 -> MACHINE FAULT (range doesn't resolve, git failure). Diagnostic to
#           stderr, nothing to stdout -- same "exit 2 is the machine, never
#           the content" convention as scripts/merge-precheck.sh (lode-9i2p).
#
# Precedence when several kinds of commits are present: breaking > feat > fix
# > none -- matches docs/release.md and the skill's own written precedence.
#
# Read-only: runs `git log`, never touches the working tree, never writes bd.

set -uo pipefail

gate_could_not_run() {
  echo "GATE COULD NOT RUN: $1" >&2
  shift
  for line in "$@"; do echo "$line" >&2; done
  exit 2
}

# Arg-count check first, and it must exit 2 -- never let an unset "${1:?...}"
# exit 1, which would collide with a legitimate "none" outcome being
# communicated some other way. There is no ambiguity to guard against here
# the way merge-precheck.sh has (0/1/2 are all live verdicts there); this
# script only ever prints to stdout on exit 0, so the arity check is simpler,
# but the exit code itself still must not overload a real bump value.
if [ "$#" -ne 1 ]; then
  gate_could_not_run \
    "usage: release-bump.sh <range>" \
    "Got $# argument(s), expected exactly 1 (a git log range, e.g. 'v1.1.0..HEAD')."
fi
RANGE="$1"

errfile="$(mktemp 2>/dev/null)" || gate_could_not_run \
  "could not create a temporary file (mktemp failed)" \
  "Usual causes: TMPDIR points at a nonexistent, full, or read-only filesystem."
trap 'rm -f "$errfile"' EXIT

if ! git rev-list --count "$RANGE" >/dev/null 2>"$errfile"; then
  err="$(<"$errfile")"
  lines=("range '$RANGE' does not resolve to a valid git log range."
         "Usual causes: a deleted/mistyped tag, or a malformed range expression."
         "Diagnose with: git rev-list --count $RANGE")
  if [ -n "$err" ]; then
    lines+=("git's own error output:")
    while IFS= read -r errline; do lines+=("$errline"); done <<<"$err"
  fi
  gate_could_not_run "range does not resolve" "${lines[@]}"
fi

# BREAKING-CHANGE-in-body check: a single whole-stream substring search, no
# per-commit attribution needed, so no record-splitting bug to reintroduce.
if git log "$RANGE" --format='%B' 2>/dev/null | grep -qE 'BREAKING[ -]CHANGE:'; then
  echo breaking
  exit 0
fi

BUMP="none"
while IFS= read -r SUBJECT; do
  if printf '%s' "$SUBJECT" | grep -qE '^[a-zA-Z]+(\([^)]*\))?!:'; then
    BUMP="breaking"
    break                                                  # highest priority, stop scanning
  elif printf '%s' "$SUBJECT" | grep -qE '^feat(\([^)]*\))?:' && [ "$BUMP" != "feat" ]; then
    BUMP="feat"
  elif printf '%s' "$SUBJECT" | grep -qE '^fix(\([^)]*\))?:' && [ "$BUMP" = "none" ]; then
    BUMP="fix"
  fi
done < <(git log "$RANGE" --format='%s')

echo "$BUMP"
