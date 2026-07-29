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
#      The pip refresh (`uv pip install -U pip`) is NOT dead work like the
#      step lode-xo99 deleted from step 3 below -- unlike that no-op, this
#      one DOES change what's installed (pip's own version: e.g. 26.1.1 ->
#      26.1.2 from ensurepip's bundle was the only diff in an otherwise
#      byte-identical `uv pip list --format=freeze` with vs. without it,
#      lode-hfaz, reproduced). Nothing downstream ever calls the venv's pip
#      again -- no `venv/bin/pip` / `python -m pip` invocation anywhere in
#      scripts/, noxfile.py, or any workflow -- so the ONLY beneficiary is
#      pip's own self-emitted "a new release is available" console notice on
#      a LATER `pip install -U uv`. Every path that creates the venv FRESH --
#      python-init.sh's first run, every CI leg (no `./venv` is cached
#      across runs, only the model-weights cache is), and update-deps.sh's
#      rebuild_venv (always `rm -rf ./venv` first) -- starts from
#      ensurepip's bundled pip regardless of whether this step ran before,
#      so the notice fires there either way and this step buys nothing on
#      any of those paths. It only pays off re-running python-init.sh a
#      SECOND time against a `./venv` that survived from a prior run:
#      verified directly -- `python -m venv` on an EXISTING venv directory
#      does not reset an already-upgraded pip back to the bundled version, so
#      a prior run's upgrade is what suppresses the notice on that repeat.
#      Narrow, but real -- kept. Full record:
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
