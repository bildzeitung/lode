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
# CONTRACT (rulings recorded once, in docs/decisions.md under lode-8uh71 -- not restated here):
#   - Gate on the BRANCH; decide "ask", never "deny".
#   - Reads tool_input.command, so unlike trunk-write-guard.sh it needs jq; the settings.json
#     wrapper denies every Bash call when jq is missing, like the three older Bash guards
#     (lode-oii9). This script itself takes the command as $1 and uses no jq.
#   - LANDER EXEMPTION: /land runs unattended on trunk in the main checkout and legitimately
#     merges, commits (--no-verify), pushes and re-gates there, so an ask would stall every
#     pass. The guard therefore stays silent when the branch is trunk AND `land-lock.sh held`
#     (scripts/land-lock.sh) reports a LIVE single-lander lock; a stale lock does not exempt.
#     ACCEPTED LIMIT: while a land pass holds the lock, any other agent whose cwd falls back to
#     trunk is exempt too. This deliberately bends lode-8uh71's "never tell a subagent from the
#     main session" point.
#
# MATCHING is conservative (a false ask is cheap, a miss is what this exists to stop): the command
# is split on ; & | ( ) { } backtick and newlines; in each segment env assignments, option words,
# numeric words, shell keywords (if, then, do, !, ...) and wrappers (env, sudo, uv run, python -m,
# bash, eval, xargs, timeout, ...) are skipped and the first remaining word must be `nox` (any
# path) or `git` followed, past its global options, by commit, merge or push.
#
# KNOWN LIMITS:
#   - FALSE ASKS: quoted text and heredoc bodies are split and matched like commands, so
#     `bd update x --notes "gates: fix green; nox -s unit green"` asks on trunk. So does
#     `command -v nox`.
#   - MISSES: a wrapper option that takes a separate value word (`uv run --with x nox`,
#     `sudo -u me git push`) hides the command behind that word. A command built by variable
#     expansion, or a script that itself commits, is not seen.
#   - CWD: only a LEADING `cd <dir>` (ended by &&, ; or a newline) is followed when picking the
#     cwd whose branch is checked, and only when <dir> is a literal path or starts with `~`; a
#     target that is not an existing directory (a variable, a typo), a later cd, `git -C <dir>`
#     and a `bash -c` string hiding a cd are all judged against the hook's own cwd.
#
# Usage: scripts/trunk-bash-guard.sh "<the Bash command>"
# Prints nothing (allow) or one PreToolUse hookSpecificOutput JSON object with permissionDecision
# "ask". ALWAYS exits 0: a PreToolUse hook exiting non-zero is itself a defect.

set -uo pipefail
# No pathname expansion: segment_hits word-splits the command text unquoted.
set -f

CMD="${1:-}"
[ -n "$CMD" ] || exit 0

# Split into candidate segments, one per line, after joining backslash-newline continuations.
SEGMENTS="$(printf '%s\n' "${CMD//$'\\\n'/ }" | tr ';&|(){}`' '\n')"

segment_hits() {
  local seg="$1" tok base sub
  local -a toks
  seg="${seg//\"/}"
  seg="${seg//\'/}"
  # shellcheck disable=SC2206
  toks=($seg)
  local i=0 n=${#toks[@]}
  while [ "$i" -lt "$n" ]; do
    tok="${toks[$i]}"
    base="${tok##*/}"
    case "$tok" in
      -* | *=* | [0-9]* | '!') ;;
      *)
        case "$base" in
          env | command | exec | sudo | time | nohup | uv | uvx | run | python* | \
            bash | sh | dash | zsh | eval | xargs | timeout | \
            if | then | else | elif | while | until | do) ;;
          *) break ;;
        esac
        ;;
    esac
    i=$((i + 1))
  done
  [ "$i" -lt "$n" ] || return 1
  base="${toks[$i]##*/}"
  case "$base" in
    nox) return 0 ;;
    git)
      i=$((i + 1))
      while [ "$i" -lt "$n" ]; do
        sub="${toks[$i]}"
        case "$sub" in
          -C | -c | --git-dir | --work-tree | --namespace) i=$((i + 2)) ;;
          -*) i=$((i + 1)) ;;
          commit | merge | push) return 0 ;;
          *) return 1 ;;
        esac
      done
      ;;
  esac
  return 1
}

hit=0
while IFS= read -r seg; do
  if segment_hits "$seg"; then
    hit=1
    break
  fi
done <<<"$SEGMENTS"
[ "$hit" = 1 ] || exit 0

# The cwd the command will run in: the hook's own, unless it opens with `cd <dir>` ended by
# &&, ; or a newline.
DIR="."
LEADING_CD='^[[:space:]]*cd[[:space:]]+("([^"]*)"|'"'([^']*)'"'|([^[:space:];&|]+))[[:blank:]]*(&&|;|'$'\n'')'
if [[ "$CMD" =~ $LEADING_CD ]]; then
  cand="${BASH_REMATCH[2]}${BASH_REMATCH[3]}${BASH_REMATCH[4]}"
  case "$cand" in
    '~' | '~/'*) cand="${HOME:-}${cand#'~'}" ;;
  esac
  [ -d "$cand" ] && DIR="$cand"
fi

branch="$(git -C "$DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
[ "$branch" = "trunk" ] || exit 0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if (cd "$DIR" && bash "$HERE/land-lock.sh" held) >/dev/null 2>&1; then
  exit 0
fi

printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "lode-21sxk: this Bash call runs git commit/merge/push or nox from a checkout on trunk. The STOP banner in CLAUDE.md requires every change to go through a worktree -- confirm this is a sanctioned exception (e.g. the doc-only --no-verify path the Workflow gotchas section of CLAUDE.md describes) before proceeding. A dispatched subagent should treat a prompt here as a signal its worktree is gone (lode-6wgc) and stop rather than approve; a human at the terminal can approve and continue. A live /land lock exempts the lander itself."}}'
exit 0
