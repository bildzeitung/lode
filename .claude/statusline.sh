#!/bin/bash
# lode project statusline: active bd ticket + git + model + token usage.
# Claude Code pipes a JSON status payload on stdin and renders our stdout.
# Overrides the global statusline (~/.claude/statusline-command.sh) within lode.

input=$(cat)

cwd=$(echo "$input" | jq -r '.cwd // .workspace.current_dir // empty')
model=$(echo "$input" | jq -r '.model.display_name // empty')
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
remaining=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty')

# --- Active bd ticket -------------------------------------------------------
# `bd list` costs ~0.85s; the statusline re-renders far too often to pay that
# synchronously. So read from a short-lived cache and refresh it in the
# background: every render stays instant, the ticket lags reality by <1 cycle.
bd_part=""
if [ -n "$cwd" ] && [ -d "$cwd/.beads" ]; then
    cache="${TMPDIR:-/tmp}/lode-statusline-bd.cache"
    ttl=5
    now=$(date +%s)
    mtime=0
    [ -f "$cache" ] && mtime=$(stat -c %Y "$cache" 2>/dev/null || echo 0)
    if [ $((now - mtime)) -ge "$ttl" ]; then
        # Reset mtime first so the next few renders (within the ~0.85s bd takes)
        # don't each spawn their own refresh; then refresh detached.
        touch "$cache"
        ( bd -C "$cwd" list --status=in_progress --json 2>/dev/null > "$cache.new" \
            && mv -f "$cache.new" "$cache" || rm -f "$cache.new" ) >/dev/null 2>&1 &
    fi
    if [ -s "$cache" ]; then
        bd_part=$(jq -r '
            (sort_by(.started_at) | reverse) as $s
            | if ($s | length) == 0 then empty
              else
                ($s[0].title // "") as $t
                | ($t | if length > 38 then .[0:37] + "…" else . end) as $tt
                | "▶ \($s[0].id) \($tt)"
                  + (if ($s | length) > 1 then " (+\(($s | length) - 1))" else "" end)
              end
        ' "$cache" 2>/dev/null)
    fi
fi

# --- Git (short form) -------------------------------------------------------
git_part=""
if [ -n "$cwd" ] && git -C "$cwd" rev-parse --git-dir > /dev/null 2>&1; then
    branch=$(git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null || git -C "$cwd" rev-parse --short HEAD 2>/dev/null)
    status=$(git -C "$cwd" status --porcelain 2>/dev/null)
    if [ -z "$status" ]; then
        git_status="clean"
    else
        changed=$(echo "$status" | grep -v '^??' | wc -l | tr -d ' ')
        untracked=$(echo "$status" | grep '^??' | wc -l | tr -d ' ')
        git_status=""
        [ "$changed" -gt 0 ] && git_status="${changed}~"
        [ "$untracked" -gt 0 ] && git_status="${git_status}${untracked}?"
    fi
    git_part="git:${branch} ${git_status}"
fi

# --- Model + tokens ---------------------------------------------------------
model_part=""
[ -n "$model" ] && model_part="$model"

tokens_part=""
tokens_remaining_part=""
[ -n "$used" ] && tokens_part="$(printf '%.0f' "$used")% used"
[ -n "$remaining" ] && tokens_remaining_part="$(printf '%.0f' "$remaining")% left"

# --- Assemble (ticket first), skipping empties ------------------------------
parts=()
[ -n "$bd_part" ] && parts+=("$bd_part")
[ -n "$git_part" ] && parts+=("$git_part")
[ -n "$model_part" ] && parts+=("$model_part")
[ -n "$tokens_part" ] && parts+=("$tokens_part")
[ -n "$tokens_remaining_part" ] && parts+=("$tokens_remaining_part")

IFS=' | '
printf '%s' "${parts[*]}"
