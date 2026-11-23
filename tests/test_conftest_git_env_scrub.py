"""conftest.py drops git's repository-locating env vars for every subprocess (lode-a50si)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _gitrepo import _commit_file, _git, os_env

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A test that builds a scratch repo and runs a script against it with no ``env=``. The script
#: reaches ``git reset --hard``, which writes the index: the only way a ``GIT_INDEX_FILE`` left
#: in the environment shows.
_CANARY = (
    "tests/test_recycled_worktree_guard.py"
    "::test_contaminated_worktree_is_rescued_reset_onto_origin_trunk_and_cleaned"
)

#: Under ``pyproject.toml``'s global per-test ``timeout = 120``, so a wedged nested run surfaces
#: as a ``TimeoutExpired`` from here, not as an outer kill. The run measures about 2 s.
_NESTED_TIMEOUT_S = 90


def test_script_under_test_acts_on_scratch_repo_not_decoy(tmp_path: Path) -> None:
    """A nested pytest run with GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE aimed at a decoy repo
    still passes a test that runs a script against a scratch repo, and leaves the decoy's
    index alone. Without the scrub in ``tests/conftest.py`` the script's git calls follow
    GIT_DIR and that test fails; with only GIT_INDEX_FILE left in place it passes and the
    script rewrites the decoy's index. Sabotage: comment out the ``for _name in
    _local_env_vars()`` loop there, or have it skip GIT_INDEX_FILE.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q", "-b", "trunk")
    _git(decoy, "config", "user.name", "t")
    _git(decoy, "config", "user.email", "t@t")
    _commit_file(decoy, "decoy.txt", "x\n", "decoy")
    index_before = _git(decoy, "ls-files", "-s").stdout
    env = os_env()
    env["GIT_DIR"] = str(decoy / ".git")
    env["GIT_WORK_TREE"] = str(decoy)
    env["GIT_INDEX_FILE"] = str(decoy / ".git" / "index")
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
        timeout=_NESTED_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert _git(decoy, "ls-files", "-s").stdout == index_before
