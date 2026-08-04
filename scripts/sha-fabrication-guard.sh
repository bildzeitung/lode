#!/usr/bin/env bash
#
# PreToolUse(Bash) guard body (lode-fpmi): deny a Bash call carrying a 40-hex
# string that LOOKS like a git object id but is not one -- the fingerprint of a
# FABRICATED SHA, i.e. an agent pattern-completing a short prefix it held in
# context rather than deriving the value via `git rev-parse`. The invented tail
# is exactly as fluent as a real one, so this is not self-detectable by
# re-reading what was typed; `git cat-file -e` is the mechanical oracle, since a
# fabricated SHA is essentially never a real object.
#
# Usage: scripts/sha-fabrication-guard.sh '<bash command string>'
#
# Prints nothing (allow) or one PreToolUse hookSpecificOutput JSON object with
# permissionDecision "deny" -- same contract as the lode-ij24/lode-o29m guards.
# ALWAYS exits 0: a PreToolUse hook exiting non-zero is itself a defect.
#
# Extracted from .claude/settings.json so it can be driven by tests
# (tests/test_sha_fabrication_guard.py), following scripts/bd-dolt-push-guard.sh
# and scripts/code-concurrency-cap.sh. Each scope narrowing below, and the one
# deliberately accepted over-match, is documented in docs/agents-workflow.md
# ("Guard against fabricated SHAs (lode-fpmi)") and pinned by the tests.

set -euo pipefail

CMD="${1:-}"
[ -n "$CMD" ] || exit 0

# Cheap fork-free early-out for the overwhelmingly common case: no 40-hex run
# anywhere in the command, so nothing below can possibly deny. This runs on
# EVERY Bash tool call, so it is placed first, ahead of the git/sed/tr/grep/sort
# work it short-circuits. A bash builtin regex, deliberately kept here in the
# tested script rather than inline in .claude/settings.json -- config is where
# this repo has already shipped silent undetected bugs (lode-mh9g, lode-54mo),
# and tests/test_sha_fabrication_guard.py pins the wrapper as logic-free.
# Strict superset of what the scan below can match: the continuation collapse
# only ever replaces a backslash-newline with a space, so it can break a hex run
# apart but never create one.
[[ "$CMD" =~ [0-9a-f]{40} ]] || exit 0

# Not in a git work tree -> cat-file has nothing to check against.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Collapse backslash-newline continuations so a real multi-line `bd update ... \`
# metadata write is still seen as ONE bd invocation segment (lode-m6px).
CMD=$(printf '%s' "$CMD" | sed -e :a -e '/\\$/N; s/\\\n/ /; ta')

# Split into command segments on shell control operators (same technique as the
# lode-o29m gh-write guard), keep only segments that are a bd/git invocation,
# and extract lowercase 40-hex tokens from those. Real `git rev-parse` output is
# always lowercase, so an uppercase token was never meant as a SHA.
INVOKE_RE='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((sudo|env|command|time|nohup|xargs)[[:space:]]+)*(bd|git)([[:space:]]|$)'

TOKENS=$(printf '%s' "$CMD" | tr ';&|(){}`' '\n' \
  | grep -E "$INVOKE_RE" \
  | grep -oE '\b[0-9a-f]{40}\b' \
  | sort -u || true)
[ -n "$TOKENS" ] || exit 0

SHA_LIST=$(while IFS= read -r tok; do
  git cat-file -e "$tok" 2>/dev/null || printf '%s\n' "$tok"
done <<<"$TOKENS" | paste -sd, -)
[ -n "$SHA_LIST" ] || exit 0

jq -n --arg shas "$SHA_LIST" '
  {hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
   permissionDecisionReason: ("lode-fpmi: 40-hex string(s) [" + $shas + "] look like git object ids but do NOT exist as objects in this repository -- the signature of a FABRICATED/retyped SHA (an agent pattern-completing a short prefix it held in context, per lode-fpmi). Never hand-type a long identifier: derive it, e.g. `git rev-parse <ref>` for a commit SHA. If this really is a legitimate value that is simply not yet a reachable object (e.g. a commit on a branch not yet fetched), fetch/derive it fresh rather than retyping it -- or surface this to a human.")}}'

exit 0
