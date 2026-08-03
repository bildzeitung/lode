#!/usr/bin/env bash
#
# Determine the conventional-commit SemVer bump (breaking|feat|fix|none) for a
# git log range, for /release's Section 2 derivation
# (.claude/skills/release/SKILL.md#2-derive-the-proposal).
#
# Extracted per lode-ns3r: the inline snippet this replaces read each commit's
# full message via `git log RANGE --format='%B%x00'` +
# `while IFS= read -r -d '' MSG`, then took the commit's subject as
# `SUBJECT=$(printf '%s' "$MSG" | head -1)`. git inserts a newline AFTER each
# record's `%B` expansion, BEFORE the `%x00` delimiter -- so the NUL-delimited
# stream is actually "body1\n\x00body2\n\x00...", not
# "body1\x00body2\x00...". Every record from the SECOND onward is therefore
# captured WITH A LEADING NEWLINE, so `head -1` on it returns an EMPTY first
# line, and the subject regexes (^feat/^fix/^...!:) never match anything but
# the newest commit in the range. Concrete case (lode-905v): v1.1.0..trunk
# during the v1.2.0 release contained 2 feat(...) commits, neither of them the
# newest commit in the range -- the inline snippet computed BUMP=none (would
# have proposed v1.1.1 instead of the correct v1.2.0). Only a manual re-scan
# by the operator caught it before the release shipped.
#
# Fixed per the ticket's "simplest" fix option: subjects are read from a
# SEPARATE `git log RANGE --format='%s'` stream, one full line per commit --
# a commit subject is, by git's own convention, always exactly one line, so
# there is no record-splitting to get wrong and no NUL/newline-ordering
# footgun left to trip over. The BREAKING-CHANGE-in-body check still needs
# the full message body (the marker can appear anywhere in a footer, not just
# the subject), so it scans a separate `--format='%B'` stream for the literal
# marker -- but that scan only ever needs to know "did ANY commit in range
# contain this marker", never WHICH commit, so it is immune to the same
# per-record-boundary bug by construction (no per-commit split is needed at
# all for a whole-stream substring search).
#
# Usage: scripts/release-bump.sh <range>       # e.g. "v1.1.0..HEAD"
#
# Exit 0 -> prints exactly one of: breaking | feat | fix | none
# Exit 2 -> MACHINE FAULT (range doesn't resolve, git failure). Diagnostic to
#           stderr, nothing to stdout -- same "exit 2 is the machine, never
#           the content" convention as scripts/merge-precheck.sh (lode-9i2p).
#
# Precedence when several kinds of commits are present: breaking > feat > fix
# > none -- matches docs/release.md and the skill's own written precedence.
#
# Read-only: runs `git log`, never touches the working tree, never writes bd.

set -uo pipefail

# The source itself must fail CLOSED (lode-bss5) -- see gate-lib.sh's Usage
# section for the measurement and why the guard can't use the library it loads.
# --no-advisory (lode-ysr6): this gate carries no domain-specific trailer, the
# same shape it always had -- but the sentinel is REQUIRED, never omitted.
# Omitting it would fold this script's own RANGE argument into GATE_ADVISORY
# instead; see gate-lib.sh's GATE_ADVISORY contract for why.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" --no-advisory; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

# Arg-count check first, and it must exit 2 -- never let an unset "${1:?...}"
# exit 1, which would collide with a legitimate "none" outcome being
# communicated some other way. There is no ambiguity to guard against here
# the way merge-precheck.sh has (0/1/2 are all live verdicts there); this
# script only ever prints to stdout on exit 0, so the arity check is simpler,
# but the exit code itself still must not overload a real bump value.
if [ "$#" -ne 1 ]; then
  gate_could_not_run \
    "usage: release-bump.sh <range>" \
    "Got $# argument(s), expected exactly 1 (a git log range, e.g. 'v1.1.0..HEAD')."
fi
RANGE="$1"

errfile="$(mktemp 2>/dev/null)" || gate_could_not_run \
  "could not create a temporary file (mktemp failed)" \
  "Usual causes: TMPDIR points at a nonexistent, full, or read-only filesystem."
trap 'rm -f "$errfile"' EXIT

# Each `git log` read is CAPTURED into a variable with its exit status
# checked, and every match then runs against that variable via a here-string.
# `git log` is NEVER piped straight into `grep`. Two distinct bugs made that
# mandatory, both of which shipped in the first cut of this script:
#
#   1. SIGPIPE + pipefail = a silent FALSE NEGATIVE. `grep -q` exits the
#      instant it matches, closing the pipe; `git log` is then killed by
#      SIGPIPE (exit 141), and `set -o pipefail` promotes that 141 to the
#      status of the WHOLE pipeline -- so `if git log ... | grep -q ...`
#      evaluated FALSE exactly when the marker WAS found, provided it was
#      found early enough that output was still in flight. That inverts the
#      check precisely in the case that matters most: a recent commit
#      carrying the marker. Measured on lode's own history, `v1.1.0..HEAD` is
#      ~75KB of `%B` -- far past the threshold -- and a synthetic range with
#      the marker in the NEWEST commit returned `feat` (MINOR) where the
#      right answer was `breaking` (MAJOR). Note this was a POSITION-dependent
#      false negative: the same class of bug this script was extracted to fix
#      (lode-ns3r), reintroduced through a different mechanism. A here-string
#      has no writer process, so there is no pipe left to break.
#
#   2. An unchecked `git log` failure produced NO output, which reads
#      identically to "no marker" / "no recognized prefixes" and returned a
#      legitimate-looking `none` on exit 0 -- a machine fault reported as
#      content, the exact thing this script's exit-2 contract exists to
#      prevent.
#
# Checking each read's status also removes the need for a separate up-front
# range validation: an unresolvable range fails the first `git log` with
# git's own diagnostic, so one error path covers both.

# NOTE on the `|| exit $?` at each call site below, which is load-bearing:
# `read_log` is always invoked inside a command substitution, and a command
# substitution runs in a SUBSHELL -- so `gate_could_not_run`'s `exit 2` ends
# only that subshell, NOT this script. Without the explicit propagation the
# assignment would simply succeed with an empty value and the script would
# print a confident `none` on exit 0, which is precisely the machine-fault-
# reported-as-content failure the exit-2 contract exists to prevent. The
# diagnostics themselves still reach the terminal either way (stderr is not
# captured by the substitution); it is only the exit status that needs help.
read_log() {   # read_log <pretty-format> -> stdout, or exit 2 with git's own stderr
  local out err
  if out="$(git log "$RANGE" --format="$1" 2>"$errfile")"; then
    printf '%s' "$out"
    return 0
  fi
  local lines=("git log failed on range '$RANGE' (format '$1')."
               "Usual causes: a deleted/mistyped tag, a malformed range expression,"
               "or a machine fault. This is never a statement about the commits."
               "Diagnose with: git log $RANGE --format='$1'")
  err="$(<"$errfile")"
  if [ -n "$err" ]; then
    lines+=("git's own error output:")
    while IFS= read -r errline; do lines+=("$errline"); done <<<"$err"
  fi
  gate_could_not_run "git log failed" "${lines[@]}"
}

# Each of the four greps below partitions its OWN exit code the same way
# lode-yoc3 did for validate-mermaid.sh's per-doc loop (lode-umtc): exit 1 is
# grep's own "no match" -- a CONTENT answer (this stream genuinely carries no
# such marker) -- and every other nonzero exit (an unreadable stream, an I/O
# fault, grep missing from PATH (127), ...) is a MACHINE fault that must not
# be silently read as "no match". Before this fix all four sites landed in
# the same implicit fallthrough a bare `if`/`elif` chain gives a false test:
# a fault at any of them silently downgraded the verdict (breaking -> feat ->
# fix -> none) and still exited 0 -- worse than the mermaid gate's false PASS,
# since a wrong-but-plausible SemVer level then gets tagged and shipped.
#
# `!` is deliberately NOT used to test each grep (same reason as
# validate-mermaid.sh's loop): `if ! cmd; then rc=$?` captures 0, not cmd's
# own status -- measured on lode-yoc3, restated here since a fourth
# consumer joining this file makes it worth restating once at the top rather
# than at each of the four sites below.
#
# EXTRACTION DECIDED (per this ticket's own acceptance criterion): declined,
# not done. Between this file (four sites) and validate-mermaid.sh's two
# (its per-doc grep, and its `docker run` partition, the same "exit 1 is
# content, everything else is a fault" shape) the idiom now stands at six
# sites across two files -- gate-lib.sh's own header cites reaching three
# duplicated copies as its bar for extracting a *shared* piece (that is what
# justified pulling gate_could_not_run itself out from three call sites). But
# this idiom isn't one reusable statement the way that function was: every
# site here differs in its SUCCESS arm (echo a different verdict and exit 0,
# vs. mermaid's `found=1` / `echo OK`), its FALLTHROUGH arm (this file falls
# through to the next check on a clean "no match"; mermaid's loop instead
# `continue`s to the next doc), and its FAILURE message (which marker, which
# stream, which diagnostic command to hand the operator). A wrapper general
# enough to cover all six would need a success callback, a fallthrough
# callback, and a caller-supplied cause-line array -- machinery that is not
# meaningfully shorter than the `if`/`else`/`rc=$?` block it would replace.
# So each site stays open-coded, matching the shape lode-yoc3 already
# established rather than inventing a third one.

# BREAKING-CHANGE-in-body check: a whole-stream search. No per-commit
# attribution is needed -- only "did ANY commit in range carry the marker" --
# so there is no record-splitting to get wrong.
BODIES="$(read_log '%B')" || exit $?
if grep -qE 'BREAKING[ -]CHANGE:' <<<"$BODIES"; then
  echo breaking
  exit 0
else
  rc=$?
  if [ "$rc" -ne 1 ]; then
    gate_could_not_run \
      "grep failed scanning commit bodies for a BREAKING-CHANGE marker" \
      "(exit $rc) -- grep's exit 1 means \"no match\" (a content answer: no" \
      "breaking-change marker anywhere in this range), so anything else is a" \
      "machine fault, not content. Diagnose with:" \
      "git log $RANGE --format='%B' | grep -qE 'BREAKING[ -]CHANGE:'"
  fi
fi

# Subjects: `%s` is one full line per commit, so `grep`'s own per-line
# matching supplies the record boundaries and the `^` anchors bind per
# commit. Precedence (breaking > feat > fix > none) used to fall out of an
# `if`/`elif`/`elif`/`else` chain; that chain is now three sequential `if`
# blocks instead, each of which exits immediately on a match (`echo ...;
# exit 0`) and each of which escalates immediately on a machine fault rather
# than falling through to the next, lower-precedence check. Only a genuine
# "no match" (rc 1) reaches the next block -- the precedence order is
# unchanged, but a fault at, say, the breaking-subject check can no longer be
# silently reported as `feat` by the next block down.
SUBJECTS="$(read_log '%s')" || exit $?
if grep -qE '^[a-zA-Z]+(\([^)]*\))?!:' <<<"$SUBJECTS"; then
  echo breaking
  exit 0
else
  rc=$?
  if [ "$rc" -ne 1 ]; then
    gate_could_not_run \
      "grep failed scanning commit subjects for a breaking-change marker" \
      "(exit $rc) -- grep's exit 1 means \"no match\" (a content answer: no" \
      "subject in this range carries a \"!:\" breaking marker), so anything" \
      "else is a machine fault, not content. Diagnose with:" \
      "git log $RANGE --format='%s' | grep -qE '^[a-zA-Z]+(\([^)]*\))?!:'"
  fi
fi

if grep -qE '^feat(\([^)]*\))?:' <<<"$SUBJECTS"; then
  echo feat
  exit 0
else
  rc=$?
  if [ "$rc" -ne 1 ]; then
    gate_could_not_run \
      "grep failed scanning commit subjects for a feat prefix (exit $rc) --" \
      "grep's exit 1 means \"no match\" (a content answer: no feat commit in" \
      "this range), so anything else is a machine fault, not content." \
      "Diagnose with: git log $RANGE --format='%s' | grep -qE '^feat(\([^)]*\))?:'"
  fi
fi

if grep -qE '^fix(\([^)]*\))?:' <<<"$SUBJECTS"; then
  echo fix
  exit 0
else
  rc=$?
  if [ "$rc" -ne 1 ]; then
    gate_could_not_run \
      "grep failed scanning commit subjects for a fix prefix (exit $rc) --" \
      "grep's exit 1 means \"no match\" (a content answer: no fix commit in" \
      "this range), so anything else is a machine fault, not content." \
      "Diagnose with: git log $RANGE --format='%s' | grep -qE '^fix(\([^)]*\))?:'"
  fi
fi

echo none
