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
#
#      Do NOT delete the pip refresh (`uv pip install -U pip`) as dead work
#      like the step lode-xo99 removed: it is cosmetic, not dead (lode-hfaz)
#      -- it does bump the venv's own pip, which suppresses pip's "a new
#      release is available" notice the next time this step's own opening
#      `pip install -U uv` runs. That pays off only on a repeat
#      python-init.sh against a surviving ./venv; every from-scratch venv
#      starts from ensurepip's bundle regardless. Full record:
#      docs/stack.md#dependency-locking-lode-g2741.
#   2. hash-verified runtime deps, from LOCKFILE
#   3. the local package itself PLUS the dev extra, editable, resolved FRESH
#      from pyproject.toml -- deliberately NOT locked. Already-installed,
#      hash-locked runtime deps satisfy pyproject.toml's ranges, so this step
#      only resolves/installs the dev-only packages on top -- but `-e` is
#      REQUIRED: a plain (non-editable) `.[dev]` here would install a frozen
#      build-time copy instead of the editable one (the wrong-source-tree
#      guard, tests/conftest.py, lode-jh80, exists to catch exactly that).
#
#      Do NOT re-add a separate `uv pip install -e . --no-deps` step before
#      this one: measured as a pure no-op (lode-xo99) -- same package set,
#      same runtime pins, same resolved source path with or without it,
#      since this step discards and rebuilds the editable install either
#      way. Full record: docs/stack.md#dependency-locking-lode-g2741.
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
