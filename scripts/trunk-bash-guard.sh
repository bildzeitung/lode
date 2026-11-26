#!/usr/bin/env bash
#
# PreToolUse(Bash) guard body (lode-21sxk): ask for confirmation before a Bash call that runs
# `git commit`, `git merge`, `git push`, `git rebase`, `git cherry-pick` or `nox` from a cwd whose
# branch is `trunk`.
#
# Companion to scripts/trunk-write-guard.sh (lode-p8zl), which covers the Edit|Write tools. That
# hook cannot see a commit or a `nox -t fix` reformat issued through Bash, which is the lode-6wgc
# step-6 window: a worktree destroyed mid-session drops the agent's cwd back onto the main
# checkout on trunk, where `git commit` succeeds against trunk. Scope is exactly those commands;
# every other Bash call on trunk is silent (the main session lives on trunk), and file writes
# through Bash (python/sed/heredoc) stay uncovered by maintainer ruling. `git pull` is
# deliberately NOT covered: the main session pulls on trunk routinely (lode-u4ea7).
#
# CONTRACT (the rulings, the lander exemption's rationale and its accepted limit are recorded
# once, in docs/decisions.md under lode-21sxk):
#   - Gate on the BRANCH; decide "ask", never "deny".
#   - The settings.json wrapper extracts tool_input.command with jq and passes it as $1; it
#     denies every Bash call when jq is missing, like the other Bash guards (lode-oii9).
#   - Silent on trunk while `scripts/land-lock.sh held` reports a LIVE single-lander lock, so an
#     unattended /land pass is not stalled. A stale lock does not exempt.
#
# MATCHING is conservative, because a false ask is cheap and a miss is what this exists to stop.
# strip_quoted first takes out what the shell does not run as a command: heredoc bodies and
# comments go, and a quoted string that is text becomes one word (each space and separator in it
# is replaced), so `git -C "a b" commit` keeps its word positions and `"$ROOT/.venv/bin/nox"`
# still reads as nox. Three things stay as written because they ARE commands, or hold one: a
# quoted string that is the argument of `-c` (any `-[a-z]*c`, so `bash -lc` too) or of `eval`,
# unless it reads like git's `-c section.key=value`; the body of a heredoc whose command line
# names bash, sh, dash or zsh; and a double-quoted string that holds `$(`. The result is split on
# ; & | ( ) { } backtick and newlines, and segment_hits below finds each segment's command word.
# scripts/shell-quote-split.sh is deliberately not used: it denies past its scan cap, which this
# guard must never do. This is not a shell parser, and a quote it pairs wrongly would hide a real
# command. So whenever the scan meets something it cannot pair (a quote with no partner, a heredoc
# with no terminator, a `"$(` string that an inner quote cut short), the WHOLE command is matched
# unstripped: one stray quote before that point means the pairs already made cannot be trusted.
#
# KNOWN LIMITS:
#   - FALSE ASKS: a kept string is matched like a command, so `python3 -c '...`nox -t fix`...'`
#     and `bd update x --notes "$(echo; nox)"` ask on trunk. So does `command -v nox`, and a
#     `git commit` that runs in a scratch repo reached by anything but a leading `cd` literal.
#     A command the scan gave up on is matched with its prose and heredoc bodies included.
#   - MISSES: a word before the command that segment_hits' wrapper list does not know hides it:
#     another wrapper (`nice nox`, `su me -c 'git push'`), a wrapper option's separate value word
#     (`uv run --with x nox`, `sudo -u me git push`), or a redirection written first
#     (`>log nox`). So does a `$(...)` between `git` and its subcommand. A command built by
#     variable expansion, or a script that itself commits, is not seen. A command inside backticks
#     within double quotes, `echo "`git commit`"`, or inside `$(...)` or backticks in a heredoc
#     body that no shell reads, is taken out with the text around it. Wrong pairs that still come
#     out even are not noticed: `"note :) $(echo "x"; git push)"`, or a comment the scan does not
#     see as one (`#` straight after `)`) holding two apostrophes around a command.
#   - NOT MATCHED: `git pull` (also `pull --rebase`), left out by ruling (above); and `git reset`,
#     `git stash`, `git am` and `git revert`, which no ruling has put in scope.
#   - MID-REBASE: while a rebase of trunk is stopped (a conflict, an `edit` step) HEAD is detached,
#     so the branch reads `HEAD`, not `trunk`, and every command is silent, `git rebase --continue`
#     and `git commit` included. A stopped cherry-pick keeps HEAD on trunk and still asks.
#     Whether to close this is lode-3cxzs.
#   - CWD: only a LEADING `cd <dir>` (ended by &&, ; or a newline) is followed when picking the
#     cwd whose branch is checked, and only when <dir> is a literal path or starts with `~`; a
#     target that is not an existing directory (a variable, a typo), a later cd, `git -C <dir>`
#     and a `bash -c` string hiding a cd are all judged against the hook's own cwd.
#
# Usage: scripts/trunk-bash-guard.sh "<the Bash command>"
# Prints nothing (allow) or one PreToolUse hookSpecificOutput JSON object with permissionDecision
# "ask". ALWAYS exits 0: a PreToolUse hook exiting non-zero is itself a defect.

set -uo pipefail
# No pathname expansion: segment_hits and feeds_shell word-split command text unquoted.
set -f
# Byte-wise string handling: every character the scan looks for is ASCII, and strip_quoted is
# several times faster on a long command without multibyte matching.
LC_ALL=C

QUOTES="[\"']"
CMD="${1:-}"

# This runs on every Bash call, so leave before the first fork when no hit is possible: a hit
# needs a word that reads `git` or `nox` once quotes are removed.
case "${CMD//$QUOTES/}" in
  *git* | *nox*) ;;
  *) exit 0 ;;
esac

STOP="[\\\\'\"<#"$'\n]'
# A string is code, not text, when it directly follows `-c` (or `bash -lc`) or `eval` ...
ARGC='(^|[[:space:]])(-[A-Za-z]*c|eval)[[:space:]]*$'
# ... unless it reads `section.key=value`: that is git's own `-c`, and no shell word starts so.
GIT_CONFIG='^[A-Za-z][-A-Za-z0-9]*\.[^[:space:]=]*='
HEREDOC="^<<(-?)[[:blank:]]*([^[:space:];&|<>()]+)"
DELIM_QUOTING="[\"'\\\\]"
# The characters that end a word or a command.
WORD_BREAK='[[:space:];&|(){}<>`]'
SEGMENT_BREAK='[;&|(){}`]'

# Does the command line $1 name a shell anywhere on it?
feeds_shell() {
  local tok
  for tok in ${1//$WORD_BREAK/ }; do
    tok="${tok//$QUOTES/}"
    case "${tok##*/}" in
      bash | sh | dash | zsh) return 0 ;;
    esac
  done
  return 1
}

# Reads a string that honours backslash escapes off the front of strip_quoted's `rest`, up to
# its closing quote $1: sets `body`, leaves `rest` after the quote. Fails when there is none.
close_escaped() {
  local stop="[$1\\\\]" pre
  body=""
  while :; do
    pre="${rest%%$stop*}"
    [ "$pre" = "$rest" ] && return 1
    body+="$pre"
    rest="${rest:${#pre}}"
    if [ "${rest:0:1}" = "$1" ]; then
      rest="${rest:1}"
      return 0
    fi
    body+="${rest:0:2}"
    rest="${rest:2}"
  done
}

# Sets STRIPPED: $1 with strings, heredoc bodies and comments that are text, not commands, made
# harmless (see MATCHING). Fails, setting nothing, when it meets something it cannot pair.
strip_quoted() {
  local rest="$1" out="" pre ch body line word keep opens closes
  local -a pend=()
  while [ -n "$rest" ]; do
    pre="${rest%%$STOP*}"
    out+="$pre"
    rest="${rest:${#pre}}"
    ch="${rest:0:1}"
    case "$ch" in
      '\')
        out+="${rest:0:2}"
        rest="${rest:2}"
        ;;
      "'" | '"')
        rest="${rest:1}"
        if [ "$ch" = '"' ] || [ "${out: -1}" = '$' ]; then
          close_escaped "$ch" || return 1
        else
          [[ "$rest" == *\'* ]] || return 1
          body="${rest%%\'*}"
          rest="${rest:${#body}+1}"
        fi
        if [ "$ch" = '"' ] && [[ "$body" == *'$('* ]]; then
          # Unbalanced parentheses mean a quote inside the `$(` ended the string early.
          opens="${body//[^(]/}"
          closes="${body//[^)]/}"
          [ "${#opens}" = "${#closes}" ] || return 1
        elif [[ ! "$out" =~ $ARGC || "$body" =~ $GIT_CONFIG ]]; then
          body="${body//$WORD_BREAK/_}"
        fi
        out+="$ch$body$ch"
        ;;
      '<')
        if [[ "$rest" == '<<<'* ]]; then
          out+="<<<"
          rest="${rest:3}"
        elif [[ "$rest" =~ $HEREDOC ]]; then
          # One pend entry per heredoc: `-` or `.` (leading tabs stripped or not), then the
          # delimiter with its quoting removed.
          word="${BASH_REMATCH[2]}"
          pend+=("${BASH_REMATCH[1]:-.}${word//$DELIM_QUOTING/}")
          out+="${BASH_REMATCH[0]}"
          rest="${rest:${#BASH_REMATCH[0]}}"
        elif [[ "$rest" == '<<'* ]]; then
          return 1
        else
          out+="<"
          rest="${rest:1}"
        fi
        ;;
      '#')
        case "${out: -1}" in
          '' | ' ' | $'\t' | $'\n' | ';' | '&' | '|' | '(')
            pre="${rest%%$'\n'*}"
            rest="${rest:${#pre}}"
            ;;
          *)
            out+="#"
            rest="${rest:1}"
            ;;
        esac
        ;;
      $'\n')
        keep=0
        if [ "${#pend[@]}" -gt 0 ] && feeds_shell "${out##*$'\n'}"; then keep=1; fi
        out+=$'\n'
        rest="${rest:1}"
        for word in "${pend[@]}"; do
          body=""
          while :; do
            [ -n "$rest" ] || return 1
            line="${rest%%$'\n'*}"
            rest="${rest:${#line}+1}"
            pre="$line"
            [ "${word:0:1}" = - ] && pre="${line#"${line%%[!$'\t']*}"}"
            [ "$pre" = "${word:1}" ] && break
            body+="$line"$'\n'
          done
          [ "$keep" = 1 ] && out+="$body"
        done
        pend=()
        ;;
    esac
  done
  STRIPPED="$out"
}

segment_hits() {
  local -a toks
  local seg="${1//[<>]/ }"
  # shellcheck disable=SC2206
  toks=(${seg//$QUOTES/})
  local i n=${#toks[@]} tok
  for ((i = 0; i < n; i++)); do
    tok="${toks[i]#\\}"
    case "$tok" in
      -* | *=* | [0-9]* | '!') continue ;;
    esac
    case "${tok##*/}" in
      env | command | exec | sudo | time | nohup | uv | uvx | run | python* | \
        bash | sh | dash | zsh | eval | xargs | timeout | \
        if | then | else | elif | while | until | do) ;;
      nox) return 0 ;;
      git) break ;;
      *) return 1 ;;
    esac
  done
  for ((i++; i < n; i++)); do
    case "${toks[i]}" in
      -C | -c | --git-dir | --work-tree | --namespace) i=$((i + 1)) ;;
      -*) ;;
      commit | merge | push | rebase | cherry-pick) return 0 ;;
      *) return 1 ;;
    esac
  done
  return 1
}

any_segment_hits() {
  local seg
  while IFS= read -r seg; do
    segment_hits "$seg" && return 0
  done
  return 1
}

# One candidate segment per line, after joining backslash-newline continuations. No fork here:
# this much runs on every Bash call that mentions git or nox at all.
strip_quoted "$CMD" || STRIPPED="$CMD"
STRIPPED="${STRIPPED//$'\\\n'/ }"
any_segment_hits <<<"${STRIPPED//$SEGMENT_BREAK/$'\n'}" || exit 0

# The cwd the command will run in: the hook's own, unless it opens with `cd <dir>` ended by
# &&, ; or a newline.
DIR="."
dq='"([^"]*)"'
sq="'([^']*)'"
bare='([^[:space:];&|]+)'
LEADING_CD="^[[:space:]]*cd[[:space:]]+($dq|$sq|$bare)[[:blank:]]*(&&|;|"$'\n'")"
if [[ "$CMD" =~ $LEADING_CD ]]; then
  cand="${BASH_REMATCH[2]}${BASH_REMATCH[3]}${BASH_REMATCH[4]}"
  case "$cand" in
    \~ | \~/*) cand="${HOME:-}${cand#\~}" ;;
  esac
  [ -d "$cand" ] && DIR="$cand"
fi

branch="$(git -C "$DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
[ "$branch" = "trunk" ] || exit 0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
(cd "$DIR" && bash "$HERE/land-lock.sh" held) >/dev/null 2>&1 && exit 0

printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "lode-21sxk: this Bash call runs git commit/merge/push/rebase/cherry-pick or nox from a checkout on trunk. The STOP banner in CLAUDE.md requires every change to go through a worktree -- confirm this is a sanctioned exception (e.g. the doc-only --no-verify path the Workflow gotchas section of CLAUDE.md describes) before proceeding. A dispatched subagent should treat a prompt here as a signal its worktree is gone (lode-6wgc) and stop rather than approve; a human at the terminal can approve and continue. A live /land lock exempts the lander itself."}}'
exit 0
