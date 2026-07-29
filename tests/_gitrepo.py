"""Shared `_git` test helper for driving real throwaway git repos (lode-863q).

`_git(repo, *args)` was defined byte-identically (modulo the parameter name
`repo` vs `cwd`) in six shell-script test modules: test_isolation_guard.py,
test_land_merge_one.py, test_merge_precheck.py,
test_recycled_worktree_guard.py, test_release_bump.py,
test_release_latest_tag.py. A fix to its error reporting or timeout used to
have to land in every copy at once -- the same failure class
`scripts/recycled-worktree-guard.sh`'s four-copy inline-bash duplication had
before it was extracted into one script (lode-ivth), and the same reason
`tests/_hookharness.py` exists one tier up, after three copies of ITS harness
started to drift (lode-zlg8).

`_init_repo`/`_add_worktree` are deliberately NOT hoisted here. Their
differences across modules -- a real `origin` remote vs none, branching off
`origin/trunk` vs bare `trunk`, an optional `foreign_commit` kwarg, different
fixture file content and commit messages -- encode the different contracts of
the script each module drives; flattening them into one parametrized helper
would likely be a net loss (lode-863q). Each module keeps its own.

One pair IS byte-identical -- `test_release_latest_tag.py` and
`test_release_bump.py` -- and is left alone anyway: two copies of a fixture
premise sits under the three-copy bar both precedents above fired at, and a
helper imported by only 2 of the 6 modules would leave two different
`_init_repo`s sharing one name across this directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result
