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
#
# QUOTE-AWARE SEGMENT SPLIT + QUOTED-HEREDOC PRE-PASS (lode-dia6). This guard
# used to split into segments with a plain, quoting-UNAWARE `tr` over the
# shell control-operator characters -- the same shape scripts/gh-write-guard.sh
# carried before lode-obox and lode-d5je fixed it there: a control character
# sitting inside a quoted
# STRING ARGUMENT (a commit message, a doc quoting a real SHA) or inside a
# QUOTED HEREDOC body could manufacture a fake segment start, and a 40-hex
# token then landing in that fake segment got scanned as if it sat inside a
# real bd/git invocation. Both primitives (`_split_unquoted`,
# `strip_quoted_heredoc_bodies`) now live in scripts/shell-quote-split.sh,
# shared with gh-write-guard.sh -- see that file's header for the full
# rationale and the false-positive shapes each fix closes. Sourced below,
# failing CLOSED if it cannot be resolved.

set -euo pipefail

CMD="${1:-}"
[ -n "$CMD" ] || exit 0

# Cheap fork-free early-out for the overwhelmingly common case: no 40-hex run
# anywhere in the command, so nothing below can possibly deny. This runs on
# EVERY Bash tool call, so it is placed first, ahead of the git/sed/grep/sort
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

# Fail CLOSED (deny) if the shared quote-aware split library cannot be
# resolved -- a missing/unreadable copy here would silently disable both the
# quoted-argument and quoted-heredoc fixes, reopening the exact
# false-positive class this ticket (lode-dia6) exists to close. Resolved via
# this script's OWN directory so it works regardless of the caller's cwd.
# Placed AFTER the two cheap early-outs above so a command with no 40-hex run
# (or one outside any git work tree) never pays even the cost of resolving it.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_LIB="$_LIB_DIR/shell-quote-split.sh"
if [ ! -r "$_LIB" ]; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
    permissionDecisionReason: "lode-dia6: scripts/shell-quote-split.sh (the shared quote-aware split library scripts/sha-fabrication-guard.sh depends on) could not be resolved -- denying this Bash call rather than silently scanning with the split disabled, since a false ALLOW here is unrecoverable. Surface this to a human; do not retry."}}'
  exit 0
fi
# shellcheck source=scripts/shell-quote-split.sh
source "$_LIB"

# Strip QUOTED heredoc bodies first (line-based, operates on the real
# multi-line command) before collapsing backslash-newline continuations --
# mirroring gh-write-guard.sh's ordering, since the two are different
# multi-line mechanisms and continuation-collapse must not run across a
# heredoc boundary it hasn't yet recognized.
case "$CMD" in
  *'<<'*) CMD=$(strip_quoted_heredoc_bodies "$CMD") ;;
esac

# Collapse backslash-newline continuations so a real multi-line `bd update ... \`
# metadata write is still seen as ONE bd invocation segment (lode-m6px).
CMD=$(printf '%s' "$CMD" | sed -e :a -e '/\\$/N; s/\\\n/ /; ta')

# Split into command segments with the shared quote-aware splitter (lode-dia6,
# same technique as the lode-o29m gh-write guard -- see shell-quote-split.sh),
# keep only segments that are a bd/git invocation, and extract lowercase
# 40-hex tokens from those. Real `git rev-parse` output is always lowercase,
# so an uppercase token was never meant as a SHA.
INVOKE_RE='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((sudo|env|command|time|nohup|xargs)[[:space:]]+)*(bd|git)([[:space:]]|$)'

TOKENS=$(_split_unquoted "$CMD" \
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
