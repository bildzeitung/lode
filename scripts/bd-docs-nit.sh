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
#   scripts/bd-docs-nit.sh ensure-open [<closed-id> ...]
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
# `ensure-open` is /land's reopen-on-close backstop (lode-z1n5 part 4) in one
# call: given the ids a pass just closed, it opens a successor collector --
# via scripts/bd-docs-nit-create.sh, never an inline `bd create` -- if and
# only if one of those ids carried the docs-nits label and no OPEN collector
# is left. No ids, no docs-nits id among them, or a collector already open:
# no-op. It is deliberately the WHOLE backstop rather than a bare predicate,
# so the branch lives under pytest instead of in a markdown bash block.
#
# Exit 0  -> success. `append`: collector id on stdout. `count`: TSV rows (or
#            nothing, if 0 collectors) on stdout. `ensure-open`: an open
#            collector exists now, whether or not this call created it.
# Exit 1  -> `append`: 2+ open collectors exist and at least one carries a
#            NON-standard title, so convergence does not apply -- do not guess
#            which is authoritative. Diagnostic + the id/title list to stderr,
#            nothing to stdout. A human opened one on purpose; report both ids
#            and let a human consolidate (keep one, strip the label off the
#            rest).
# Exit 2  -> MACHINE FAULT (bad arguments, `bd`/`jq` failed, the create or the
#            migrate-and-close convergence step failed). Same "exit 2 is the
#            machine, never the content" convention as sweep-digest-id.sh.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/docs-nits-constants.sh
. "$SCRIPT_DIR/docs-nits-constants.sh"

usage() {
  cat >&2 <<'EOF'
usage:
  bd-docs-nit.sh append --source <text> --file <path> --line <n> --anchor <text> --replacement <text> [--what <text>]
  bd-docs-nit.sh count
  bd-docs-nit.sh ensure-open [<closed-id> ...]
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

open_collector_rows() {
  # The OPEN-only query both `append`'s resolve and `ensure-open` run (never
  # in_progress -- an in_progress collector is one a builder has already
  # claimed, per lode-z1n5 part 3). Prints the raw JSON rows; returns 2 on a
  # machine fault, having already diagnosed it.
  #
  # scripts/bd-label-single-id.sh's --status opt-in flag (lode-z1n5) makes its
  # underlying query the same one this needs, but its generic 0-vs-2+ refusal
  # contract collapses N==0 and N>1 into a single "not exactly one" exit 1 --
  # the callers here need to tell them apart (create vs. converge-or-refuse),
  # so this queries directly instead of going through that helper.
  local rows
  if ! rows="$(bd list --label "$DOCS_NITS_LABEL" --status open --limit 0 --json 2>/dev/null)"; then
    echo "bd-docs-nit.sh: the docs-nits open-collector query failed" >&2
    return 2
  fi
  if ! printf '%s' "$rows" | jq -e '(. // []) | length >= 0' >/dev/null 2>&1; then
    echo "bd-docs-nit.sh: could not parse the docs-nits open-collector query JSON" >&2
    return 2
  fi
  printf '%s' "$rows"
}

resolve_open_collector() {
  # Prints the collector id to append to on stdout; returns 1 (2+ with a
  # non-standard title, human must consolidate) or 2 (machine fault), per the
  # header contract. Never sees an in_progress collector -- `open_collector_rows`
  # queries `--status open`.
  local rows n all_standard survivor losers loser
  rows="$(open_collector_rows)" || return 2
  n="$(printf '%s' "$rows" | jq '(. // []) | length')"

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
  # Same reason as the create script's own push call: stdout carries the
  # collector id a caller may capture, so the push writes to stderr only.
  if ! "$SCRIPT_DIR/bd-dolt-push.sh" >&2; then
    echo "bd-docs-nit.sh append: scripts/bd-dolt-push.sh failed" >&2
    return 2
  fi
  printf '%s\n' "$id"
}

cmd_ensure_open() {
  # /land's reopen-on-close backstop (lode-z1n5 part 4), whole: given the ids
  # a pass just closed, open the successor collector -- via
  # scripts/bd-docs-nit-create.sh, never an inline `bd create` -- if and only
  # if one of those ids carried the docs-nits label and no OPEN collector is
  # left. Lives here rather than as a loop in .claude/skills/land/SKILL.md
  # because nothing gates inline shell in a fenced markdown block, which is
  # the reason this script exists at all.
  #
  # No ids (an empty $LANDED -- a legitimate pass that closed nothing) is a
  # no-op, exit 0. A machine fault is exit 2, never a silent create: reading a
  # broken `bd` as "none open" would mint a duplicate collector on every
  # failing pass -- the state convergence exists to clean up, not to generate.
  local id ticket rows closed_a_collector=0
  for id in "$@"; do
    if ! ticket="$(bd show "$id" --json 2>/dev/null)"; then
      echo "bd-docs-nit.sh ensure-open: could not read $id" >&2
      return 2
    fi
    if printf '%s' "$ticket" \
      | jq -e --arg l "$DOCS_NITS_LABEL" '(.[0].labels // []) | any(. == $l)' \
        >/dev/null 2>&1; then
      closed_a_collector=1
      break
    fi
  done
  [ "$closed_a_collector" -eq 1 ] || return 0

  rows="$(open_collector_rows)" || return 2
  if [ "$(printf '%s' "$rows" | jq '(. // []) | length')" -gt 0 ]; then
    return 0
  fi
  if ! "$SCRIPT_DIR/bd-docs-nit-create.sh"; then
    echo "bd-docs-nit.sh ensure-open: no open docs-nits collector, and creating one failed" >&2
    return 2
  fi
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
  ensure-open) cmd_ensure_open "$@" ;;
  *)
    echo "bd-docs-nit.sh: unknown subcommand: $subcmd" >&2
    usage
    exit 2
    ;;
esac
