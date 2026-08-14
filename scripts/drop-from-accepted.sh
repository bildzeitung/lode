#!/usr/bin/env bash
#
# Remove a branch AND every branch stacked on it from /land's accepted set.
#
# THE INVARIANT THIS ENFORCES (Section 3a): "a base that leaves the merge set
# takes its dependents with it." Ordering the set up front is not sufficient,
# because a base can drop OUT later -- kicked back on a real merge conflict, or
# bounced during isolation. If it does and its dependents stay, the loop merges
# a dependent whose base is no longer landing, putting the departed base's
# un-landed, just-rejected content onto trunk under the DEPENDENT's ticket
# name.
#
# WHY A SCRIPT. This was a shell recipe written inside a COMMENT in
# .claude/skills/land/SKILL.md -- a `grep -vxF`/`mv` pair the agent was
# expected to implement inline, plus a transitive dependent lookup it had to
# do by hand, at the exact moment a conflict had just fired. Detection was
# made executable (scripts/stacked-graph.sh, lode-s9xe.2); leaving the ACTION
# as prose is the half of the fix that actually decides what reaches trunk.
#
# THE REDUCTION MUST BE WRITTEN TO THE FILE, not just to the caller's shell
# variable: the isolation-replay loop re-reads the file, and would otherwise
# re-merge a branch this pass already kicked back.
#
# Usage:
#   scripts/drop-from-accepted.sh <id> --accepted <file> [--graph <file>]
#
#   <id>         the branch leaving the merge set
#   --accepted   /land's ordered accepted-set file, rewritten in place
#   --graph      scripts/stacked-graph.sh output. Omit only when the pass has
#                no stacked branches at all; omitting it when there ARE stacks
#                silently skips the dependent drop, which is the whole point.
#
# Output (stdout), one per line, in the order they were removed:
#   DROPPED <id>      the branch named on the command line
#   HELD    <id>      a dependent removed with it -- the caller owes each of
#                     these a HELD note; they are not conflicted and not
#                     rejected, they simply have no foundation this pass.
#
# Exit codes: 0 = reduction applied (or the id was already absent, which is
# idempotent and fine), 2 = machine fault. There is no exit 1: removing a branch
# from a set is not a verdict about it.
set -u

ID=""
ACCEPTED=""
GRAPH=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --accepted) shift; [ "$#" -gt 0 ] || { echo "GATE COULD NOT RUN: --accepted needs a value" >&2; exit 2; }; ACCEPTED="$1" ;;
    --graph)    shift; [ "$#" -gt 0 ] || { echo "GATE COULD NOT RUN: --graph needs a value" >&2; exit 2; }; GRAPH="$1" ;;
    -*)         echo "GATE COULD NOT RUN: unknown argument '$1'" >&2; exit 2 ;;
    *)          [ -z "$ID" ] || { echo "GATE COULD NOT RUN: more than one id given" >&2; exit 2; }; ID="$1" ;;
  esac
  shift
done

[ -n "$ID" ]       || { echo "GATE COULD NOT RUN: no id given" >&2; exit 2; }
[ -n "$ACCEPTED" ] || { echo "GATE COULD NOT RUN: --accepted is required" >&2; exit 2; }
# A MISSING accepted file is a machine fault, never an empty set: continuing
# would report "nothing to drop" for a reduction that never happened.
[ -f "$ACCEPTED" ] || { echo "GATE COULD NOT RUN: accepted file '$ACCEPTED' does not exist" >&2; exit 2; }
if [ -n "$GRAPH" ] && [ ! -f "$GRAPH" ]; then
  echo "GATE COULD NOT RUN: graph file '$GRAPH' does not exist" >&2
  echo "Pass the file scripts/stacked-graph.sh wrote, or omit --graph only if this pass has no stacks." >&2
  exit 2
fi

# Dependents of $ID = every EDGE whose BASE is $ID. stacked-graph.sh emits the
# transitive closure, so one pass catches a dependent-of-a-dependent too; no
# closure walk is needed (and hand-rolling one here is how this goes wrong).
DEPENDENTS=""
if [ -n "$GRAPH" ]; then
  DEPENDENTS="$(awk -F'\t' -v base="$ID" '$1 == "EDGE" && $3 == base { print $2 }' "$GRAPH" | sort -u)"
fi

TMP="$ACCEPTED.tmp"
drop_one() {
  # `|| true` because grep exits 1 when it filters out the LAST remaining line,
  # and an empty accepted set is a legitimate outcome here -- an all-kicked-back
  # pass. Without it, the reduction would abort exactly when it mattered most.
  grep -vxF "$1" "$ACCEPTED" > "$TMP" || true
  mv "$TMP" "$ACCEPTED"
}

drop_one "$ID"
printf 'DROPPED\t%s\n' "$ID"

for dep in $DEPENDENTS; do
  [ -n "$dep" ] || continue
  drop_one "$dep"
  printf 'HELD\t%s\n' "$dep"
done

exit 0
