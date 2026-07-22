#!/bin/bash
#
# Sourced by scripts/python-init.sh and scripts/update-deps.sh -- the single
# copy of the locked venv install sequence (lode-02xy). Before this, the two
# scripts carried this block byte-for-byte, with nothing binding them: a
# future 4th install step would need editing in both, silently drifting if
# it wasn't.
#
# Assumes the target venv already exists and is the ACTIVE one -- creating
# and activating it is the CALLER's job, since that step differs between
# callers (python-init.sh assumes no venv exists yet; update-deps.sh's
# rebuild_venv first deactivates and `rm -rf`s a stale one).
#
# install_locked_venv LOCKFILE
#   1. refresh uv itself, and pip inside the venv
#   2. hash-verified runtime deps, from LOCKFILE
#   3. the local package itself, editable, --no-deps (its runtime deps were
#      already satisfied by step 2 -- --no-deps keeps this step from
#      re-resolving them unhashed, which would defeat the lock)
#   4. the dev extra, resolved FRESH from pyproject.toml -- deliberately NOT
#      locked. Already-installed, hash-locked runtime deps satisfy
#      pyproject.toml's ranges, so this step only resolves/installs the
#      dev-only packages -- but it MUST repeat `-e`: a plain (non-editable)
#      `.[dev]` here would silently overwrite step 3's editable install with
#      a frozen build-time copy (the wrong-source-tree guard,
#      tests/conftest.py, lode-jh80, exists to catch exactly that).
#
# && chained -- NOT relying on the caller's -e/errexit state -- so a failure
# partway through is never masked by a later, unrelated command's exit
# status. When a function is called inside `if ! func; then`, bash suspends
# -e for the ENTIRE function body during that call (not just the call
# site), so without this explicit chaining an earlier step failing would be
# silently skipped past, and the function would return the LAST command's
# (successful) exit status. Verified empirically while building
# scripts/update-deps.sh (lode-fdjr): a bare-statement chain reached the
# "gates green" branch even with a deliberately-failing install step.
# Chaining with `&&` makes the function's own return code reflect the FIRST
# failing step regardless of the caller's -e state, independent of how the
# function happens to be invoked.
install_locked_venv() {
  local lockfile="$1"
  pip install -U uv &&
    uv pip install -U pip &&
    uv pip install --require-hashes -r "$lockfile" &&
    uv pip install -e . --no-deps &&
    uv pip install -e '.[dev]'
}
