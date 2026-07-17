#!/usr/bin/env bash
#
# Enumerate the `blocks`-dependents of a ticket -- the tickets that will
# unblock prematurely if <id> closes without being re-pointed (lode-verb).
#
# `/land`'s Bounce section (`.claude/skills/land/SKILL.md`) supersedes a
# bounced/escalated ticket with a fresh rebuild ticket via `bd supersede`,
# which CLOSES the original. Any OTHER ticket that depends on the original
# via a `blocks` edge (e.g. a diagnosis spike gating its follow-ups) then
# reads that blocker as satisfied and unblocks -- against work that still
# sits unbuilt in the rebuild. Bounce re-points each such dependent onto the
# rebuild instead, so it stays blocked until the rebuild actually lands.
#
# This script derives that dependent set via `bd show <id> --json
# --include-dependents`. The `--include-dependents` flag is REQUIRED
# (lode-v4rk, re-confirmed here): `bd show`'s `.dependents` array is
# populated ONLY with that opt-in flag ("may be slow on hub beads" per `bd
# show --help") -- without it, `dependent_count` can be non-zero while
# `.dependents` is absent from the JSON entirely, and a derivation built on
# `.dependents` silently iterates zero times. Three sibling checks
# (scripts/epic-children-closed.sh's callers) hit the identical bug and
# failed silently SAFE (a missed epic-completion flag reads the same as "not
# complete yet"). This site is worse: a dropped re-point fails silently
# UNSAFE -- the dependent unblocks and /code's fan-out can dispatch a builder
# onto it before the rebuild it actually needs even exists.
#
# Usage: scripts/blocks-dependents.sh <id>
#
# Prints the id of each ticket that depends on <id> via a `blocks` edge, one
# per line, in the order `bd show` returns them. Prints nothing if there are
# none. Exit code is always 0 -- this is a query, not an assertion; the
# caller (Bounce) decides what to do with an empty result (nothing to
# re-point) same as a non-empty one.
#
# Read-only: this script only ever calls `bd show`, never a bd write. The
# caller (/land Bounce) is the one that runs `bd dep add`.

set -euo pipefail

id="${1:?usage: blocks-dependents.sh <id>}"

bd show "$id" --json --include-dependents |
  jq -r '.[0].dependents[]? | select(.dependency_type=="blocks") | .id'
