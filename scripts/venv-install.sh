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
#   3. the local package itself PLUS the dev extra, editable, resolved FRESH
#      from pyproject.toml -- deliberately NOT locked. Already-installed,
#      hash-locked runtime deps satisfy pyproject.toml's ranges, so this step
#      only resolves/installs the dev-only packages on top -- but `-e` is
#      REQUIRED: a plain (non-editable) `.[dev]` here would install a frozen
#      build-time copy instead of the editable one (the wrong-source-tree
#      guard, tests/conftest.py, lode-jh80, exists to catch exactly that).
#
#      An earlier revision ran `uv pip install -e . --no-deps` as its own
#      step before this one, on the theory that skipping it might let this
#      step re-resolve (and un-pin) the runtime deps step 2 just hash-
#      verified. Reproduced and refuted (lode-xo99): with or without that
#      extra step, the installed package set, versions, the lock's runtime
#      pins, and the resolved `lode` source path came out byte-for-byte
#      identical -- this step's own resolution already leaves already-
#      installed, range-satisfying pins untouched, `--no-deps` or not, and
#      it discards and rebuilds the editable `lode` install either way
#      (visible in the install log as "Uninstalled 1 package" / "~ lode=="
#      when the extra step ran first, vs "+ lode==" when it didn't). The
#      separate step bought nothing but ~1.5s of dead work, so it's gone.
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
    uv pip install -e '.[dev]'
}
