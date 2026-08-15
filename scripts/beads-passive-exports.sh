#!/usr/bin/env bash
#
# Sourced helper owning the load+validate+pathspec-build idiom for
# scripts/beads-passive-exports.txt (lode-xlcm). Before this, FOUR consumers
# each hand-rolled the same mapfile+validate+":(exclude)" transform:
# scripts/worktree-gc-classify.sh, scripts/land-replay.sh,
# scripts/land-merge-one.sh, and scripts/discard-beads-passive-export-churn.sh
# -- two of them (gc-classify, land-replay) byte-for-byte identical apart from
# the failure reporter. The repo's own precedent (scripts/gate-lib.sh,
# lode-090f; also scripts/epic-children-closed.sh,
# scripts/recycled-worktree-guard.sh) is "reaches three copies, extract."
#
# This file is a LIBRARY -- source it, never execute it directly. It performs
# no validation and sets no globals at source time; call
# load_beads_passive_exports() explicitly once sourced.
#
# Usage:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"    # or $(dirname "$0")
#   # shellcheck source=beads-passive-exports.sh
#   if ! . "$SCRIPT_DIR/beads-passive-exports.sh"; then
#     <caller's own "helper missing" failure behaviour here>
#   fi
#   if ! load_beads_passive_exports "$SCRIPT_DIR/beads-passive-exports.txt"; then
#     <caller's own "list unreadable/empty/malformed" failure behaviour here --
#      load_beads_passive_exports has already printed one diagnostic line to
#      stderr naming the list path and the cause>
#   fi
#   # $BEADS_PASSIVE_EXPORTS and $BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS are
#   # now set for the rest of the caller's script.
#
# THE SOURCE LINE ITSELF MUST BE GUARDED, same convention as gate-lib.sh
# (lode-bss5's measurement applies here too: a bare, unguarded source under
# `set -uo pipefail` does not stop the script when the source fails -- it
# just leaves load_beads_passive_exports undefined, and the first call site
# then resolves to a bash "command not found" whose exit code is whatever the
# surrounding logic happens to produce next, never a code the caller chose on
# purpose). A missing/unreadable copy of this file must fail CLOSED.
#
# load_beads_passive_exports <list-path>
#   Reads <list-path>, validates it (must be readable, non-empty, and contain
#   no blank line), and on success sets two globals:
#
#     BEADS_PASSIVE_EXPORTS                    -- the raw array of
#                                                  repo-relative paths, as read
#                                                  from <list-path>.
#     BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS  -- the same paths, each
#                                                  prefixed ":(exclude)" --
#                                                  ready to append to a `git
#                                                  status`/`git diff` pathspec
#                                                  list so passive-export
#                                                  churn never reads as real
#                                                  work (lode-bns3).
#
#   Returns 0 on success. Returns 1 and prints exactly one diagnostic line to
#   stderr (naming <list-path> and the cause) on failure -- this function
#   NEVER exits the calling script. Each caller keeps its own failure
#   semantics on top of that return code: scripts/worktree-gc-classify.sh and
#   scripts/land-replay.sh both fail loud (gate_could_not_run / echo+exit 2)
#   because an empty exclude list would silently invert lode-bns3 for a gate;
#   scripts/discard-beads-passive-export-churn.sh is deliberately best-effort
#   and must keep exiting 0 regardless.
load_beads_passive_exports() {
  local list_path="$1"
  if [ ! -r "$list_path" ]; then
    echo "load_beads_passive_exports: cannot read $list_path" >&2
    return 1
  fi
  mapfile -t BEADS_PASSIVE_EXPORTS < "$list_path"
  if [ "${#BEADS_PASSIVE_EXPORTS[@]}" -eq 0 ] \
    || printf '%s\n' "${BEADS_PASSIVE_EXPORTS[@]}" | grep -qx ''; then
    echo "load_beads_passive_exports: $list_path is empty or contains a blank line" >&2
    return 1
  fi
  BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS=("${BEADS_PASSIVE_EXPORTS[@]/#/:(exclude)}")
  return 0
}
