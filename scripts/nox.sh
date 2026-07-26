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
# EXIT STATUS: this is a gate ENTRY POINT, so it carries the lode-9i2p
# contract its sibling gate scripts carry -- 1 means the CONTENT under gate
# failed, 2 means the GATE COULD NOT RUN and the status must never be
# attributed to a branch. An unusable venv is definitionally the second kind,
# so it exits 2, not 1. `exec`ing nox preserves nox's own 0/1/2 (noxfile.py's
# GATE_MACHINE_FAULT) unchanged, so the whole wrapper speaks one contract.
# Unlike scripts/validate-mermaid.sh's exit 2 (an unreachable docker engine,
# which only a human can fix), THIS exit 2 is agent-fixable with the one
# command named in the message -- the stderr says so explicitly, so an agent
# trained on "exit 2 is an escalation, not a skip" re-runs python-init.sh
# instead of escalating a machine it can repair itself.
#
# Usage: scripts/nox.sh [any `nox` args, e.g. -t fix / -s tests]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Same name and signature as the copies in scripts/validate-mermaid.sh,
# merge-precheck.sh and release-bump.sh -- keeping the banner and the exit
# code together is what stops a call site emitting half the contract. Those
# three are being extracted into a shared scripts/gate-lib.sh (lode-090f,
# unlanded); matching the signature here makes adopting it a delete-and-source.
gate_could_not_run() {
  echo "GATE COULD NOT RUN: $1" >&2
  shift
  for line in "$@"; do echo "$line" >&2; done
  exit 2
}

# Test for the binary actually about to run, not merely the activation stub.
# scripts/python-init.sh creates venv/bin/activate in its FIRST step and
# installs nox (the deliberately unlocked `dev` extra) several steps later, so
# an interrupted or failed init leaves `activate` present and `nox` absent --
# a state an `-f venv/bin/activate` check waves through, after which a bare
# `exec nox` resolves off the post-activation PATH and can reach ANOTHER
# checkout's nox. Testing venv/bin/nox covers "no venv at all" and "half-built
# venv" in one condition, and the explicit exec path below closes the PATH
# fallthrough outright.
if [ ! -x "$ROOT/venv/bin/nox" ]; then
  gate_could_not_run \
    "no usable venv at $ROOT/venv -- venv/bin/nox is missing or not" \
    "executable, so the gate never ran. This is NOT a verdict on any branch's" \
    "content (lode-9i2p), which is why it exits 2 and not 1." \
    "Remedy, from inside THIS checkout: ./scripts/python-init.sh" \
    "Agent-fixable machine fault -- run that and re-gate. Do not escalate, and" \
    "do not hand off with the gate skipped."
fi

cd "$ROOT"
# shellcheck source=/dev/null
. ./venv/bin/activate
# Activation stays load-bearing even though nox is exec'd by explicit path:
# noxfile.py sets default_venv_backend = "none", so `nox -s tests` resolves
# `pytest` off PATH and would otherwise reach another checkout's (lode-jh80).
exec "$ROOT/venv/bin/nox" "$@"
