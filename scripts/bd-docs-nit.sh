#!/usr/bin/env bash
#
# Own the docs-nits collector recipe -- resolve the bd issue(s) carrying the
# reserved `docs-nits` label, append a nit note in the mandated patch shape
# with the `NIT` prefix this script owns, push the write over refs/dolt/data,
# and (in `count` mode) report the per-collector `^NIT` note count /sweep
# section 2d reads.
#
# Extracted per lode-y86u; lifecycle policy (create-on-zero, converge-on-dup)
# added per lode-z1n5 -- the same six-clause recipe was inline, byte-for-
# byte-ish, in .claude/skills/land/SKILL.md, .claude/agents/coding.md, and
# .claude/agents/code-reviewer.md, with the read half duplicated again in
# .claude/skills/sweep/SKILL.md section 2d. Nothing gates inline shell in a
# fenced markdown block, so the four copies were already free to drift -- the
# same "logic shared by two call sites belongs in scripts/, never duplicated"
# rule docs/agents-workflow.md states, and the same lesson as
# scripts/sweep-digest-id.sh (lode-x495), the direct precedent this script
# follows for its resolve-by-label + refusal contract.
#
# Usage:
#   scripts/bd-docs-nit.sh append --source <text> --file <path> --line <n> \
#     --anchor <text> --replacement <text> [--what <text>]
#   scripts/bd-docs-nit.sh count
#   scripts/bd-docs-nit.sh has-open
#
# `append` resolves the OPEN docs-nits collector(s) (never in_progress -- an
# in_progress collector is one a builder has already claimed, per lode-z1n5
# part 3, and must be invisible here so nits don't keep landing on the batch
# being fixed):
#   N == 0  creates one via scripts/bd-docs-nit-create.sh (lode-z1n5 part 1 --
#           reverses the old "a human opens it" rule) and appends to it.
#   N == 1  appends to it.
#   N >  1, ALL carrying the STANDARD title  converges (lode-z1n5 part 1a):
#           the lexically smallest id survives; every `^NIT` note on each
#           loser migrates (appended) to the survivor; each loser is closed
#           naming the survivor; the nit is then appended to the survivor.
#           Covers the same-machine create race, the cross-machine Dolt-sync
#           race, and the /land reopen race (part 4) uniformly.
#   N >  1, ANY carrying a NON-standard title  refuses (exit 1) -- a human
#           opened one on purpose; do not guess which is authoritative.
# composes the note in the mandated patch shape (file, line, anchor quoted
# verbatim, exact replacement), prefixes it `NIT` (the literal this script
# owns -- /sweep's section 2d count depends on every appended note starting
# with it), appends via `bd update --append-notes`, calls
# scripts/bd-dolt-push.sh, and prints the collector's id to stdout.
#
# `count` lists EVERY non-closed (open + in_progress) `docs-nits` collector --
# an in_progress one is precisely what /sweep section 2d wants to see, even
# though `append` must not touch it -- as `<id>\t<title>\t<count>` TSV rows,
# one per line, where <count> is the number of `^NIT`-prefixed notes in that
# issue. Does not refuse on 0 or 2+; an empty result set prints nothing.
#
# `has-open` is a plain read-only predicate: exit 0 if at least one OPEN
# docs-nits collector exists, exit 1 if none. /land's reopen-on-close backstop
# (lode-z1n5 part 4) calls this instead of inlining its own `bd list --label
# docs-nits` query.
#
# Exit 0  -> success. `append`: collector id on stdout. `count`: TSV rows (or
#            nothing, if 0 collectors) on stdout. `has-open`: at least one open
#            collector exists (nothing on stdout).
# Exit 1  -> `append`: 2+ open collectors exist and at least one carries a
#            NON-standard title, so convergence does not apply -- do not guess
#            which is authoritative. Diagnostic + the id/title list to stderr,
#            nothing to stdout. A human opened one on purpose; report both ids
#            and let a human consolidate (keep one, strip the label off the
#            rest). `has-open`: no open collector exists -- a legitimate state,
#            not a fault.
# Exit 2  -> MACHINE FAULT (bad arguments, `bd`/`jq` failed, the create or the
#            migrate-and-close convergence step failed). Same "exit 2 is the
#            machine, never the content" convention as sweep-digest-id.sh.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# The one place the collector's label and standard title are spelled for the
# APPEND/CONVERGE path (scripts/bd-docs-nit-create.sh spells its own copy of
# the title for the CREATE path -- see that script's header for why there is
# no single shared constant across two independent bash scripts).
readonly DOCS_NITS_LABEL="docs-nits"
readonly STANDARD_TITLE="Docs wording nits: batch fix in the next docs pass"

usage() {
  cat >&2 <<'EOF'
usage:
  bd-docs-nit.sh append --source <text> --file <path> --line <n> --anchor <text> --replacement <text> [--what <text>]
  bd-docs-nit.sh count
EOF
}

list_collectors() {
  # `cmd_count`'s query: every non-closed row (open + in_progress), unlike
  # `resolve_open_collector`'s open-only query below.
  if ! bd list --label "$DOCS_NITS_LABEL" --limit 0 --json 2>/dev/null; then
    echo "bd-docs-nit.sh: the docs-nits label query failed" >&2
    return 2
  fi
}

resolve_open_collector() {
  # Prints the collector id to append to on stdout; returns 1 (2+ with a
  # non-standard title, human must consolidate) or 2 (machine fault), per the
  # header contract. Never sees an in_progress collector -- `--status open`
  # via scripts/bd-label-single-id.sh's opt-in flag.
  local rows n all_standard survivor losers loser
  # scripts/bd-label-single-id.sh's --status opt-in flag (lode-z1n5) makes its
  # underlying query the same one this needs, but its generic 0-vs-2+ refusal
  # contract collapses N==0 and N>1 into a single "not exactly one" exit 1 --
  # this needs to tell them apart (create vs. converge-or-refuse), so it
  # queries directly instead of going through that helper.
  if ! rows="$(bd list --label "$DOCS_NITS_LABEL" --status open --limit 0 --json 2>/dev/null)"; then
    echo "bd-docs-nit.sh: the docs-nits open-collector query failed" >&2
    return 2
  fi
  if ! n="$(printf '%s' "$rows" | jq '(. // []) | length' 2>/dev/null)"; then
    echo "bd-docs-nit.sh: could not parse the docs-nits open-collector query JSON" >&2
    return 2
  fi

  if [ "$n" -eq 0 ]; then
    if ! "$SCRIPT_DIR/bd-docs-nit-create.sh"; then
      echo "bd-docs-nit.sh: no open docs-nits collector, and creating one failed" >&2
      return 2
    fi
    return 0
  fi

  if [ "$n" -eq 1 ]; then
    printf '%s' "$rows" | jq -r '.[0].id'
    return 0
  fi

  # n > 1: converge only if every open collector carries the standard title.
  if ! all_standard="$(printf '%s' "$rows" | jq -r --arg t "$STANDARD_TITLE" \
    '(. // []) | all(.title == $t)' 2>/dev/null)"; then
    echo "bd-docs-nit.sh: could not evaluate open docs-nits collector titles" >&2
    return 2
  fi
  if [ "$all_standard" != "true" ]; then
    echo "bd-docs-nit.sh: $n open docs-nits collectors, at least one with a non-standard title -- do NOT guess which is authoritative. A human opened one on purpose; report the ids and let a human consolidate (keep one, strip the docs-nits label off the rest):" >&2
    printf '%s' "$rows" | jq -r '(. // []) | .[] | "    \(.id)\t\(.title)"' >&2
    return 1
  fi

  if ! survivor="$(printf '%s' "$rows" | jq -r '(. // []) | map(.id) | sort | .[0]')"; then
    echo "bd-docs-nit.sh: could not determine the convergence survivor" >&2
    return 2
  fi
  if ! losers="$(printf '%s' "$rows" | jq -r --arg s "$survivor" '(. // []) | map(select(.id != $s)) | .[].id')"; then
    echo "bd-docs-nit.sh: could not determine convergence losers" >&2
    return 2
  fi

  while IFS= read -r loser; do
    [ -z "$loser" ] && continue
    local loser_json loser_notes
    if ! loser_json="$(bd show "$loser" --json 2>/dev/null)"; then
      echo "bd-docs-nit.sh: could not read duplicate collector $loser to migrate its notes" >&2
      return 2
    fi
    loser_notes="$(printf '%s' "$loser_json" | jq -r '.[0].notes // ""')"
    if [ -n "$loser_notes" ]; then
      if ! bd update "$survivor" --append-notes "$loser_notes" >/dev/null; then
        echo "bd-docs-nit.sh: could not migrate $loser's notes onto $survivor" >&2
        return 2
      fi
    fi
    if ! bd close "$loser" --reason "Converged into $survivor (duplicate docs-nits collector, lode-z1n5)" >/dev/null; then
      echo "bd-docs-nit.sh: could not close duplicate collector $loser after migrating its notes" >&2
      return 2
    fi
  done <<<"$losers"

  printf '%s\n' "$survivor"
}

cmd_append() {
  local source="" file="" line="" anchor="" replacement="" what="" id
  while [ "$#" -gt 0 ]; do
    # A flag whose value is missing must be a MACHINE FAULT (exit 2), not the
    # `set -u` unbound-variable death that exits 1 -- exit 1 is reserved for
    # "2+ collectors, human must consolidate", and a caller distinguishing the
    # two on the exit code alone would misread a typo'd invocation.
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
  id="$(resolve_open_collector)" || rc=$?
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

cmd_has_open() {
  # Read-only: exit 0 if at least one OPEN docs-nits collector exists, exit 1
  # if none (never conflated with append's 2+ convergence/refusal cases --
  # this predicate only cares about zero-vs-some). /land's reopen-on-close
  # backstop (lode-z1n5 part 4) calls this instead of inlining its own `bd
  # list --label docs-nits` query, which the append-recipe delegation tests
  # (tests/test_bd_docs_nit.py) refuse to let any call site duplicate.
  local rows n
  if ! rows="$(bd list --label "$DOCS_NITS_LABEL" --status open --limit 0 --json 2>/dev/null)"; then
    echo "bd-docs-nit.sh has-open: the docs-nits open-collector query failed" >&2
    return 2
  fi
  if ! n="$(printf '%s' "$rows" | jq '(. // []) | length' 2>/dev/null)"; then
    echo "bd-docs-nit.sh has-open: could not parse the docs-nits open-collector query JSON" >&2
    return 2
  fi
  [ "$n" -gt 0 ]
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
  has-open)
    if [ "$#" -ne 0 ]; then
      echo "bd-docs-nit.sh has-open: takes no arguments (got: $*)" >&2
      exit 2
    fi
    cmd_has_open
    ;;
  *)
    echo "bd-docs-nit.sh: unknown subcommand: $subcmd" >&2
    usage
    exit 2
    ;;
esac
