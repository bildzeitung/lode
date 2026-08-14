#!/usr/bin/env bash
#
# Merge a whole accepted set of `land/<id>` branches onto the current
# checkout, one at a time, classifying each as LANDED / CONFLICT / HELD.
# (lode-s9xe.4)
#
# WHY THIS IS A SCRIPT. `/land`'s Section 3 has TWO merge loops with the
# identical shape -- the first-pass batch merge, and the isolation-replay
# loop that runs after a `git reset --hard origin/trunk` -- fenced separately
# in .claude/skills/land/SKILL.md with a comment asking a human to "keep the
# two loops the same shape". That is an unenforced sync invariant over
# destructive code: nothing stops the two copies drifting the next time
# either one is edited, and nothing tests either one. This script is the one
# copy both call sites drive.
#
# Two traps this script is careful to get right, both learned the hard way in
# /land's own history:
#
#   * `if CMD; then rc=0; else rc=$?; fi`, NOT `if ! CMD; then rc=$?; fi` --
#     inside the negated form's `then` arm, `$?` is the status of the
#     NEGATION, which is always 0. A machine-fault exit 2 from
#     land-merge-one.sh would read as a clean merge and the loop would carry
#     on as though the branch had landed.
#   * On a real conflict the branch that just failed to merge, AND every
#     branch stacked on it, must leave the accepted set -- written back to
#     the FILE, not just to this shell's view of it, because a machine-fault
#     stop and restart (or the isolation-replay loop, a separate Bash
#     invocation) re-reads the file from disk. scripts/drop-from-accepted.sh
#     (lode-s9xe.3) already owns that reduction; this script calls it rather
#     than re-deriving the drop.
#
# This script runs NO gates (no nox) and makes NO tracker writes (no bd, no
# git push) -- it classifies and reports; the caller (an agent executing
# /land's SKILL.md) re-gates the combined result and acts on the report.
#
# Usage:
#   scripts/land-merge-batch.sh --accepted <file> --msg-dir <dir> \
#       --conflicts-dir <dir> [--graph <file>] [--token <token>] \
#       [--landed <file>]
#
#   --accepted       the ordered accepted-set file (base before dependent --
#                     see /land's Section 3a). Read once, then rewritten in
#                     place by drop-from-accepted.sh every time a branch in it
#                     conflicts. Missing is a machine fault; present-but-empty
#                     is a legitimate "nothing left to merge" outcome and
#                     iterates zero times (lode-0jan's rule).
#   --msg-dir        directory of precomputed merge messages, one file per id
#                     at <msg-dir>/<id> -- forwarded verbatim to
#                     land-merge-one.sh.
#   --conflicts-dir  directory to write <conflicts-dir>/<id> into on a real
#                     conflict -- the conflicting paths land-merge-one.sh
#                     printed, persisted for a later, separate Bash
#                     invocation to read back for the needs-rebase kick-back
#                     note (lode-rfon's reasoning: this loop's own shell
#                     variables do not survive past this script's exit).
#   --graph          scripts/stacked-graph.sh output, forwarded to
#                     drop-from-accepted.sh so a conflicting base's
#                     dependents are held too. Omit only when this pass has
#                     no stacked branches at all -- omitting it when there ARE
#                     stacks silently skips the dependent drop.
#   --token          this pass's land-lock token (lode-q9pm), forwarded
#                     verbatim to land-merge-one.sh as its own [own-token]
#                     argument. Omit to fall through to that script's blind
#                     heartbeat.
#   --landed         optional: append each LANDED id to this file, one per
#                     line, as it merges -- the same file /land's Section 4
#                     reads back. Omit to skip this bookkeeping (a caller that
#                     wants it can instead grep this script's own LANDED
#                     lines from stdout).
#
# Output (stdout), one line per id processed, in accepted-set order:
#   LANDED\t<id>      merged cleanly onto the current checkout.
#   CONFLICT\t<id>    a real textual conflict against a branch already merged
#                     this pass. Left the accepted set (dropped from the
#                     --accepted file); the merge was already aborted by
#                     land-merge-one.sh before this line is printed.
#   HELD\t<id>        NOT processed -- removed from the accepted set as a
#                     dependent of a branch this same call classified
#                     CONFLICT (or an earlier HELD's own dependent, via
#                     drop-from-accepted.sh's transitive closure). Not
#                     conflicted and not rejected; it simply has no
#                     foundation left this pass.
#
# Exit codes: 0 = the loop ran to completion (LANDED/CONFLICT/HELD are all
# non-fault outcomes -- a batch that conflicts or holds every id still exits
# 0). 2 = machine fault: bad usage, a required file missing, or any called
# script (land-merge-one.sh, drop-from-accepted.sh) itself exited 2. Per
# lode-9i2p's rule, this is never read as a branch verdict, and processing
# stops immediately -- an id whose fate is unknown must not be silently
# carried forward or silently dropped. There is no exit 1: a batch of
# multiple ids has no single verdict to report through the exit code: read
# stdout.
set -uo pipefail   # deliberately NOT -e -- see land-merge-one.sh's identical
                   # note: every branch below inspects an exit code by hand.

# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" --no-advisory; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

SCRIPT_DIR="$(dirname "$0")"

ACCEPTED=""
MSG_DIR=""
CONFLICTS_DIR=""
GRAPH=""
TOKEN=""
LANDED_FILE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --accepted)       shift; [ "$#" -gt 0 ] || gate_could_not_run "--accepted needs a value"; ACCEPTED="$1" ;;
    --msg-dir)        shift; [ "$#" -gt 0 ] || gate_could_not_run "--msg-dir needs a value"; MSG_DIR="$1" ;;
    --conflicts-dir)  shift; [ "$#" -gt 0 ] || gate_could_not_run "--conflicts-dir needs a value"; CONFLICTS_DIR="$1" ;;
    --graph)          shift; [ "$#" -gt 0 ] || gate_could_not_run "--graph needs a value"; GRAPH="$1" ;;
    --token)          shift; [ "$#" -gt 0 ] || gate_could_not_run "--token needs a value"; TOKEN="$1" ;;
    --landed)         shift; [ "$#" -gt 0 ] || gate_could_not_run "--landed needs a value"; LANDED_FILE="$1" ;;
    *)                gate_could_not_run "unknown argument '$1'" \
                        "usage: land-merge-batch.sh --accepted <file> --msg-dir <dir>" \
                        "  --conflicts-dir <dir> [--graph <file>] [--token <token>] [--landed <file>]" ;;
  esac
  shift
done

[ -n "$ACCEPTED" ]      || gate_could_not_run "--accepted is required"
[ -n "$MSG_DIR" ]       || gate_could_not_run "--msg-dir is required"
[ -n "$CONFLICTS_DIR" ] || gate_could_not_run "--conflicts-dir is required"
[ -d "$MSG_DIR" ]       || gate_could_not_run "--msg-dir '$MSG_DIR' does not exist"
[ -d "$CONFLICTS_DIR" ] || gate_could_not_run "--conflicts-dir '$CONFLICTS_DIR' does not exist"
if [ -n "$GRAPH" ] && [ ! -f "$GRAPH" ]; then
  gate_could_not_run "graph file '$GRAPH' does not exist" \
    "Pass the file scripts/stacked-graph.sh wrote, or omit --graph only if this pass has no stacks."
fi

# Missing -> fatal (the caller's own precompute step did not run at all);
# present-but-empty -> OK, iterates zero times (lode-0jan's rule: an
# all-bounced/all-kicked-back pass is a legitimate outcome, not a fault).
ACCEPTED_IDS=$("$SCRIPT_DIR/land-state-load.sh" "$ACCEPTED" -- \
  "land-merge-batch.sh: the accepted-set precompute did not run.") || exit 2

for id in $ACCEPTED_IDS; do
  # A branch already HELD/CONFLICT-dropped by an earlier iteration this same
  # call may still appear in $ACCEPTED_IDS (that variable was captured
  # before the loop started) -- drop-from-accepted.sh already removed it
  # from the FILE, so re-check membership rather than trust the stale list.
  if ! grep -qxF "$id" "$ACCEPTED"; then
    continue
  fi

  # Same idiom as land-merge-one.sh's own callers, for the same reason: `if !
  # CMD; then rc=$?` would capture the negation's status (always 0 in that
  # arm), silently reading a machine-fault 2 as a clean merge.
  if CONFLICTS=$("$SCRIPT_DIR/land-merge-one.sh" "$id" "$MSG_DIR" "$TOKEN"); then
    rc=0
  else
    rc=$?
  fi

  case "$rc" in
    0)
      printf 'LANDED\t%s\n' "$id"
      [ -z "$LANDED_FILE" ] || printf '%s\n' "$id" >> "$LANDED_FILE"
      ;;
    2)
      # MACHINE FAULT -- land-merge-one.sh already printed its own diagnostic
      # to this call's stderr. Never a branch verdict (lode-9i2p): stop
      # processing rather than guess at the fate of the ids not yet reached.
      exit 2
      ;;
    *)
      # rc=1: a real textual conflict against a branch already merged this
      # pass. Persist the conflicting paths now, while this loop actually
      # holds them -- a later, separate Bash invocation writing the
      # needs-rebase kick-back note cannot see this loop's $CONFLICTS once
      # this script exits (lode-rfon).
      printf '%s\n' "$CONFLICTS" > "$CONFLICTS_DIR/$id"
      printf 'CONFLICT\t%s\n' "$id"

      # 3a's invariant: this branch just LEFT the merge set, so drop it AND
      # every branch stacked on it -- drop-from-accepted.sh rewrites
      # --accepted in place and reports each dependent it held. A drop-side
      # machine fault (missing --accepted, an unreadable file) is this
      # script's own fault too: propagate it as exit 2 rather than silently
      # leaving a conflicted branch in the accepted set.
      DROP_OUT=$("$SCRIPT_DIR/drop-from-accepted.sh" "$id" --accepted "$ACCEPTED" \
        ${GRAPH:+--graph "$GRAPH"}) || exit 2
      printf '%s\n' "$DROP_OUT" | while IFS=$'\t' read -r verb held_id; do
        [ "$verb" = "HELD" ] || continue
        printf 'HELD\t%s\n' "$held_id"
      done
      ;;
  esac
done

exit 0
