#!/usr/bin/env bash
#
# PreToolUse(Bash) guard body for lode-fpmi: deny a Bash call that contains a
# 40-hex string which LOOKS like a git object id (a full SHA-1) but does not
# actually exist as a git object in this repository -- the fingerprint of a
# FABRICATED/retyped SHA rather than a real one copied or derived from git.
#
# Root cause (lode-fpmi): an agent held a short 7-char prefix in context,
# needed the full 40-char form for a bd --metadata write (land_head /
# review_head, which /land and /code both read to check out and detect
# drift), and pattern-completed the remaining 33 characters rather than
# deriving it via `git rev-parse <ref>`. The invented tail is exactly as
# fluent as a real one, so this is not self-detectable by re-reading what
# was typed -- it needs a mechanical check. `git cat-file -e <sha>` is that
# check: a fabricated SHA is, by construction, essentially always a
# nonexistent object.
#
# Extracted to its own script (rather than embedded inline in
# .claude/settings.json, unlike the lode-ij24/lode-o29m guards) specifically
# so it can be driven by tests/test_sha_fabrication_guard.py the way
# scripts/code-concurrency-cap.sh is driven by
# tests/test_code_concurrency_cap.py -- per this ticket's own acceptance
# criteria: "Ungated inline shell embedded in config is exactly where this
# repo already shipped a silent undetected-for-months bug" (lode-mh9g,
# lode-54mo).
#
# Usage: scripts/sha-fabrication-guard.sh '<bash command string>'
#
# On stdout: nothing (fall through / allow) OR a single-line PreToolUse
# hookSpecificOutput JSON object with permissionDecision "deny" -- same
# contract as the lode-ij24/lode-o29m guards, so the settings.json wrapper
# can just print whatever this script prints. Always exits 0: a PreToolUse
# hook exiting non-zero is itself a defect (see tests/test_bd_deps_guard.py).
#
# Scope (deliberately narrow, per this ticket's design constraints):
#   - Skipped entirely when not inside a git work tree -- git cat-file has
#     nothing to check against.
#   - Only scans command SEGMENTS (split on ; & | ( ) { } and backtick, same
#     technique as the lode-o29m gh-write guard) that are themselves a bd or
#     git invocation (optionally through `rtk`, leading env assignments, or
#     a handful of common wrapper commands) -- "do not scan every Bash call
#     for hex" (this ticket's own constraint). A 40-hex string inside an
#     unrelated command (grep, cat, echo, curl, ...) is never even looked
#     at.
#   - Only lowercase [0-9a-f]{40} tokens count -- real `git rev-parse` /
#     `git log --format=%H` output is always lowercase; this also
#     sidesteps an all-caps 40-char token that was never meant as a SHA in
#     the first place.
#   - Backslash-newline line continuations are collapsed to a space before
#     scanning, so a real, multi-line `bd update <id> \` +
#     `  --set-metadata land_head=<sha>` call is still recognised as ONE
#     bd-invocation segment (same fix as lode-m6px's sed-based collapse in
#     the lode-ij24 guard; ported here since a metadata write is routinely
#     multi-line, per this repo's own coding.md examples).
#
# Known, accepted over-match (same tradeoff this repo already made for the
# lode-ij24/lode-o29m guards, per lode-oii9's tiebreak: when a regex-based
# guard cannot evaluate precisely, it denies rather than silently letting a
# real fabrication through): a genuine-looking 40-lowercase-hex-character
# run embedded in prose inside a bd --title/--description/--notes value on
# a line this script judges to be a bd/git invocation is scanned too, since
# this is a heuristic guard, not a shell parser. Any git object that
# genuinely exists still passes cat-file -e and is allowed regardless of
# where in the segment it appears, so this only matters for a
# fabricated-looking string that is ALSO not a real object AND also isn't
# meant as an identifier at all -- vanishingly unlikely in real usage, and
# the guard's own deny message names the offending token and tells you how
# to route around it.

set -euo pipefail

CMD="${1:-}"
[ -n "$CMD" ] || exit 0

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

CMD=$(printf '%s' "$CMD" | sed -e :a -e '/\\$/N; s/\\\n/ /; ta')

INVOKE_RE='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((rtk|sudo|env|command|time|nohup|xargs)[[:space:]]+)*(bd|git)([[:space:]]|$)'

# Split on shell control operators -- each resulting line is one command
# segment, same technique as the lode-o29m gh-write guard.
SEGMENTS=$(printf '%s' "$CMD" | tr ';&|(){}`' '\n')

ALL_TOKENS=$(printf '%s\n' "$SEGMENTS" | while IFS= read -r seg; do
  printf '%s' "$seg" | grep -qE "$INVOKE_RE" || continue
  printf '%s' "$seg" | grep -oE '\b[0-9a-f]{40}\b' || true
done | sort -u)

[ -n "$ALL_TOKENS" ] || exit 0

FOUND=""
while IFS= read -r tok; do
  [ -n "$tok" ] || continue
  git cat-file -e "$tok" 2>/dev/null || FOUND="$FOUND $tok"
done <<<"$ALL_TOKENS"

FOUND=$(printf '%s' "$FOUND" | sed -e 's/^ *//' -e 's/ *$//')
[ -n "$FOUND" ] || exit 0

SHA_LIST=$(printf '%s' "$FOUND" | tr ' ' ',')

jq -n --arg shas "$SHA_LIST" '
  {hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
   permissionDecisionReason: ("lode-fpmi: 40-hex string(s) [" + $shas + "] look like git object ids but do NOT exist as objects in this repository -- the signature of a FABRICATED/retyped SHA (an agent pattern-completing a short prefix it held in context, per lode-fpmi). Never hand-type a long identifier: derive it, e.g. `git rev-parse <ref>` for a commit SHA. If this really is a legitimate value that is simply not yet a reachable object (e.g. a commit on a branch not yet fetched), fetch/derive it fresh rather than retyping it -- or surface this to a human.")}}'

exit 0
