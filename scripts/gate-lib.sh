#!/bin/bash
#
# Shared gate_could_not_run() helper for the "exit 2 means the GATE could not
# run, never that the CONTENT is bad" convention (lode-9i2p). Sourced by every
# scripts/*.sh gate that draws this distinction -- discover the current set
# rather than naming it here, since a named list goes stale on every migration
# -- the same way scripts/python-init.sh already sources
# scripts/venv-install.sh. Ask for the SOURCE LINE, not the library's name:
# `grep -l gate-lib.sh scripts/*.sh` also returns this file plus any script
# that merely explains why it does NOT source the library (lode-pcee), so use
#   grep -lE '^[^#]*\. "\$\(dirname "\$0"\)/gate-lib\.sh"' scripts/*.sh
# which is the same question tests/test_gate_lib.py's `_sources_gate_lib` asks.
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
# Usage (from a sourcing script) -- the source itself MUST be guarded so a
# missing/unreadable gate-lib.sh fails CLOSED (exit 2), never falls through to
# whatever happens next when gate_could_not_run is called but was never
# defined. MEASURED (lode-bss5): a bare, unguarded
# `. "$(dirname "$0")/gate-lib.sh"` under `set -uo pipefail` (no -e, the
# convention every consumer uses) does NOT stop the script when the source
# fails -- it just leaves gate_could_not_run undefined, and the first call
# site then resolves to a bash "command not found" whose exit code is
# whatever the surrounding logic happens to produce next: measured as 0, 1,
# and 127 across two real consumers, never the required 2. The guard cannot
# depend on this library (it doesn't exist yet at that point), so it is
# small, plain bash, duplicated verbatim in every consumer -- and pinned
# byte-for-byte across all of them by tests/test_gate_lib.py's sweep, which
# discovers the consumer set rather than listing it, so a NEW consumer that
# sources this library bare is caught the day it lands:
#
#   # shellcheck source=gate-lib.sh
#   if ! . "$(dirname "$0")/gate-lib.sh"; then
#     echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
#     echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
#     exit 2
#   fi
#   gate_could_not_run "one-line summary" "cause line 1" "cause line 2" ...
#
# NOTE the deliberate absence of `2>/dev/null` on that source: bash's own
# message ("No such file or directory", or a syntax error with a line number
# if this file is present but corrupt) is the only evidence of WHICH failure
# occurred, and the guard's own two lines cannot reproduce it. Suppressing it
# would report a corrupt library as a missing one -- naming one confident
# cause for every failure, which is the lode-9i2p bug relocated.
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
# THE COST OF THAT CONVENIENCE, and the one thing to get right when adding a
# call site: this used to be structural. The advisory lived inside the function
# body, so a call could not exist without emitting it. Now it is an ORDERING
# CONVENTION -- a call site placed above its script's GATE_ADVISORY assignment
# still exits 2 with a correct banner, but silently emits HALF the contract,
# which is precisely how lode-9i2p's machine-vs-content confusion gets back in.
# An accidentally-empty GATE_ADVISORY is byte-identical to release-bump.sh's
# deliberately-empty one, so nothing in the LANGUAGE can tell them apart:
# `set -u` sees a validly declared-empty array, shellcheck's view is suppressed
# by the SC2034 disable each caller needs, and this library's own tests choose
# their own orderings.
#
# It is nonetheless enforced, in two places (lode-bss5):
#
#   * tests/test_gate_lib.py sweeps the DISCOVERED consumer set and asserts
#     that every advisory-setting consumer assigns GATE_ADVISORY above all of
#     its own call sites. This covers consumers nobody wrote a bespoke test
#     for, including ones added after this comment.
#   * each consuming script's own tests assert the advisory TEXT on an exit-2
#     path, which the sweep cannot do (it reads line order, not output). When
#     you add a call site IN A CONSUMER THAT SETS GATE_ADVISORY, add or extend
#     such an assertion (tests/test_merge_precheck.py and
#     tests/test_validate_mermaid_gate.py show the shape).
#
# For a NO-advisory consumer (release-bump.sh's shape -- no GATE_ADVISORY set
# at all) there is nothing to assert either way: half-a-contract cannot go
# missing from a contract that was never more than the banner and the caller's
# own cause lines to begin with. The sweep skips those consumers explicitly.
#
# GATE_ADVISORY is declared here (as an empty array, if not already set) so
# that referencing it below is safe under `set -u`: bash's `nounset` treats a
# never-declared array as an unbound-variable error on `${arr[@]}` (verified
# empirically, bash 5.2), unlike a scalar's more forgiving `${var:-}` default.
#
# Do NOT "tidy" this into `[[ -v GATE_ADVISORY ]] || GATE_ADVISORY=()`. The two
# are not equivalent: `-v` on an array tests element 0, so it reports FALSE for
# an array that is declared but empty, and the `||` would then reinitialize a
# caller's deliberately-empty GATE_ADVISORY (measured, bash 5.2). `declare -p`
# tests declaration, which is the actual question here.
declare -p GATE_ADVISORY >/dev/null 2>&1 || GATE_ADVISORY=()

gate_could_not_run() {
  echo "GATE COULD NOT RUN: $1" >&2
  shift
  for line in "$@"; do echo "$line" >&2; done
  for line in "${GATE_ADVISORY[@]}"; do echo "$line" >&2; done
  exit 2
}
