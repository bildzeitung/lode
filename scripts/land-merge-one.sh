#!/usr/bin/env bash
#
# Merge a single accepted `land/<id>` branch onto the current checkout with its
# pre-computed commit message, retrying once past a re-staged
# `.beads/issues.jsonl` (a passive export -- import.auto: false, lode-6ra --
# never real work). Extracted per lode-sfnb: `/land`'s Section 3 merge loop
# used to define this as an inline bash FUNCTION (`merge_one()`) and read a
# bash ASSOCIATIVE ARRAY (`MSG`) populated by a separate, earlier fenced code
# block in .claude/skills/land/SKILL.md. Those are two different Bash tool
# invocations -- the harness does not carry shell state (variables, arrays, or
# function definitions) between them -- so by the time the merge loop ran,
# `MSG` was empty and `merge_one` may not even exist, and the failure was
# SILENT (an empty-message merge, or an unrelated-looking error with no
# output). OBSERVED landing the 2026-07-26 lode-ns3r/lode-1q2i/lode-sys4 pass.
# A script on disk has no such problem: it is available identically to every
# Bash invocation that calls it, with no bash state to redeclare.
#
# Usage: scripts/land-merge-one.sh <id> <land-msg-dir>
#
#   <id>            -- the bd ticket id whose `origin/land/<id>` branch is
#                      about to be merged into the current checkout (trunk).
#   <land-msg-dir>  -- a directory containing one file per id, named exactly
#                      `<land-msg-dir>/<id>`, whose full content (verbatim) is
#                      the merge commit message. Written once, ahead of any
#                      merge, by /land's Section 3a precompute step -- this
#                      script never calls `bd` itself, so the "one bd-show
#                      pass instead of N subprocess calls per merge" property
#                      lode-bns3 established stays intact.
#
# Exit codes -- same 0/1/2 convention as scripts/merge-precheck.sh and
# scripts/validate-mermaid.sh (lode-9i2p's rule: exit 2 is a MACHINE/setup
# fault, never a branch's content, and must never be read as a conflict or a
# reason to bounce):
#
#   0 -- merged cleanly (on the first attempt, or after the jsonl-restore
#        retry). Nothing on stdout.
#   1 -- a REAL textual conflict. The conflicting path(s) (one per line, no
#        other chatter) are printed to STDOUT for the caller to capture as
#        $CONFLICTS -- e.g. `CONFLICTS=$(rtk scripts/land-merge-one.sh "$id"
#        "$MSG_DIR")`. The merge is already aborted (working tree left clean)
#        before this script returns.
#   2 -- could not even attempt the merge: bad usage, a missing/empty message
#        file for <id> (Section 3a's precompute did not run, or did not cover
#        this id), or an unexpected git failure that is neither the retryable
#        jsonl trap nor a real conflict (an empty `git ls-files -u` yet the
#        merge still failed). Stderr names the cause. This is the "LOUD, never
#        a silent empty-message merge" acceptance criterion -- refusing here
#        is exactly what closes lode-sfnb's failure mode.
#
# Never touches bd, never pushes, never runs a gate -- purely the merge step.
# The caller (the /land skill) decides what a 1 or a 2 means for the pass.

set -uo pipefail   # deliberately NOT -e: every branch below inspects an exit
                   # code by hand; -e would short-circuit exactly the paths
                   # this script exists to distinguish (retryable jsonl trap
                   # vs. real conflict vs. machine fault).

# The ONE owner of the gate-could-not-run contract (lode-9i2p): the banner
# every exit-2 diagnostic in this repo opens with, this script's own
# advisory trailer below, and the exit 2 itself. Migrated onto
# scripts/gate-lib.sh per lode-090f/lode-bss5 -- this was the stranded 5th
# consumer (merge-precheck.sh, validate-mermaid.sh, release-bump.sh and
# release-latest-tag.sh already sourced it); this script's own extraction
# had deliberately left it inline because gate-lib.sh's extraction was still
# in flight on a sibling branch at the time, and the promised follow-up
# migration never happened until now.
#
# The source itself must fail CLOSED (lode-bss5): an unguarded source that
# fails here leaves gate_could_not_run undefined, and this script's own
# call sites then resolve to a bash "command not found" that does NOT
# reliably exit 2 -- see gate-lib.sh's own Usage section for the measurement
# and why the guard can't depend on the library it's loading.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" 2>/dev/null; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

# This gate's own advisory trailer (see gate-lib.sh's GATE_ADVISORY contract).
# KEEP THIS ABOVE EVERY gate_could_not_run CALL SITE BELOW -- a call placed
# above it still exits 2 with a correct banner but silently emits HALF the
# contract, and nothing catches that but this script's own tests asserting
# the advisory text on an exit-2 path (tests/test_land_merge_one.py's
# _assert_machine_fault_contract).
# shellcheck disable=SC2034  # read by gate_could_not_run() in the sourced gate-lib.sh
GATE_ADVISORY=(
  "This is a machine fault a human must fix, not a branch conflict --"
  "do not kick this branch back needs-rebase in place of diagnosing it."
)

# Arg-count check FIRST, and it must exit 2 -- never `${1:?...}`, whose exit 1
# is exactly the CONFLICT code (same reasoning as merge-precheck.sh's header).
if [ "$#" -ne 2 ]; then
  gate_could_not_run \
    "usage: land-merge-one.sh <id> <land-msg-dir>" \
    "Got $# argument(s), expected exactly 2. This is a caller bug, not a" \
    "branch conflict, so it exits 2 (never 1) to stay out of the conflict path."
fi
id="$1"
msg_dir="$2"

msg_file="$msg_dir/$id"
if [ ! -s "$msg_file" ]; then
  gate_could_not_run \
    "no precomputed merge message for '$id' at '$msg_file'." \
    "/land's Section 3a precompute step must run, and must write a message" \
    "for every id in this pass's accepted set, before any merge is attempted" \
    "-- refusing to merge '$id' with a fabricated or empty commit message."
fi
msg="$(<"$msg_file")"

err="$(git merge --no-ff "origin/land/$id" -m "$msg" 2>&1)" && exit 0

# A native bash match, NOT `printf ... | grep -q`: under `pipefail` above,
# `grep -q` exits the moment it matches, which can SIGPIPE the writer and make
# the whole pipeline report 141 -- silently skipping the retry this branch
# exists to perform. No pipeline, no subprocesses, no hazard.
if [[ "$err" == *"would be overwritten by merge"* ]] \
   && [ -z "$(git ls-files -u)" ]; then
  # Passive-export trap, not a conflict (see docs/decisions.md, lode-6ra /
  # lode-bns3): `.beads/issues.jsonl` got (re-)staged by something other than
  # this merge. Restore it -- it is by invariant never real work -- and retry
  # the SAME merge once.
  git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true
  err="$(git merge --no-ff "origin/land/$id" -m "$msg" 2>&1)" && exit 0
fi

printf '%s\n' "$err" >&2
unmerged="$(git ls-files -u)"
if [ -n "$unmerged" ]; then
  # A REAL textual conflict -- name the paths for the needs-rebase kick-back,
  # then abort back to a clean tree before returning.
  printf '%s\n' "$unmerged" | cut -f2- | sort -u
  git merge --abort
  exit 1
fi

# Neither the jsonl trap nor a real conflict (empty ls-files -u, merge still
# failed): an unexpected git failure. This is a machine fault, not a branch
# verdict -- exit 2, loud, never silent.
gate_could_not_run \
  "'git merge --no-ff origin/land/$id' failed, but git ls-files -u is empty." \
  "This is neither the retryable jsonl trap nor a real textual conflict --" \
  "see git's own error above for the cause."
