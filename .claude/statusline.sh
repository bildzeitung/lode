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
# Used TOKENS (current context: input + cache create + cache read) and the
# window size, so we compute "percent of tokens used" ourselves rather than
# leaning on .used_percentage (which reads as context occupancy and has drifted
# from current usage across versions). Keep used_percentage only as a fallback.
used_tokens=$(echo "$input" | jq -r '.context_window.total_input_tokens // empty')
window=$(echo "$input" | jq -r '.context_window.context_window_size // empty')
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')

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
            if echo "$live $total $landc" > "$wt_cache.new" \
                  && mv -f "$wt_cache.new" "$wt_cache"; then :; else rm -f "$wt_cache.new"; fi
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
        ( if bd -C "$cwd" list --json 2>/dev/null > "$cache.new" \
              && mv -f "$cache.new" "$cache"; then :; else rm -f "$cache.new"; fi ) >/dev/null 2>&1 &
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

# --- Git (fleet-aware short form) -------------------------------------------
# Beyond branch + dirty count, show how far this branch sits from its merge
# TARGET — that's the git-side analogue of the bd pipeline view. On trunk the
# target is origin/trunk; on a feature/worktree branch it's local trunk. We add
# the same freshness honesty as the bd staleness gate: an ahead/behind computed
# against origin is only as current as the last fetch, so on trunk we surface a
# "fetch:<age>" token once that knowledge starts going stale. We NEVER fetch from
# the statusline (must stay side-effect-free) — we only read FETCH_HEAD's age,
# and from the COMMON git dir, since a worktree's own git-dir has no FETCH_HEAD.
git_part=""
if [ -n "$cwd" ] && git -C "$cwd" rev-parse --git-dir > /dev/null 2>&1; then
    branch=$(git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null || git -C "$cwd" rev-parse --short HEAD 2>/dev/null)
    status=$(git -C "$cwd" status --porcelain 2>/dev/null)
    if [ -z "$status" ]; then
        git_status="clean"
    else
        changed=$(echo "$status" | grep -vc '^??')
        untracked=$(echo "$status" | grep -c '^??')
        git_status=""
        [ "$changed" -gt 0 ] && git_status="${changed}~"
        [ "$untracked" -gt 0 ] && git_status="${git_status}${untracked}?"
    fi

    # Divergence from the merge target (↑ahead ↓behind), zeros omitted.
    if [ "$branch" = "trunk" ]; then base="origin/trunk"; else base="trunk"; fi
    div=""
    if [ "$branch" != "$base" ] && git -C "$cwd" rev-parse --verify -q "$base" >/dev/null 2>&1; then
        ahead=$(git -C "$cwd" rev-list --count "${base}..HEAD" 2>/dev/null || echo 0)
        behind=$(git -C "$cwd" rev-list --count "HEAD..${base}" 2>/dev/null || echo 0)
        [ "${ahead:-0}" -gt 0 ] && div="↑${ahead}"
        [ "${behind:-0}" -gt 0 ] && div="${div:+$div }↓${behind}"
    fi

    # Fetch-age honesty: only when the target is origin/* (i.e. on trunk), and
    # only once the last fetch is old enough that the ↓behind may be understated.
    fetch_part=""
    case "$base" in
        origin/*)
            fh="$(git -C "$cwd" rev-parse --git-common-dir 2>/dev/null)/FETCH_HEAD"
            if [ -f "$fh" ]; then
                age=$(( $(date +%s) - $(stat -c %Y "$fh" 2>/dev/null || echo 0) ))
                if   [ "$age" -ge 86400 ]; then fetch_part="fetch:$((age/86400))d"
                elif [ "$age" -ge 3600 ];  then fetch_part="fetch:$((age/3600))h"
                elif [ "$age" -ge 900 ];   then fetch_part="fetch:$((age/60))m"
                fi
            fi
            ;;
    esac

    git_part="git:${branch} ${git_status}"
    [ -n "$div" ] && git_part="${git_part} ${div}"
    [ -n "$fetch_part" ] && git_part="${git_part} ${fetch_part}"
fi

# --- Model + tokens ---------------------------------------------------------
# Model: only the class matters (Opus/Sonnet/…), so keep the first word and drop
# the version + "(1M context)" tail that display_name carries.
model_part=""
[ -n "$model" ] && model_part="${model%% *}"

# Tokens: one meter instead of separate used/left numbers. A 10-cell bar fills
# with the share of tokens used (used_tokens / window), the % printed after.
# Prefer the raw counts; fall back to the pre-baked used_percentage if absent.
tokens_part=""
u=""
if [ -n "$used_tokens" ] && [ -n "$window" ] && [ "$window" -gt 0 ] 2>/dev/null; then
    u=$(( used_tokens * 100 / window ))
elif [ -n "$used" ]; then
    u=$(printf '%.0f' "$used")
fi
if [ -n "$u" ]; then
    [ "$u" -lt 0 ] && u=0; [ "$u" -gt 100 ] && u=100
    width=10
    filled=$(( u * width / 100 ))
    bar=""; i=0
    while [ "$i" -lt "$width" ]; do
        if [ "$i" -lt "$filled" ]; then bar="${bar}█"; else bar="${bar}░"; fi
        i=$((i + 1))
    done
    tokens_part="[${bar}] ${u}%"
fi

# --- Assemble (fleet first), skipping empties -------------------------------
parts=()
[ -n "$pipeline_part" ] && parts+=("$pipeline_part")
[ -n "$agents_part" ] && parts+=("$agents_part")
[ -n "$git_part" ] && parts+=("$git_part")
[ -n "$model_part" ] && parts+=("$model_part")
[ -n "$tokens_part" ] && parts+=("$tokens_part")

# Join with an explicit " | " (IFS+"${arr[*]}" would join on a single space,
# blurring the pipeline group into the agents group).
sep=""; out=""
for p in "${parts[@]}"; do out="${out}${sep}${p}"; sep=" | "; done
printf '%s' "$out"
