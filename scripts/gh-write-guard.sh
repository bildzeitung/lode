#!/usr/bin/env bash
#
# PreToolUse(Bash) guard body (lode-o29m / lode-9mbt): DEFAULT-DENY any `gh` call
# that is not on the read-only ALLOWLIST. `gh` is authed as the USER, so a write
# files/comments/closes/merges/reviews/publishes publicly under their name and is
# unrecoverable -- the notification has already gone out. A false deny costs
# seconds. The trade is deliberately asymmetric, hence allowlist not denylist.
#
# Usage: scripts/gh-write-guard.sh '<bash command string>'
#
# Prints nothing (allow) or one PreToolUse hookSpecificOutput JSON object with
# permissionDecision "deny" -- same contract as the lode-ij24/lode-fpmi guards.
# ALWAYS exits 0: a PreToolUse hook exiting non-zero is itself a defect.
#
# Extracted from .claude/settings.json (lode-fpmi's acceptance criterion, applied
# to the remaining two inline guards: "the guard logic lives in a tested script,
# not untested inline shell" -- ungated inline shell in config is where this repo
# has already shipped silent undetected-for-months bugs, lode-mh9g / lode-54mo).
# Behaviour is unchanged by the extraction; tests/test_gh_write_guard.py drives
# both this script and the shipped wrapper. Every scope decision below, and the
# two accepted structural residuals, are documented in docs/agents-workflow.md
# ("Never write to an external tracker under the user's identity (lode-o29m)").
#
# NOTE: this file may use bash-only syntax -- it runs under `bash "$SCRIPT"`, not
# under the harness's dash /bin/sh. The WRAPPER in settings.json is what must stay
# POSIX (lode-9gm2), and its test pins that.

set -euo pipefail

CMD="${1:-}"
[ -n "$CMD" ] || exit 0

# Split into command segments on shell control operators, so `gh` is only ever
# judged at a command position.
SEG=$(printf '%s' "$CMD" | tr ';&|(){}`' '\n')

# `gh` at a command position: through a leading VAR=x assignment, a fixed wrapper
# list, an absolute/relative path to the binary, and gh's global -R/--repo/
# --hostname flags inserted before the subcommand.
P='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((if|then|else|do|env|command|sudo|nohup|time|xargs)[[:space:]]+)*([^[:space:]]*/)?gh([[:space:]]+(-R|--repo|--hostname)([[:space:]]+|=)[^[:space:]]+)*[[:space:]]+'
API='api\b'
# The read-only allowlist: everything NOT matching this is presumed a write.
R='(issue[[:space:]]+(view|list|status)\b|pr[[:space:]]+(view|list|checks|diff)\b|run[[:space:]]+(list|view)\b|release[[:space:]]+(list|view)\b|repo[[:space:]]+view\b|label[[:space:]]+list\b|workflow[[:space:]]+(list|view)\b|secret[[:space:]]+list\b|variable[[:space:]]+list\b|ssh-key[[:space:]]+list\b|gpg-key[[:space:]]+list\b|cache[[:space:]]+list\b)'
# `gh api` is judged separately: field flags trigger an IMPLICIT POST with no -X
# on the line at all, which is gh's documented default and NOT a way around this.
APIWRITE='api[[:space:]]+(.*[[:space:]])?(-[fF]|(--field|--raw-field|--input)([[:space:]]|=)|(-X[[:space:]]*|--method[[:space:]=]+)[A-Za-z])'
# ...but fields on an EXPLICIT GET are query params, not a body -- gh documents
# this as the way to send a GET query string. Scoped to the SAME segment, so a
# read-then-write chain cannot let the read half exempt the write half.
APIGET='api[[:space:]]+(.*[[:space:]])?(-X[[:space:]]*|--method[[:space:]=]+)GET\b'

GH=$(printf '%s' "$SEG" | grep -iE "$P" || true)
API_LINES=$(printf '%s' "$GH" | grep -iE "$P$API" || true)
NONAPI_LINES=$(printf '%s' "$GH" | grep -ivE "$P$API" || true)
UNSAFE_API=$(printf '%s' "$API_LINES" | grep -iE "$P$APIWRITE" | grep -ivE "$P$APIGET" || true)
UNSAFE_NONAPI=$(printf '%s' "$NONAPI_LINES" | grep -ivE "$P$R" || true)

if [ -n "$UNSAFE_API" ] || [ -n "$UNSAFE_NONAPI" ]; then
  printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "lode-o29m/lode-9mbt: this gh guard is DEFAULT-DENY -- your call does not match the read-only ALLOWLIST, so it is presumed a WRITE under your public identity and denied. gh is authed as YOU: a false allow files/comments/closes/merges/reviews/publishes publicly under your name (unrecoverable -- the notification already went out); a false deny costs you seconds to surface and unblock. DRAFT the issue/PR/comment text instead, record it PENDING A HUMAN in the hand-off, and stop -- the human files it manually. NOTE: gh api is allowed ONLY on a positive read test (an explicit -X GET/--method GET, or no -f/-F/--field/--raw-field/--input at all); any other form -- including the IMPLICIT POST that field flags trigger with no method flag at all -- is denied. Read-only gh calls (view/list/status/checks/diff, gh api GET) remain allowed when they match the allowlist. If a legitimate read is denied, do NOT retry or work around this -- surface it to a human so they can widen the allowlist. All internal bd filing is unaffected."}}'
fi

exit 0
