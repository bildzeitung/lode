#!/bin/bash
# lode project statusline: fleet pipeline + live agents + git + model + tokens.
# Claude Code pipes a JSON status payload on stdin and renders our stdout.
# Overrides the global statusline (~/.claude/statusline-command.sh) within lode.
#
# Design note: this line renders in EVERY lode session, but bd `in_progress`
# and agent worktrees are machine-GLOBAL, not per-session. So we deliberately
# do NOT claim "you are on ticket X" (that was stale between sessions and often
# wrong). We show an honest fleet aggregate instead: how many tickets sit at
# each pipeline stage, and how many agent worktrees are actually live. An
# in_progress count with no live agent to back it is marked stale ("?").

input=$(cat)

cwd=$(echo "$input" | jq -r '.cwd // .workspace.current_dir // empty')
model=$(echo "$input" | jq -r '.model.display_name // empty')
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
remaining=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty')

# --- Live agent worktrees (B) -----------------------------------------------
# One worktree per spawned agent under .claude/worktrees/agent-*. Branch encodes
# role (land/* = reviewer|lander, else builder); dir mtime within LIVE_TTL = live
# (stale worktrees linger for days after their agent exits). `git worktree list`
# is cheap but we cache the COMPUTED triple "live total stale" like bd below, so
# every render stays instant. `live` also gates bd staleness (see below).
wt_cache="${TMPDIR:-/tmp}/lode-statusline-wt.cache"
wt_ttl=5
live_agents=0
total_agents=0
land_live=0
if [ -n "$cwd" ] && git -C "$cwd" rev-parse --git-dir > /dev/null 2>&1; then
    now=$(date +%s)
    mtime=0
    [ -f "$wt_cache" ] && mtime=$(stat -c %Y "$wt_cache" 2>/dev/null || echo 0)
    if [ $((now - mtime)) -ge "$wt_ttl" ]; then
        touch "$wt_cache"
        (
            live=0; total=0; landc=0; nowb=$(date +%s); live_ttl=600
            p=""; b=""
            finalize() {
                case "$p" in
                    */.claude/worktrees/agent-*)
                        total=$((total + 1))
                        m=$(stat -c %Y "$p" 2>/dev/null || echo 0)
                        if [ $((nowb - m)) -lt "$live_ttl" ]; then
                            live=$((live + 1))
                            case "$b" in */land/*) landc=$((landc + 1)) ;; esac
                        fi
                        ;;
                esac
                p=""; b=""
            }
            while IFS= read -r line; do
                if [ -z "$line" ]; then finalize; continue; fi
                case "$line" in
                    "worktree "*) p=${line#worktree } ;;
                    "branch "*)   b=${line#branch } ;;
                esac
            done < <(git -C "$cwd" worktree list --porcelain 2>/dev/null)
            finalize
            echo "$live $total $landc" > "$wt_cache.new" \
                && mv -f "$wt_cache.new" "$wt_cache" || rm -f "$wt_cache.new"
        ) >/dev/null 2>&1 &
    fi
    if [ -s "$wt_cache" ]; then
        read -r live_agents total_agents land_live < "$wt_cache" 2>/dev/null
        live_agents=${live_agents:-0}; total_agents=${total_agents:-0}; land_live=${land_live:-0}
    fi
fi

# --- Fleet pipeline counts (A) ----------------------------------------------
# `bd list` costs ~0.85s; the statusline re-renders far too often to pay that
# synchronously, so read from a short-lived cache refreshed in the background.
# We cache ALL open issues (one call) and count by stage: in_progress = build,
# then the workflow labels. Zero-count stages are omitted.
pipeline_part=""
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
        ( bd -C "$cwd" list --json 2>/dev/null > "$cache.new" \
            && mv -f "$cache.new" "$cache" || rm -f "$cache.new" ) >/dev/null 2>&1 &
    fi
    if [ -s "$cache" ]; then
        counts=$(jq -r '
            def hasl($l): ((.labels // []) | index($l)) != null;
            [ ([.[] | select(.status=="in_progress")]                       | length),
              ([.[] | select(hasl("ready-for-code-review"))]                | length),
              ([.[] | select(hasl("ready-for-land"))]                       | length),
              ([.[] | select(hasl("needs-rebase"))]                         | length),
              ([.[] | select(hasl("human") or hasl("land-escalated"))]      | length)
            ] | join(" ")
        ' "$cache" 2>/dev/null)
        if [ -n "$counts" ]; then
            read -r build review land rebase human <<< "$counts"
            # in_progress with no live agent backing it is stale -> mark "?".
            stale=""; [ "${build:-0}" -gt 0 ] && [ "${live_agents:-0}" -eq 0 ] && stale="?"
            segs=()
            [ "${build:-0}"  -gt 0 ] && segs+=("build:${build}${stale}")
            [ "${review:-0}" -gt 0 ] && segs+=("review:${review}")
            [ "${land:-0}"   -gt 0 ] && segs+=("land:${land}")
            [ "${rebase:-0}" -gt 0 ] && segs+=("rebase:${rebase}")
            [ "${human:-0}"  -gt 0 ] && segs+=("!human:${human}")
            [ ${#segs[@]} -gt 0 ] && pipeline_part="${segs[*]}"
        fi
    fi
fi

# --- Live agents segment (rendered) -----------------------------------------
agents_part=""
if [ "${total_agents:-0}" -gt 0 ]; then
    agents_part="agents:${live_agents}"
    [ "${land_live:-0}" -gt 0 ] && agents_part="${agents_part} (${land_live} land)"
    idle=$((total_agents - live_agents))
    [ "$idle" -gt 0 ] && agents_part="${agents_part}, ${idle} stale"
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

# --- Assemble (fleet first), skipping empties -------------------------------
parts=()
[ -n "$pipeline_part" ] && parts+=("$pipeline_part")
[ -n "$agents_part" ] && parts+=("$agents_part")
[ -n "$git_part" ] && parts+=("$git_part")
[ -n "$model_part" ] && parts+=("$model_part")
[ -n "$tokens_part" ] && parts+=("$tokens_part")
[ -n "$tokens_remaining_part" ] && parts+=("$tokens_remaining_part")

# Join with an explicit " | " (IFS+"${arr[*]}" would join on a single space,
# blurring the pipeline group into the agents group).
sep=""; out=""
for p in "${parts[@]}"; do out="${out}${sep}${p}"; sep=" | "; done
printf '%s' "$out"
