#!/usr/bin/env bash
#
# Cheap conflict precheck for /land's Section 2b
# (.claude/skills/land/SKILL.md#2b-cheap-conflict-precheck--does-it-still-merge-onto-trunk):
# does <branch-ref> still merge cleanly onto <base-ref>, without touching the
# working tree? Extracted per lode-mh9g: the inline shell+merge-tree snippet
# this replaces had two live defects (below), and no gate in this repo catches
# a shell/jq snippet embedded in markdown, so it rots silently until a pass
# trips over it -- same defect CLASS as lode-v4rk / lode-verb, both closed the
# same way: extraction to scripts/ + fixture-backed tests.
#
# Usage: scripts/merge-precheck.sh <base-ref> <branch-ref>
#
# Exit 0 -> merges CLEAN. Prints nothing. Caller proceeds to the semantic gate
#           (2c).
# Exit 1 -> textual CONFLICT. Prints ONLY the conflicting path(s), one per
#           line, to stdout -- no tree OID, no blank line, no
#           "Auto-merging"/"CONFLICT" chatter. Caller runs the needs-rebase
#           kick-back with this as $CONFLICTS.
# Exit 2 -> MACHINE FAULT (git < 2.38, an unreadable/unknown ref, unrelated
#           histories, or `merge-tree` failing for any other reason). Prints a
#           diagnostic naming cause + remedy to STDERR, nothing to stdout. The
#           caller must NOT kick back -- per lode-9i2p's rule (already honoured
#           by scripts/validate-mermaid.sh and /land Section 3's nox re-gate),
#           exit 2 is the MACHINE, not the branch: stop and surface it verbatim
#           as a human decision instead.
#
# THE TWO DEFECTS THIS EXTRACTION FIXES (found landing lode-l38d.6, 2026-07-17):
#
# DEFECT 1 -- the inline snippet's `tail -n +2` captured git's informational
# chatter as if it were file paths. Real `git merge-tree --write-tree
# --name-only` output on a conflict is: the tree OID, then the conflicting
# path(s) (one per line, no blank between them), then a BLANK LINE, then
# "Auto-merging ..."/"CONFLICT ..." lines -- including one "Auto-merging
# <path>" line for every file that merged CLEAN. `tail -n +2` alone yields the
# paths PLUS the blank line PLUS all of that chatter, so a file that merged
# clean gets reported as though it conflicted. Fixed by truncating at the
# first blank line.
#
# DEFECT 2 -- `git merge-tree --write-tree` exits 0=clean, 1=conflict, and
# something else on failure (per its own man page: "If the merge is not able
# to complete (or start) due to some kind of error, the exit status is
# something other than 0 or 1"). The inline snippet's `if MT=$(git merge-tree
# ...); then : else <kick back> fi` collapsed conflict and failure into ONE
# else-arm, so a broken invocation blamed the BRANCH (kicked back needs-rebase)
# for a broken MACHINE.
#
# ONE CONFOUNDER VERIFIED EMPIRICALLY (git 2.43.0) THAT THE MAN PAGE DOES NOT
# MENTION: an unknown/unreadable ref does NOT exit "something other than 0 or
# 1" -- it exits 1, identically to a real conflict, but with EMPTY stdout (no
# tree OID at all; a real conflict always writes one, per the man page's own
# "the output includes ... the OID of the top-level tree"). Deriving exit 1
# vs. exit 2 from merge-tree's raw exit status alone would misclassify that
# case as a conflict with zero conflicting paths -- exactly defect 2's failure
# mode, just relocated. So refs are validated explicitly, up front, with `git
# rev-parse --verify`, which gives a precise diagnostic instead of relying on
# that ambiguity. Every OTHER failure mode checked (an unsupported flag on
# git < 2.38, unrelated histories without --allow-unrelated-histories) exits
# 128/129 as the man page describes, so once both refs are known-good,
# merge-tree's raw exit status (0/1/other) is trusted directly for the rest --
# never by parsing its text, since the text is exactly what defect 1 shows is
# unreliable.
#
# Read-only: never touches the working tree (--write-tree writes a tree
# object, not a checkout) and never runs a bd write -- the caller (/land
# Section 2b) is the one that kicks a branch back.

set -uo pipefail   # deliberately NOT -e: this script's entire job is to
                   # inspect a command's exit code, which -e would short-circuit

base="${1:?usage: merge-precheck.sh <base-ref> <branch-ref>}"
branch="${2:?usage: merge-precheck.sh <base-ref> <branch-ref>}"

gate_could_not_run() {
  echo "GATE COULD NOT RUN: $1" >&2
  shift
  for line in "$@"; do echo "$line" >&2; done
  echo "This is a machine fault a human must fix, not a branch conflict --" >&2
  echo "do not kick this branch back needs-rebase in place of diagnosing it." >&2
  exit 2
}

for ref_name_val in "base-ref:$base" "branch-ref:$branch"; do
  ref_name="${ref_name_val%%:*}"
  ref_val="${ref_name_val#*:}"
  if ! git rev-parse --verify --quiet "${ref_val}^{commit}" >/dev/null; then
    gate_could_not_run \
      "unreadable/unknown ref (${ref_name}): '$ref_val' does not resolve to a" \
      "commit. Usual causes: the branch was deleted or force-pushed away, or a" \
      "typo in the ref name. Diagnose with: git rev-parse --verify $ref_val"
  fi
done

errfile="$(mktemp)"
trap 'rm -f "$errfile"' EXIT

out="$(git merge-tree --write-tree --name-only "$base" "$branch" 2>"$errfile")"
rc=$?
err="$(cat "$errfile")"

case "$rc" in
  0)
    exit 0
    ;;
  1)
    # Truncate at the first blank line (defect 1's fix): `tail -n +2` drops
    # the tree-OID line; `sed '/^$/q'` stops printing at (and including) the
    # first blank line; the trailing `sed '/^$/d'` then drops that
    # now-included blank line, leaving only the conflicting path(s).
    printf '%s\n' "$out" | tail -n +2 | sed '/^$/q' | sed '/^$/d'
    exit 1
    ;;
  *)
    lines=(
      "git merge-tree exited $rc while checking whether $branch merges onto"
      "$base (0=clean, 1=conflict expected; this is neither). Usual causes:"
      "git < 2.38 (merge-tree --write-tree was added in 2.38), or unrelated"
      "histories."
    )
    if [ -n "$err" ]; then
      lines+=("git's own error output:")
      while IFS= read -r errline; do lines+=("$errline"); done <<<"$err"
    fi
    gate_could_not_run "git merge-tree failed unexpectedly" "${lines[@]}"
    ;;
esac
