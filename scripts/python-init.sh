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
# --require-hashes is set), so this is a three-step install:
#
#   1. hash-verified runtime deps, from requirements.lock
#   2. the local package itself, editable, --no-deps (its runtime deps were
#      already satisfied by step 1 -- --no-deps keeps this step from
#      re-resolving them unhashed, which would defeat the lock)
#   3. the dev extra, resolved FRESH from pyproject.toml -- deliberately NOT
#      locked (epic lode-g274 OQ#1: dev-tool drift is not this lock's job;
#      the gates themselves, run at HEAD, are the backstop for it). Already
#      installed, hash-locked runtime deps satisfy pyproject.toml's ranges,
#      so this step only resolves/installs the dev-only packages -- but it
#      MUST repeat `-e`: a plain (non-editable) `.[dev]` here would silently
#      overwrite step 2's editable install with a frozen build-time copy,
#      which is exactly the failure the wrong-source-tree guard
#      (tests/conftest.py, lode-jh80) exists to catch.
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
pip install -U uv
uv pip install -U pip

if [ "$UNLOCKED" -eq 1 ]; then
    uv pip install -e '.[dev]'
else
    uv pip install --require-hashes -r requirements.lock
    uv pip install -e . --no-deps
    uv pip install -e '.[dev]'
fi
