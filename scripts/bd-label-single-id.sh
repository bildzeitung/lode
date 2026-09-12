#!/usr/bin/env bash
#
# Resolve the single bd issue carrying a given reserved LABEL, and REFUSE
# unless there is exactly one. The sole owner of that query and of the 0/1/2
# refusal contract below, for every reserved-label singleton in this repo:
# scripts/sweep-digest-id.sh (sweep-digest) and scripts/bd-docs-nit.sh
# (docs-nits) are both thin callers, per the "logic shared by two call sites
# belongs in scripts/, never duplicated" rule docs/agents-workflow.md states
# (lode-ayfm).
#
# Usage:
#   scripts/bd-label-single-id.sh LABEL [--all] [--status VALUE] \
#     [--zero-advisory TEXT] [--dup-advisory TEXT]
#
# LABEL is the reserved bd label to query (e.g. sweep-digest, docs-nits).
# --all also counts CLOSED issues (sweep-digest-id.sh's own need: a closed
#   duplicate is still a duplicate a human must resolve). Omit it for an
#   open-only query (bd-docs-nit.sh's own need).
# --status VALUE is OPT-IN: passed straight through as `bd list --status
#   VALUE` only when a caller supplies it. sweep-digest-id.sh never passes it
#   and is unaffected (lode-z1n5): docs-nits' own resolve needs open-only
#   (an in_progress collector a builder has already claimed must be invisible
#   to it), which this flag lets a caller ask for without touching --all.
# --zero-advisory / --dup-advisory carry the CALLER's own per-label wording,
#   printed verbatim (may be multi-line) on the N==0 / N>1 refusal path
#   respectively, right after this script's own diagnostic line -- neither is
#   required, and the base diagnostic plus (on N>1) the list of matching
#   ids/titles always print regardless. Deliberately NOT owned here: the two
#   callers' advisory paragraphs differ in wording and audience, so that
#   wording stays with each caller (lode-ayfm).
#
# Exit 0 -> prints the single matching issue's id to stdout. Exactly one
#           match.
# Exit 1 -> NOT exactly one match. Diagnostic (+ any advisory, + the id list
#           on a duplicate) to stderr, nothing to stdout. A legitimate state,
#           not a fault -- the caller must not guess which match (if any) is
#           authoritative and must not proceed past this refusal.
# Exit 2 -> MACHINE FAULT (bad arguments, or bd/jq failed). Same "exit 2 is
#           the machine, never the content" convention as
#           sweep-digest-id.sh / bd-docs-nit.sh / gate-lib.sh.
#
# Read-only: only ever calls `bd list`, never a bd write.

set -euo pipefail

usage() {
  echo "usage: bd-label-single-id.sh LABEL [--all] [--status VALUE] [--zero-advisory TEXT] [--dup-advisory TEXT]" >&2
}

if [ "$#" -eq 0 ]; then
  usage
  exit 2
fi

label="$1"
shift

if [ -z "$label" ]; then
  echo "bd-label-single-id.sh: LABEL is required" >&2
  usage
  exit 2
fi

all=0
status=""
zero_advisory=""
dup_advisory=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --all)
      all=1
      shift
      ;;
    --status | --zero-advisory | --dup-advisory)
      # A flag whose value is missing is a MACHINE FAULT (exit 2), not the
      # `set -u` unbound-variable death that would otherwise exit 1 -- exit 1
      # is reserved for "not exactly one match", so a typo'd invocation must
      # not read to a caller as a legitimate empty/duplicate state.
      if [ "$#" -lt 2 ]; then
        echo "bd-label-single-id.sh: $1 requires a value" >&2
        usage
        exit 2
      fi
      case "$1" in
        --status) status="$2" ;;
        --zero-advisory) zero_advisory="$2" ;;
        --dup-advisory) dup_advisory="$2" ;;
      esac
      shift 2
      ;;
    *)
      echo "bd-label-single-id.sh: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

# `--limit 0` for the same reason every `bd list` in this repo passes it: no
# truncation. The invocation is spelled literally up to the optional `--all`
# suffix, rather than built wholesale from an array: tests/test_bd_list_limit_gate.py
# only sees a literal `bd ... list` carrying `--limit` in the same command
# segment, and this is now the ONLY resolve-by-label query in the tree -- an
# array-built call would leave that flag ungated everywhere (sabotage-checked:
# the array form stayed green with `--limit 0` deleted).
all_args=()
[ "$all" -eq 1 ] && all_args+=(--all)
[ -n "$status" ] && all_args+=(--status "$status")

if ! rows="$(bd list --label "$label" --limit 0 --json "${all_args[@]}" 2>/dev/null)"; then
  echo "bd-label-single-id.sh: \`bd list --label $label\` failed" >&2
  exit 2
fi

# `(. // [])` because bd serializes an empty result set as `null`, not `[]`.
if ! n="$(printf '%s' "$rows" | jq '(. // []) | length' 2>/dev/null)"; then
  echo "bd-label-single-id.sh: could not parse \`bd list\` JSON" >&2
  exit 2
fi

if [ "$n" -ne 1 ]; then
  echo "bd-label-single-id.sh: expected exactly 1 issue labelled $label, found $n." >&2
  if [ "$n" -eq 0 ]; then
    [ -n "$zero_advisory" ] && printf '%s\n' "$zero_advisory" >&2
  else
    [ -n "$dup_advisory" ] && printf '%s\n' "$dup_advisory" >&2
    printf '%s' "$rows" | jq -r '(. // []) | .[] | "    \(.id)\t\(.title)"' >&2
  fi
  exit 1
fi

printf '%s' "$rows" | jq -r '.[0].id'
