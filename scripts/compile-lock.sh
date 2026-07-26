#!/bin/bash -e
#
# The SINGLE implementation of lode's lock-gen command (lode-sys4). Every
# caller that needs to resolve requirements.lock from pyproject.toml runs
# THIS script rather than keeping its own copy of the `uv pip compile`
# invocation -- there is exactly one place the command string lives:
#   - .github/workflows/tests.yml's `lock-currency` job
#   - scripts/update-deps.sh (both the whole-set and --package flows)
#   - noxfile.py's `lock_currency` session (the local pre-flight, run by
#     /land's re-gate -- .claude/skills/land/SKILL.md)
#
# WHY THIS EXISTS (lode-gyag / lode-sys4 root cause): `uv pip compile` does
# NOT read .python-version -- it resolves against whichever interpreter it
# discovers (a dev box can auto-pick a DIFFERENT Python than CI's, e.g. 3.11
# vs CI's 3.14), and some transitive deps carry python-version markers
# (lancedb's `overrides`, anyio's marker-gated `typing_extensions`) that only
# resolve on some interpreters and not others. Generating the lock against
# the wrong interpreter produces a DIFFERENT, but still internally valid,
# lock -- and CI's lock-currency job (which recompiles for 3.14 and diffs)
# then flapped red against a lock someone generated locally on 3.11.
#
# THE FIX: pass --python-version explicitly, derived from .python-version --
# the single source of truth for lode's target interpreter (do NOT
# hard-code a version literal here or in any caller; .python-version is
# already the one place this repo keeps it, per this ticket's own
# constraint).
#
# Usage: scripts/compile-lock.sh <extra uv-pip-compile args...>
#   e.g. scripts/compile-lock.sh -o requirements.lock
#        scripts/compile-lock.sh --upgrade -q -o "$CANDIDATE"
#        scripts/compile-lock.sh --upgrade-package foo -q -o "$CANDIDATE"
# `pyproject.toml`, `--generate-hashes`, and `--python-version <ver>` are
# always supplied by this script; the caller passes only what varies
# (--upgrade / --upgrade-package / -q / -o PATH / ...).
#
# FAILS CLOSED if `uv` is not on PATH -- a silently-skipped lock check is
# worse than a noisy one (this repo's stance elsewhere: a broken gate must
# never look like a passed one, see docs/decisions.md's mermaid-gate exit-2
# note). CI's lock-currency job installs uv itself first, so this only bites
# a developer machine or /land's local pre-flight without uv installed; the
# public CI badge still catches a stale lock in that case, just later.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

if ! command -v uv >/dev/null 2>&1; then
  echo "compile-lock.sh: 'uv' not found on PATH -- cannot generate/verify" >&2
  echo "compile-lock.sh: requirements.lock (fails closed, lode-sys4)." >&2
  echo "compile-lock.sh: install uv (pip install -U uv) and re-run." >&2
  exit 1
fi

PYVER="$(cat .python-version)"
exec uv pip compile pyproject.toml --generate-hashes --python-version "$PYVER" "$@"
