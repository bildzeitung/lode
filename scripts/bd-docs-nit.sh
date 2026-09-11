#!/usr/bin/env bash
#
# Own the docs-nits collector recipe -- resolve the single bd issue carrying the
# reserved `docs-nits` label, append a nit note to it in the mandated patch shape
# with the `NIT` prefix this script owns, push the write over refs/dolt/data, and
# (in `count` mode) report the per-collector `^NIT` note count /sweep section 2d
# reads.
#
# Extracted per lode-y86u: the same six-clause recipe -- locate the collector via
# `bd list --label docs-nits --limit 0 --json`, refuse/report unless the caller can
# proceed, `bd update --append-notes` in patch shape, own the `NIT` prefix, then
# `scripts/bd-dolt-push.sh` -- was inline, byte-for-byte-ish, in
# .claude/skills/land/SKILL.md, .claude/agents/coding.md, and
# .claude/agents/code-reviewer.md, with the read half duplicated again in
# .claude/skills/sweep/SKILL.md section 2d. Nothing gates inline shell in a fenced
# markdown block, so the four copies were already free to drift -- the same
# "logic shared by two call sites belongs in scripts/, never duplicated in
# markdown" rule docs/agents-workflow.md states, and the same lesson as
# scripts/sweep-digest-id.sh (lode-x495), the direct precedent this script follows
# for its resolve-by-label + refusal contract.
#
# Usage:
#   scripts/bd-docs-nit.sh append --source <text> --file <path> --line <n> \
#     --anchor <text> --replacement <text> [--what <text>]
#   scripts/bd-docs-nit.sh count
#
# `append` resolves the collector, refuses unless exactly one open `docs-nits`
# issue exists (same 0-vs-2+ refusal contract as sweep-digest-id.sh), composes the
# note in the mandated patch shape (file, line, anchor quoted verbatim, exact
# replacement), prefixes it `NIT` (the literal this script owns -- /sweep's
# section 2d count depends on every appended note starting with it), appends via
# `bd update --append-notes`, calls scripts/bd-dolt-push.sh, and prints the
# collector's id to stdout.
#
# `count` lists EVERY open `docs-nits` collector (not just one -- a second one
# existing is precisely what a human needs to see, per sweep/SKILL.md section
# 2d) as `<id>\t<title>\t<count>` TSV rows, one per line, where <count> is the
# number of `^NIT`-prefixed notes in that issue. Does not refuse on 0 or 2+; an
# empty result set prints nothing (not an error).
#
# Exit 0  -> success. `append`: collector id on stdout. `count`: TSV rows (or
#            nothing, if 0 open collectors) on stdout.
# Exit 1  -> `append` only: NOT exactly one open `docs-nits` collector, so the
#            caller cannot proceed. Diagnostic to stderr, nothing to stdout.
#              N == 0  no collector exists yet. The caller falls back to
#                      reporting the nit in its own hand-off instead -- it does
#                      NOT create one; a human opens the collector.
#              N >  1  do not guess which is authoritative. Report both ids and
#                      let a human consolidate (keep one, strip the label off
#                      the rest) -- the same anomaly-handling sweep-digest-id.sh
#                      uses for its own label.
# Exit 2  -> MACHINE FAULT (bad arguments, or `bd`/`jq` failed). Same "exit 2 is
#            the machine, never the content" convention as sweep-digest-id.sh.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat >&2 <<'EOF'
usage:
  bd-docs-nit.sh append --source <text> --file <path> --line <n> --anchor <text> --replacement <text> [--what <text>]
  bd-docs-nit.sh count
EOF
}

list_collectors() {
  # The one place the label query is spelled -- both subcommands read it from
  # here, so a label or --limit change cannot half-land.
  if ! bd list --label docs-nits --limit 0 --json 2>/dev/null; then
    echo "bd-docs-nit.sh: the docs-nits label query failed" >&2
    return 2
  fi
}

resolve_one_collector() {
  # Prints the single open docs-nits collector's id to stdout on success;
  # returns 1 (not exactly one) or 2 (machine fault), per the header contract.
  local rows n
  rows="$(list_collectors)" || return 2
  # `(. // [])` -- bd serializes an empty result set as `null`, not `[]`.
  if ! n="$(printf '%s' "$rows" | jq '(. // []) | length' 2>/dev/null)"; then
    echo "bd-docs-nit.sh: could not parse the docs-nits query JSON" >&2
    return 2
  fi
  if [ "$n" -ne 1 ]; then
    echo "bd-docs-nit.sh: expected exactly 1 open issue labelled docs-nits, found $n." >&2
    if [ "$n" -eq 0 ]; then
      echo "  No collector exists yet -- fall back to reporting this nit in your own" >&2
      echo "  hand-off instead. Do not create one; a human opens the collector." >&2
    else
      echo "  Duplicate collectors -- do NOT guess which is authoritative. Report the" >&2
      echo "  ids and let a human consolidate (keep one, strip the docs-nits label off" >&2
      echo "  the rest):" >&2
      printf '%s' "$rows" | jq -r '(. // []) | .[] | "    \(.id)\t\(.title)"' >&2
    fi
    return 1
  fi
  printf '%s' "$rows" | jq -r '.[0].id'
}

cmd_append() {
  local source="" file="" line="" anchor="" replacement="" what="" id
  while [ "$#" -gt 0 ]; do
    # A flag whose value is missing must be a MACHINE FAULT (exit 2), not the
    # `set -u` unbound-variable death that exits 1 -- exit 1 is reserved for
    # "not exactly one collector", and a caller distinguishing the two on the
    # exit code alone would read a typo'd invocation as "no collector exists".
    case "$1" in
      --source|--file|--line|--anchor|--replacement|--what)
        if [ "$#" -lt 2 ]; then
          echo "bd-docs-nit.sh append: $1 requires a value" >&2
          usage
          return 2
        fi
        ;;
    esac
    case "$1" in
      --source) source="$2"; shift 2 ;;
      --file) file="$2"; shift 2 ;;
      --line) line="$2"; shift 2 ;;
      --anchor) anchor="$2"; shift 2 ;;
      --replacement) replacement="$2"; shift 2 ;;
      --what) what="$2"; shift 2 ;;
      *) echo "bd-docs-nit.sh append: unknown argument: $1" >&2; usage; return 2 ;;
    esac
  done
  if [ -z "$source" ] || [ -z "$file" ] || [ -z "$line" ] || [ -z "$anchor" ] || [ -z "$replacement" ]; then
    echo "bd-docs-nit.sh append: --source, --file, --line, --anchor and --replacement are all required" >&2
    usage
    return 2
  fi

  local rc=0
  id="$(resolve_one_collector)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    return "$rc"
  fi

  # `NIT` is the literal prefix this script owns -- /sweep section 2d's `^NIT`
  # scan depends on every appended note starting with it, so it is never left
  # to the caller to type.
  local note
  note="NIT ($source): $file:$line
Anchor (verbatim): \"$anchor\"
Replacement: \"$replacement\""
  if [ -n "$what" ]; then
    note="$note
What it changes: $what"
  fi

  if ! bd update "$id" --append-notes "$note" >/dev/null; then
    echo "bd-docs-nit.sh append: \`bd update $id --append-notes\` failed" >&2
    return 2
  fi
  if ! "$SCRIPT_DIR/bd-dolt-push.sh"; then
    echo "bd-docs-nit.sh append: scripts/bd-dolt-push.sh failed" >&2
    return 2
  fi
  printf '%s\n' "$id"
}

cmd_count() {
  local rows
  rows="$(list_collectors)" || return 2
  # (?m) is load-bearing, not decoration: in jq 1.7's Oniguruma a bare ^
  # anchors at string start only, so dropping it would count at most 1 per
  # collector regardless of how many NIT notes it actually holds.
  if ! printf '%s' "$rows" \
    | jq -r '(. // []) | .[] | [.id, .title, (.notes // "" | [scan("(?m)^NIT")] | length)] | @tsv'; then
    echo "bd-docs-nit.sh: could not parse the docs-nits query JSON" >&2
    return 2
  fi
}

if [ "$#" -eq 0 ]; then
  usage
  exit 2
fi

subcmd="$1"
shift
case "$subcmd" in
  append) cmd_append "$@" ;;
  count)
    if [ "$#" -ne 0 ]; then
      echo "bd-docs-nit.sh count: takes no arguments (got: $*)" >&2
      exit 2
    fi
    cmd_count
    ;;
  *)
    echo "bd-docs-nit.sh: unknown subcommand: $subcmd" >&2
    usage
    exit 2
    ;;
esac
