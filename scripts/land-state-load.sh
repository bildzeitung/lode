#!/usr/bin/env bash
#
# Shared missing-vs-empty policy for /land's $STATE_DIR file loads (lode-dc4n).
#
# .claude/skills/land/SKILL.md reads files under $STATE_DIR (`.git/land-state/`)
# at four call sites, encoding exactly TWO policies with FOUR different hand-
# rolled spellings:
#
#   1. Section 3 first-pass accepted:  cat || { <diagnostic>; exit 1; }   missing fatal, empty OK
#   2. Section 3 isolation accepted:   cat || exit 1  +  [ -n ... ] || exit 1   both fatal (lode-0jan)
#   3. Section 4 landed:               cat || exit 1                          missing fatal, empty OK -- but SILENT
#   4. Kick-back conflicts:            cat 2>/dev/null + [ -n ... ]            both fatal
#
# Site 3 has the SAME policy as site 1 but no diagnostic at all -- the
# loud/silent asymmetry lode-0jan fixed at site 1 still exists one section
# later. This script makes the policy a visible ARGUMENT instead of a
# spelling a future editor has to pick from by guess, and gives it shellcheck
# + unit tests instead of living only in a markdown fence no gate parses.
#
# Usage: scripts/land-state-load.sh <file> [--require-nonempty] [-- <context line>...]
#
# Reads <file> and prints its content to stdout unchanged (the caller's own
# `$(...)` capture strips only the trailing newline, same as every inline
# `cat` this replaces).
#
# Exit 0 -> the file was readable, and (unless --require-nonempty) it may be
#           empty or whitespace-only. Content on stdout.
# Exit 1 -> the file could not be read at all (missing, unreadable, or a
#           directory in its place -- the underlying `cat` failure is
#           captured and its own stderr text is included below), OR
#           --require-nonempty was given and the file read clean but came
#           back empty/whitespace-only. Either way: a diagnostic to stderr,
#           nothing to stdout.
#
# This is deliberately exit 1, never 2: unlike scripts/merge-precheck.sh or
# scripts/validate-mermaid.sh, there is no calling SCRIPT here to classify
# the code programmatically -- every call site is an agent-executed skill
# fence in .claude/skills/land/SKILL.md, reading stderr directly. gate-lib.sh's
# own header covers this split explicitly ("WHY THIS EXIT-2 CONVENTION BINDS
# scripts/*.sh AND NOT AGENT-EXECUTED SKILL FENCES") -- this script is a
# scripts/*.sh file called ONLY from such fences, so it keeps their existing
# exit-1 convention rather than introducing a 2 no caller here would ever
# distinguish from 1.
#
# Two policies, one flag:
#   (default)           missing -> fatal, empty -> OK (prints nothing, exit 0)
#   --require-nonempty   missing -> fatal, empty -> ALSO fatal
#
# Any arguments after a literal `--` are appended to the diagnostic, one per
# line, so a call site can still explain WHY this particular load mattered
# (e.g. "3a's precompute did not run", "nothing to attribute this red to")
# without the policy itself needing to vary per site.

set -uo pipefail   # deliberately NOT -e -- see merge-precheck.sh's identical
                   # note: this script's job is to inspect a command's exit
                   # code, which -e would short-circuit.

if [ "$#" -lt 1 ]; then
  echo "usage: land-state-load.sh <file> [--require-nonempty] [-- <context line>...]" >&2
  exit 1
fi

file="$1"
shift

require_nonempty=false
if [ "$#" -ge 1 ] && [ "$1" = "--require-nonempty" ]; then
  require_nonempty=true
  shift
fi

context=()
if [ "$#" -ge 1 ]; then
  if [ "$1" = "--" ]; then
    shift
    context=("$@")
  else
    echo "usage: land-state-load.sh <file> [--require-nonempty] [-- <context line>...]" >&2
    echo "unexpected argument: $1" >&2
    exit 1
  fi
fi

fail() {
  echo "STATE LOAD FAILED: $1" >&2
  for line in "${context[@]}"; do echo "$line" >&2; done
  exit 1
}

# Guard mktemp the same way scripts/merge-precheck.sh does: an empty string
# back from a failed mktemp would turn `2>"$errfile"` into `2>""`, an
# ambiguous-redirect failure that fails the read for a reason unrelated to
# the FILE this script was asked to load.
errfile="$(mktemp 2>/dev/null)" || fail \
  "could not create a temporary file (mktemp failed) -- usual causes: TMPDIR points at a nonexistent, full, or read-only filesystem."
trap 'rm -f "$errfile"' EXIT

content="$(cat "$file" 2>"$errfile")"
rc=$?
if [ "$rc" -ne 0 ]; then
  err="$(<"$errfile")"
  msg="$file could not be read (missing, unreadable, or a directory in its place)."
  if [ -n "$err" ]; then
    msg="$msg cat's own error: $err"
  fi
  fail "$msg"
fi

if [ "$require_nonempty" = true ] && [ -z "$content" ]; then
  fail "$file is missing or empty."
fi

printf '%s\n' "$content"
