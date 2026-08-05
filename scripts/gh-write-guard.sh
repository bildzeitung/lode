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
# NOTE: this file may use bash-only syntax -- it runs under `bash "$SCRIPT"`, not
# under the harness's dash /bin/sh. The WRAPPER in settings.json is what must stay
# POSIX (lode-9gm2), and its test pins that.
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
#   grep -E "(gh issue create)" docs/                                   -> denied
#   bd update lode-x --notes "mentions (gh issue create|gh pr comment) both denied" -> denied
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
# exists in the real shell grammar; it creates no NEW segment starts.
#
# But fewer segment starts is not automatically safe, and one case had to be
# put back: `$(...)` and an UNESCAPED backtick are live command substitution
# INSIDE double quotes (`;`, `|`, `(`, `{` are literal there; these two are
# not). The old blind `tr` split them incidentally and so denied
# `echo "$(gh issue create ...)"`; treating the whole double-quoted region as
# inert would have dropped that silently -- an unargued narrowing of the deny
# surface, which this ticket's own acceptance criteria forbid. So
# `_split_unquoted` splits on those two inside double quotes and only those
# two, which is exactly where the real shell would. Differential and mutation
# coverage: tests/test_gh_write_guard.py.
#
# QUOTED-HEREDOC PRE-PASS (lode-d5je). A QUOTED heredoc body (<<'EOF', <<"EOF",
# <<\EOF) is inert text -- the shell performs NO substitution in it at all --
# but the segment split above (on `` ` ``/`(`/`)`/etc.) does not know that, so
# a command substitution written as a worked example inside such a body
# manufactures a fake segment start and gets scanned as if it were live shell.
# An UNQUOTED heredoc (<<EOF) is the opposite: substitution IS real there, so
# its body must keep being scanned exactly as before -- `strip_quoted_heredoc_bodies`
# below only ever removes QUOTED heredoc bodies, before the quote-aware split
# above ever runs. Fence, not fix, same character as the other residuals in
# docs/agents-workflow.md. Every deviation from real shell heredoc parsing is
# deliberately biased toward stripping LESS, because stripping MORE than the
# shell would is a false ALLOW -- a live `gh` write hidden from the scan, the
# unrecoverable failure. Three rules enforce that bias, each closing a
# fail-open found in this function's own review:
#   1. A `<<<` HERESTRING is not a heredoc and consumes no body. The operator match
#      is guarded so `<<<'EOF'` cannot be read as `<<` + `'EOF'`.
#   2. An UNQUOTED heredoc's body is tracked (passed through verbatim, still
#      scanned) but never inspected for operators, so a quoted-heredoc lookalike
#      written INSIDE it cannot start a strip.
#   3. A quoted heredoc that is never CLOSED strips nothing -- its held lines are
#      emitted at end of input. This is what keeps a lookalike token appearing in a
#      context the shell does not treat as an operator at all (most importantly,
#      inside a quoted string -- this function is line-based, not quote-aware) from
#      swallowing the remainder of the command.
# The residual after those three: a lookalike token in a non-operator context whose
# delimiter word ALSO appears alone on a later line, with a live gh write between
# them. Documented in docs/agents-workflow.md alongside the other residuals.

set -euo pipefail

CMD="${1:-}"
[ -n "$CMD" ] || exit 0

# This guard runs on EVERY Bash tool call, and the split + five greps below cost
# ~45ms each time. Any real `gh` invocation necessarily contains the substring
# `gh`, so a command without it cannot be one -- bailing here cannot weaken the
# guard and takes the common (no-`gh`) path to ~2ms.
case "$CMD" in
  *gh*) ;;
  *) exit 0 ;;
esac

strip_quoted_heredoc_bodies() {
  local mode=none delim="" strip_tabs=0 line check d
  local -a held=()
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$mode" != none ]; then
      check="$line"
      if [ "$strip_tabs" -eq 1 ]; then
        while [[ "$check" == $'\t'* ]]; do
          check="${check:1}"
        done
      fi
      # An unquoted body is emitted (it is live shell and must still be scanned);
      # a quoted body is HELD, and only discarded once the delimiter is seen.
      if [ "$mode" = unquoted ]; then
        printf '%s\n' "$line"
      else
        held+=("$line")
      fi
      if [ "$check" = "$delim" ]; then
        [ "$mode" = quoted ] && held=()
        mode=none
      fi
      continue
    fi
    printf '%s\n' "$line"
    # Both patterns below require a literal `<<`; skip the regex work without it.
    [[ "$line" == *'<<'* ]] || continue
    # Match the FIRST heredoc operator on the line. `(^|[^<])` keeps a `<<<`
    # herestring from being read as `<<` plus a quoted word (rule 1 above).
    # A QUOTED delimiter -- <<[-]'D', <<[-]"D", <<[-]\D -- opens a strippable body;
    # a bare <<[-]D matches only the second pattern and is merely tracked, so its
    # body keeps being scanned exactly as before (rule 2 above).
    if [[ "$line" =~ (^|[^<])\<\<(-)?[[:space:]]*(\'([A-Za-z_][A-Za-z0-9_]*)\'|\"([A-Za-z_][A-Za-z0-9_]*)\"|\\([A-Za-z_][A-Za-z0-9_]*)) ]]; then
      mode=quoted
      d="${BASH_REMATCH[4]}${BASH_REMATCH[5]}${BASH_REMATCH[6]}"
    elif [[ "$line" =~ (^|[^<])\<\<(-)?[[:space:]]*([A-Za-z_][A-Za-z0-9_-]*) ]]; then
      mode=unquoted
      d="${BASH_REMATCH[3]}"
    else
      continue
    fi
    # Both patterns require [A-Za-z_] to start the delimiter, so `d` is never empty.
    delim="$d"
    # BASH_REMATCH[2] is the `-` of `<<-` in both patterns: strip leading TABS (only
    # tabs, matching bash) from the closing delimiter line.
    if [ -n "${BASH_REMATCH[2]}" ]; then strip_tabs=1; else strip_tabs=0; fi
  done <<<"$1"
  # Unterminated quoted heredoc: strip nothing (rule 3 above).
  [ "${#held[@]}" -eq 0 ] || printf '%s\n' "${held[@]}"
}

# This hook runs on EVERY Bash call, so skip the fork entirely when there is no
# heredoc operator to find: with no `<<` present the function is provably an
# identity transform (it can only ever delete lines a `<<` match opened).
case "$CMD" in
  *'<<'*) CMD_SANITIZED=$(strip_quoted_heredoc_bodies "$CMD") ;;
  *) CMD_SANITIZED="$CMD" ;;
esac

# Emit $1 with every UNQUOTED occurrence of ; & | ( ) { } ` replaced by a
# newline; occurrences inside '...' or "..." (and any backslash-escaped
# character, in or out of quotes) are left untouched -- EXCEPT `$(` and a bare
# backtick inside "...", which the shell really does execute.
#
# RESIDUAL (fail-OPEN, accepted): an UNBALANCED quote leaves the tail "inside" a
# quote, so nothing after it splits -- which is the PERMISSIVE direction, not the
# conservative one. Accepted rather than fixed; rationale with the other residuals
# in docs/agents-workflow.md.
_split_unquoted() {
  local s="$1" out="" c state=none i=0 len
  len=${#s}
  while ((i < len)); do
    c="${s:i:1}"
    if [[ "$state" != "single" && "$c" == '\' ]]; then
      # A backslash escapes the next character outside quotes and inside double
      # quotes alike; inside SINGLE quotes it is literal, so that state is excluded.
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
    elif [[ "$state" == "double" && "$c" == '`' ]]; then
      # Command substitution IS live inside double quotes (only a backslash --
      # handled above -- makes a backtick literal there). Splitting here is not
      # a false positive: the shell really does run what follows.
      out+=$'\n'
    elif [[ "$state" == "double" && "$c" == '$' && "${s:i+1:1}" == '(' ]]; then
      # Same for `$( ... )`. Note a bare `(` inside double quotes is NOT a split
      # point (it is literal there) -- that is what keeps a quoted grep
      # alternation like "(gh issue create)" from being denied.
      out+=$'\n'
      i=$((i + 1))
    elif [[ "$state" == "none" && ';&|(){}`' == *"$c"* ]]; then
      out+=$'\n'
    else
      out+="$c"
    fi
    i=$((i + 1))
  done
  printf '%s' "$out"
}

SEG=$(_split_unquoted "$CMD_SANITIZED")

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
# No segment sits at a `gh` command position -- the remaining four greps would all
# be operating on an empty string. (`gh` appears inside ordinary words like
# "through"/"highlight", so the substring pre-filter above lets plenty through here.)
[ -n "$GH" ] || exit 0
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
