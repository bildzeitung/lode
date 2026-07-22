#!/bin/bash -ex
#
# Initialize the Python environment for lode.
#
# Run from the repo root. Builds ./venv and installs the Python dependency
# set.
#
# By default (lode-g274.1), installs from the committed, hash-verified
# requirements.lock -- the exact, fully-transitive RUNTIME dependency set --
# with --require-hashes, so a hash mismatch FAILS the install rather than
# merely warning. `-e .` and `--require-hashes` are mutually exclusive in a
# single pip/uv invocation (both reject an editable/unhashed requirement once
# --require-hashes is set), so this is a multi-step install -- shared with
# scripts/update-deps.sh's rebuild_venv via install_locked_venv() in
# scripts/venv-install.sh (lode-02xy; see that file for the step-by-step
# breakdown and why it's `&&`-chained), so a future step change is made once,
# not drifted between the two scripts.
#
# `--unlocked` skips the lock entirely and resolves everything fresh from
# pyproject.toml instead (the pre-lock behavior) -- the deliberate "what
# would we get today" escape hatch: regenerating requirements.lock
# (scripts/update-deps.sh, lode-g274.2) or probing an upstream bump before
# committing to it.

UNLOCKED=0
for arg in "$@"; do
    case "$arg" in
        --unlocked) UNLOCKED=1 ;;
        *)
            echo "usage: $0 [--unlocked]" >&2
            exit 1
            ;;
    esac
done

python -m venv venv
. ./venv/bin/activate

# shellcheck source=venv-install.sh
. "$(dirname "$0")/venv-install.sh"

if [ "$UNLOCKED" -eq 1 ]; then
    pip install -U uv
    uv pip install -U pip
    uv pip install -e '.[dev]'
else
    install_locked_venv requirements.lock
fi
