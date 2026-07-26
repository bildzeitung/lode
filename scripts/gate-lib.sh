#!/bin/bash
#
# Shared gate_could_not_run() helper for the "exit 2 means the GATE could not
# run, never that the CONTENT is bad" convention (lode-9i2p). Sourced by every
# scripts/*.sh gate that draws this distinction -- scripts/validate-mermaid.sh,
# scripts/merge-precheck.sh, and scripts/release-bump.sh -- the same way
# scripts/python-init.sh already sources scripts/venv-install.sh.
#
# Extracted per lode-090f: this exact function had reached three duplicated
# literal copies (found technically reviewing lode-ns3r), free to drift --
# and had already started to: validate-mermaid.sh's copy printed a two-line
# domain advisory ("not a mermaid syntax error ...") that merge-precheck.sh's
# ("not a branch conflict ...") and release-bump.sh's (no advisory at all)
# each stated differently, so the "exit 2 is the machine, never the content"
# contract was spelled out three slightly different ways. Same "reaches three
# copies, extract" precedent as scripts/epic-children-closed.sh and
# scripts/recycled-worktree-guard.sh, both extracted for exactly that reason.
#
# Usage (from a sourcing script):
#   # shellcheck source=gate-lib.sh
#   . "$(dirname "$0")/gate-lib.sh"
#   gate_could_not_run "one-line summary" "cause line 1" "cause line 2" ...
#
# Prints "GATE COULD NOT RUN: <summary>" to stderr, then every remaining
# argument on its own line (also stderr), then any lines in the caller's
# GATE_ADVISORY array (see below), then exits the CALLING PROCESS with status
# 2 -- NEVER 0 or 1, which are live content verdicts in every caller
# (merge-precheck.sh alone treats 0/1/2 as three distinct verdicts; this
# helper only ever produces the exit-2 arm, so that three-way split is
# unaffected). Call it only from the top-level script process, never from
# inside a command substitution: a subshell's `exit` only ends the subshell,
# not the calling script -- every existing call site already handles this
# correctly (e.g. release-bump.sh's `read_log() { ...; } ... || exit $?`
# propagation out of its own command-substitution subshell).
#
# GATE_ADVISORY (optional): a bash array a sourcing script may set BEFORE
# calling gate_could_not_run, to append fixed, domain-specific advisory lines
# after every caller-supplied cause -- e.g. merge-precheck.sh's "do not kick
# this branch back needs-rebase in place of diagnosing it." Set it ONCE, near
# the top of the sourcing script (after sourcing this file); every call site
# in that script then gets it automatically, so the advisory is never
# repeated per call site. Leave it unset (the default) for no trailer at all
# -- release-bump.sh's shape, which carries none.
#
# GATE_ADVISORY is declared here (as an empty array, if not already set) so
# that referencing it below is safe under `set -u`: bash's `nounset` treats a
# never-declared array as an unbound-variable error on `${arr[@]}` (verified
# empirically, bash 5.2), unlike a scalar's more forgiving `${var:-}` default.
declare -p GATE_ADVISORY >/dev/null 2>&1 || GATE_ADVISORY=()

gate_could_not_run() {
  echo "GATE COULD NOT RUN: $1" >&2
  shift
  for line in "$@"; do echo "$line" >&2; done
  for line in "${GATE_ADVISORY[@]}"; do echo "$line" >&2; done
  exit 2
}
