#!/usr/bin/env bash
#
# Discards any working-tree churn to the beads passive exports
# (`.beads/issues.jsonl`, `.beads/interactions.jsonl`) by checking them back
# out of HEAD. Called from the `Stop` hook in `.claude/settings.json` --
# extracted out of that JSON string (lode-do3q) so the list of passive-export
# relpaths lives in exactly one place, `scripts/beads-passive-exports.txt`,
# shared with `scripts/worktree-gc-classify.sh`'s dirty-tree guard and
# `tests/test_land_lock.py`'s stall-hook scan exclusion.
#
# Usage: discard-beads-passive-export-churn.sh <repo-root>
#
# Deliberately forgiving: a missing repo root, a missing list file, or a
# checkout failure (e.g. the path was never committed yet) must never fail
# the Stop hook itself -- this is best-effort hygiene, not a gate. Every
# failure mode is swallowed and the script always exits 0.

root="${1:-}"
[ -n "$root" ] || exit 0

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
list="$script_dir/beads-passive-exports.txt"
[ -r "$list" ] || exit 0

paths=()
while IFS= read -r rel; do
  [ -n "$rel" ] && paths+=("$rel")
done < "$list"

[ "${#paths[@]}" -gt 0 ] || exit 0

git -C "$root" checkout HEAD -- "${paths[@]}" 2>/dev/null
exit 0
