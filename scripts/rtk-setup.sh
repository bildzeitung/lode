#!/bin/bash -e
#
# Ensure lode's required RTK command exclusions are present.
#
# RTK 0.42.1 stores `exclude_commands` only in the user-global config
# (~/.config/rtk/config.toml); it has no project-level equivalent. lode
# nonetheless *requires* two commands to bypass RTK's rewrite so their output
# stays raw:
#
#   ^bd .+ --json            beads must emit unfiltered JSON
#   git worktree list --porcelain   worktree GC parses real porcelain, not
#                                   RTK's reformatted table
#
# This script makes that requirement reproducible on any machine: run it once
# after installing RTK. It is idempotent — re-running never duplicates entries.

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/rtk/config.toml"

if ! command -v rtk >/dev/null 2>&1; then
    echo "rtk not on PATH — install RTK first, then re-run scripts/rtk-setup.sh" >&2
    exit 0
fi

mkdir -p "$(dirname "$CONFIG")"
[ -f "$CONFIG" ] || rtk config --create >/dev/null

python3 - "$CONFIG" <<'PY'
import re
import sys
import tomllib

REQUIRED = ["^bd .+ --json", "git worktree list --porcelain"]

path = sys.argv[1]
with open(path, "rb") as fh:
    data = tomllib.load(fh)

current = data.get("hooks", {}).get("exclude_commands", [])
missing = [c for c in REQUIRED if c not in current]
if not missing:
    print(f"rtk exclusions already present in {path}")
    sys.exit(0)

merged = current + missing


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


array = "[" + ", ".join(toml_str(s) for s in merged) + "]"
src = open(path).read()

# `exclude_commands` is unique to the [hooks] table, so a targeted rewrite of
# its array is safe and leaves the rest of the config untouched.
pattern = re.compile(r"exclude_commands\s*=\s*\[.*?\]", re.DOTALL)
if pattern.search(src):
    src = pattern.sub(f"exclude_commands = {array}", src, count=1)
elif "[hooks]" in src:
    src = src.replace("[hooks]", f"[hooks]\nexclude_commands = {array}", 1)
else:
    src = src.rstrip("\n") + f"\n\n[hooks]\nexclude_commands = {array}\n"

with open(path, "w") as fh:
    fh.write(src)

print(f"added rtk exclusions to {path}: {', '.join(missing)}")
PY
