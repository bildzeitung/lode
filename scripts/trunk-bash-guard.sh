#!/usr/bin/env bash
#
# PreToolUse(Bash) guard body (lode-21sxk): ask for confirmation before a Bash call that runs
# `git commit`, `git merge`, `git push` or `nox` from a cwd whose branch is `trunk`.
#
# Companion to scripts/trunk-write-guard.sh (lode-p8zl), which covers the Edit|Write tools. That
# hook cannot see a commit or a `nox -t fix` reformat issued through Bash, which is the lode-6wgc
# step-6 window: a worktree destroyed mid-session drops the agent's cwd back onto the main
# checkout on trunk, where `git commit` succeeds against trunk. Scope is exactly those commands;
# every other Bash call on trunk is silent (the main session lives on trunk), and file writes
# through Bash (python/sed/heredoc) stay uncovered by maintainer ruling.
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
# strip_quoted first blanks what the shell does not run as a command: the contents of quoted
# strings, heredoc bodies and comments. Two exceptions stay visible, because they ARE commands:
# a quoted string that is the argument of `-c` (any `-[a-z]*c`, so `bash -lc` too) or of `eval`, and
# the body of a heredoc whose command line names bash, sh, dash or zsh. A double-quoted string that
# holds `$(` is kept as well. The result is split on ; & | ( ) { } backtick and newlines, and
# segment_hits below finds each segment's command word. scripts/shell-quote-split.sh is
# deliberately not used: it denies past its scan cap, which this guard must never do. Whenever the
# scan cannot pair a quote or find a heredoc terminator, the text from there on stays unstripped.
#
# KNOWN LIMITS:
#   - FALSE ASKS: a kept string is matched like a command, so `python3 -c '...`nox -t fix`...'`
#     and `bd update x --notes "$(echo; nox)"` ask on trunk. So does `command -v nox`, and a
#     `git commit` that runs in a scratch repo reached by anything but a leading `cd` literal.
#   - MISSES: a wrapper option that takes a separate value word (`uv run --with x nox`,
#     `sudo -u me git push`) hides the command behind that word. A command built by variable
#     expansion, or a script that itself commits, is not seen. A command inside backticks within
#     double quotes, `echo "`git commit`"`, is blanked with the string. A heredoc fed to a shell
#     through a pipe (`cat <<E | bash`) or by a word written after the delimiter (`<<E bash`) is
#     not recognised as one.
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

QUOTES="[\"']"
CMD="${1:-}"

# This runs on every Bash call, so leave before the first fork when no hit is possible: a hit
# needs a word that reads `git` or `nox` once quotes are removed.
case "${CMD//$QUOTES/}" in
  *git* | *nox*) ;;
  *) exit 0 ;;
esac

STOP="[\\\\'\"<#"$'\n]'
SHELLS=" bash sh dash zsh "
# A string is code, not text, when it directly follows `-c` (or `bash -lc`) or `eval`.
ARGC='(^|[[:space:]])(-[A-Za-z]*c|eval)[[:space:]]*$'
HEREDOC="^<<(-?)[[:blank:]]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\\2"
STRIPPED=""

# Does the command line being read (the text of $1 after its last separator) name a shell?
feeds_shell() {
  local seg="${1##*[;&|(){\}\`]}" tok
  seg="${seg##*$'\n'}"
  for tok in $seg; do
    tok="${tok//$QUOTES/}"
    [[ "$SHELLS" == *" ${tok##*/} "* ]] && return 0
  done
  return 1
}

# Sets STRIPPED: $1 with quoted contents, heredoc bodies and comments blanked (see MATCHING).
strip_quoted() {
  local rest="$1" out="" pre ch rest2 body c line found e
  local -a pend=()
  while [ -n "$rest" ]; do
    pre="${rest%%$STOP*}"
    out+="$pre"
    rest="${rest:${#pre}}"
    [ -n "$rest" ] || break
    ch="${rest:0:1}"
    case "$ch" in
      '\')
        out+="${rest:0:2}"
        rest="${rest:2}"
        ;;
      "'")
        rest2="${rest:1}"
        if [[ "$rest2" == *\'* ]]; then
          body="${rest2%%\'*}"
          rest="${rest2#*\'}"
          if [[ "$out" =~ $ARGC ]]; then out+="'$body'"; else out+="''"; fi
        else
          out+="$rest"
          rest=""
        fi
        ;;
      '"')
        rest2="${rest:1}"
        body=""
        found=0
        while :; do
          pre="${rest2%%[\"\\\\]*}"
          [ "$pre" = "$rest2" ] && break
          body+="$pre"
          rest2="${rest2:${#pre}}"
          c="${rest2:0:1}"
          if [ "$c" = '"' ]; then
            found=1
            rest2="${rest2:1}"
            break
          fi
          body+="${rest2:0:2}"
          rest2="${rest2:2}"
        done
        if [ "$found" = 1 ]; then
          rest="$rest2"
          if [[ "$out" =~ $ARGC || "$body" == *'$('* ]]; then out+="\"$body\""; else out+='""'; fi
        else
          out+="$rest"
          rest=""
        fi
        ;;
      '<')
        if [[ "$rest" == '<<<'* ]]; then
          out+="<<<"
          rest="${rest:3}"
        elif [[ "$rest" =~ $HEREDOC ]]; then
          e="${BASH_REMATCH[0]}"
          if feeds_shell "$out"; then c=keep; else c=drop; fi
          pend+=("$c:${BASH_REMATCH[1]}:${BASH_REMATCH[3]}")
          out+="$e"
          rest="${rest:${#e}}"
        else
          out+="<"
          rest="${rest:1}"
        fi
        ;;
      '#')
        c="${out: -1}"
        case "$c" in
          '' | ' ' | $'\t' | $'\n' | ';' | '&' | '|' | '(')
            if [[ "$rest" == *$'\n'* ]]; then rest=$'\n'"${rest#*$'\n'}"; else rest=""; fi
            ;;
          *)
            out+="#"
            rest="${rest:1}"
            ;;
        esac
        ;;
      $'\n')
        out+=$'\n'
        rest="${rest:1}"
        for e in "${pend[@]}"; do
          body=""
          found=0
          rest2="$rest"
          while [ -n "$rest2" ]; do
            line="${rest2%%$'\n'*}"
            if [ "$line" = "$rest2" ]; then rest2=""; else rest2="${rest2#*$'\n'}"; fi
            c="$line"
            [[ "$e" == *:-:* ]] && c="${line#"${line%%[!$'\t']*}"}"
            if [ "$c" = "${e##*:}" ]; then
              found=1
              break
            fi
            body+="$line"$'\n'
          done
          [ "$found" = 1 ] || break
          [[ "$e" == keep:* ]] && out+="$body"
          rest="$rest2"
        done
        pend=()
        ;;
    esac
  done
  STRIPPED="$out"
}

segment_hits() {
  local -a toks
  # shellcheck disable=SC2206
  toks=(${1//$QUOTES/})
  local i n=${#toks[@]} tok
  for ((i = 0; i < n; i++)); do
    tok="${toks[i]}"
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
      commit | merge | push) return 0 ;;
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

# One candidate segment per line of the stripped text, after joining backslash-newline continuations.
strip_quoted "$CMD"
SEGMENTS="$(printf '%s\n' "${STRIPPED//$'\\\n'/ }" | tr ';&|(){}`' '\n')"
any_segment_hits <<<"$SEGMENTS" || exit 0

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

printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "lode-21sxk: this Bash call runs git commit/merge/push or nox from a checkout on trunk. The STOP banner in CLAUDE.md requires every change to go through a worktree -- confirm this is a sanctioned exception (e.g. the doc-only --no-verify path the Workflow gotchas section of CLAUDE.md describes) before proceeding. A dispatched subagent should treat a prompt here as a signal its worktree is gone (lode-6wgc) and stop rather than approve; a human at the terminal can approve and continue. A live /land lock exempts the lander itself."}}'
exit 0
