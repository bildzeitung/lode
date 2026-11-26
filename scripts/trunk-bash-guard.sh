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
# The command is split on ; & | ( ) { } backtick and newlines with no regard for quoting, and
# segment_hits below finds each segment's command word. scripts/shell-quote-split.sh is
# deliberately not used: a quote-aware split would turn `bash -c 'cd x; git commit'` into a miss,
# and that library denies past its scan cap, which this guard must never do.
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

QUOTES="[\"']"
CMD="${1:-}"

# This runs on every Bash call, so leave before the first fork when no hit is possible: a hit
# needs a word that reads `git` or `nox` once quotes are removed.
case "${CMD//$QUOTES/}" in
  *git* | *nox*) ;;
  *) exit 0 ;;
esac

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

# One candidate segment per line, after joining backslash-newline continuations.
SEGMENTS="$(printf '%s\n' "${CMD//$'\\\n'/ }" | tr ';&|(){}`' '\n')"
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
