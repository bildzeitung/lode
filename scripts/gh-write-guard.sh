#!/usr/bin/env bash
#
# PreToolUse(Bash) guard body (lode-o29m/lode-9mbt): deny any `gh` invocation
# that does not match a small, explicit READ-ONLY allowlist -- DEFAULT-DENY,
# not a write-verb denylist (lode-9mbt). `gh` is authed as the user, so a
# false ALLOW spends the user's public identity irrecoverably (the
# notification already went out); a false DENY costs a reword. Full
# rationale: docs/agents-workflow.md ("Never write to an external tracker
# under the user's identity (lode-o29m)").
#
# Usage: scripts/gh-write-guard.sh '<bash command string>'
#
# Prints nothing (allow -- fall through to normal permission handling) or one
# PreToolUse hookSpecificOutput JSON object with permissionDecision "deny".
# ALWAYS exits 0: a PreToolUse hook exiting non-zero is itself a defect.
#
# Extracted from .claude/settings.json (mirroring scripts/sha-fabrication-guard.sh,
# lode-fpmi) so the scanning logic can be driven directly by
# tests/test_gh_write_guard.py, and so the quote-aware split below -- which
# needs bash array/substring primitives dash (the harness's actual PreToolUse
# interpreter, lode-9gm2) does not have -- has somewhere to live.
#
# QUOTE-AWARE SEGMENT SPLIT (lode-obox). The command is split into candidate
# "invocation segments" at shell control-operator characters (; & | ( ) { } `)
# so a `gh ...` phrase mid-command (after `&&`, inside `$(...)`, ...) is still
# caught at a command position. Splitting used to be done with plain `tr`,
# which is QUOTING-UNAWARE: a control character sitting inside a single- or
# double-quoted STRING ARGUMENT -- a commit message, a grep pattern -- still
# split the string, and if a `gh <verb>` phrase then landed at the START of
# one of the synthetic fragments, the guard evaluated that fragment as if it
# were a real invocation. Confirmed live against the shipped (pre-fix) hook:
#   git commit -m "See \`gh release create\` for context (lode-w35h)"   -> denied
#   rtk grep -E "(gh issue create)" docs/                               -> denied
#   rtk bd update lode-x --notes "mentions (gh issue create|gh pr comment) both denied" -> denied
# all pure prose/search text with NO actual `gh` invocation anywhere on the
# line. `_split_unquoted` below walks the string tracking single-/double-quote
# state (and a backslash escaping the very next character, in or out of
# quotes) and only treats a control character as a split point OUTSIDE any
# quote -- i.e. it mirrors where the real shell would treat that character as
# an operator, not blindly.
#
# This does NOT touch the deliberately-accepted "quoted indirection" residual
# (`sh -c "gh issue create ..."`, docs/agents-workflow.md): a `gh` phrase
# INSIDE a quoted string was never at a segment START under the OLD splitter
# either, unless a control character happened to precede it INSIDE the quotes
# -- which is exactly the false-positive class this fix closes, not a new gap
# it opens. The fix only stops manufacturing a false segment start where none
# exists in the real shell grammar; it creates no new segment starts (the set
# of positions treated as "outside quotes" by `_split_unquoted` is a subset of
# the positions the old blind `tr` treated as splittable), so it cannot let
# through anything the old splitter would have caught. Differential and
# mutation coverage: tests/test_gh_write_guard.py.

set -euo pipefail

CMD="${1:-}"

# Emit $1 with every UNQUOTED occurrence of ; & | ( ) { } ` replaced by a
# newline; occurrences inside '...' or "..." (and any backslash-escaped
# character, in or out of quotes) are left untouched. Unbalanced quotes at
# end-of-string leave the tail "inside" a quote (no further splits) rather
# than guessing -- the conservative direction, since fewer splits means fewer
# segments are ever offered to the `gh` matcher at all.
_split_unquoted() {
  local s="$1" out="" c state=none i=0 len
  len=${#s}
  while ((i < len)); do
    c="${s:i:1}"
    if [[ "$state" == "none" && "$c" == '\' ]]; then
      out+="$c"
      i=$((i + 1))
      ((i < len)) && out+="${s:i:1}"
    elif [[ "$state" == "double" && "$c" == '\' ]]; then
      out+="$c"
      i=$((i + 1))
      ((i < len)) && out+="${s:i:1}"
    elif [[ "$state" == "none" && "$c" == "'" ]]; then
      state=single
      out+="$c"
    elif [[ "$state" == "single" && "$c" == "'" ]]; then
      state=none
      out+="$c"
    elif [[ "$state" == "none" && "$c" == '"' ]]; then
      state=double
      out+="$c"
    elif [[ "$state" == "double" && "$c" == '"' ]]; then
      state=none
      out+="$c"
    elif [[ "$state" == "none" && ';&|(){}`' == *"$c"* ]]; then
      out+=$'\n'
    else
      out+="$c"
    fi
    i=$((i + 1))
  done
  printf '%s' "$out"
}

[ -n "$CMD" ] || exit 0

SEG=$(_split_unquoted "$CMD")
P='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((if|then|else|do|env|command|sudo|nohup|time|xargs|rtk)[[:space:]]+)*([^[:space:]]*/)?gh([[:space:]]+(-R|--repo|--hostname)([[:space:]]+|=)[^[:space:]]+)*[[:space:]]+'
API='api\b'
R='(issue[[:space:]]+(view|list|status)\b|pr[[:space:]]+(view|list|checks|diff)\b|run[[:space:]]+(list|view)\b|release[[:space:]]+(list|view)\b|repo[[:space:]]+view\b|label[[:space:]]+list\b|workflow[[:space:]]+(list|view)\b|secret[[:space:]]+list\b|variable[[:space:]]+list\b|ssh-key[[:space:]]+list\b|gpg-key[[:space:]]+list\b|cache[[:space:]]+list\b)'
APIWRITE='api[[:space:]]+(.*[[:space:]])?(-[fF]|(--field|--raw-field|--input)([[:space:]]|=)|(-X[[:space:]]*|--method[[:space:]=]+)[A-Za-z])'
APIGET='api[[:space:]]+(.*[[:space:]])?(-X[[:space:]]*|--method[[:space:]=]+)GET\b'

GH=$(printf '%s' "$SEG" | grep -iE "$P" || true)
API_LINES=$(printf '%s' "$GH" | grep -iE "$P$API" || true)
NONAPI_LINES=$(printf '%s' "$GH" | grep -ivE "$P$API" || true)
UNSAFE_API=$(printf '%s' "$API_LINES" | grep -iE "$P$APIWRITE" | grep -ivE "$P$APIGET" || true)
UNSAFE_NONAPI=$(printf '%s' "$NONAPI_LINES" | grep -ivE "$P$R" || true)

if [ -n "$UNSAFE_API" ] || [ -n "$UNSAFE_NONAPI" ]; then
  REASON='lode-o29m/lode-9mbt: this gh guard is DEFAULT-DENY -- your call does not match the read-only ALLOWLIST, so it is presumed a WRITE under your public identity and denied. gh is authed as YOU: a false allow files/comments/closes/merges/reviews/publishes publicly under your name (unrecoverable -- the notification already went out); a false deny costs you seconds to surface and unblock. DRAFT the issue/PR/comment text instead, record it PENDING A HUMAN in the hand-off, and stop -- the human files it manually. NOTE: gh api is allowed ONLY on a positive read test (an explicit -X GET/--method GET, or no -f/-F/--field/--raw-field/--input at all); any other form -- including the IMPLICIT POST that field flags trigger with no method flag at all -- is denied. Read-only gh calls (view/list/status/checks/diff, gh api GET) remain allowed when they match the allowlist. If a legitimate read is denied, do NOT retry or work around this -- surface it to a human so they can widen the allowlist. All internal bd filing is unaffected.'
  jq -n --arg reason "$REASON" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
fi

exit 0
