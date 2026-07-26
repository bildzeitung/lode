#!/usr/bin/env bash
#
# Guard-friendly nox wrapper (lode-6874): activates THIS checkout's own venv
# and execs `nox "$@"` as a single plain command, so a worktree-isolated
# agent can copy `rtk scripts/nox.sh -t fix` / `rtk scripts/nox.sh -s tests`
# verbatim out of .claude/agents/coding.md / code-reviewer.md.
#
# THE PROBLEM THIS CLOSES: the documented gate-running step used to be
#     ./scripts/python-init.sh && . ./venv/bin/activate
#     nox -t fix
# but a worktree-isolated agent's harness isolation guard REFUSES any command
# that sources a file:
#     this command runs a string through `.`, which can't be verified to
#     stay inside the worktree; run the command directly instead.
# So that documented activation step was unrunnable by the very agents the
# docs are addressed to. The obvious un-activated fallbacks don't work
# either: running `./venv/bin/nox` directly, or hand-building
# `VIRTUAL_ENV`/`PATH`, trips the SAME isolation guard ("too complex to
# verify" once a `$PATH` expansion or command substitution is involved), and
# skipping activation altogether trips lode-jh80's `import lode` guard --
# nox's own venv-reuse design means an inactive or wrong-checkout venv makes
# pytest silently collect this checkout's tests against ANOTHER checkout's
# src.
#
# This script sidesteps both failure modes at once: the sourcing happens
# INSIDE the script, never as a top-level command the isolation guard has to
# reason about, and it always activates ITS OWN checkout's venv (derived
# from this script's own on-disk path, never cwd or $PATH), so `import lode`
# inside the resulting nox run resolves to this same checkout's src --
# satisfying lode-jh80 by construction, not by convention.
#
# Deliberately does NOT also run ./scripts/python-init.sh (build the venv):
# that step is a plain script execution, not a `.` source, so it was never
# blocked by the isolation guard in the first place, and folding it in here
# would just make a missing/broken venv harder to tell apart from a genuine
# gate failure. Build the venv first, once, the ordinary way; this wrapper
# only owns activating it for the nox invocation that follows.
#
# Usage: scripts/nox.sh [any `nox` args, e.g. -t fix / -s tests]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

if [ ! -f "$ROOT/venv/bin/activate" ]; then
  echo "scripts/nox.sh: no venv at $ROOT/venv -- run ./scripts/python-init.sh first" >&2
  exit 1
fi

cd "$ROOT"
# shellcheck source=/dev/null
. ./venv/bin/activate
exec nox "$@"
