"""conftest.py drops git's repository-locating env vars for every subprocess (lode-a50si)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _gitrepo import _commit_file, _git

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A script-running module: its tests build scratch repos and run a script that calls git.
_CANARY = "tests/test_recycled_worktree_guard.py"


@pytest.mark.timeout(300)
def test_script_under_test_acts_on_scratch_repo_not_decoy(tmp_path: Path) -> None:
    """A nested pytest run with GIT_DIR/GIT_WORK_TREE aimed at a decoy repo still passes a
    module that runs a script against scratch repos. Without the scrub in ``tests/conftest.py``
    the script's git calls follow GIT_DIR and the module fails. Sabotage: comment out the
    ``for _name in _local_env_vars()`` loop there.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q", "-b", "trunk")
    _git(decoy, "config", "user.name", "t")
    _git(decoy, "config", "user.email", "t@t")
    _commit_file(decoy, "decoy.txt", "x\n", "decoy")
    env = dict(os.environ)
    env["GIT_DIR"] = str(decoy / ".git")
    env["GIT_WORK_TREE"] = str(decoy)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "-q",
            _CANARY,
        ],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
