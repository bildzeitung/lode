"""Shared runner for tests that need a fresh nested pytest session (lode-hgy5h).

A nested session imports ``tests/conftest.py`` again and starts from the environment its caller
builds: the only way to test that file's import-time code, or an autouse fixture with its
escape hatch set.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

#: Anchored on ``__file__``, never ``Path.cwd()``: node ids are repo-relative, and a cwd-relative
#: run from a subdirectory would exit 4 ("file or directory not found"), a false red.
REPO_ROOT = Path(__file__).resolve().parent.parent


def run_nested_pytest(
    *node_ids: str,
    env: Mapping[str, str],
    timeout_s: float,
    extra_args: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run ``node_ids`` in a nested pytest and return the result without raising on a red run.

    ``-p no:cacheprovider`` keeps the nested run off the ``.pytest_cache`` state shared with the
    outer run; ``-p no:xdist`` keeps it single-process, with nothing about xdist's worker fan-out
    in the way. Switching off any other plugin that reorders tests is the caller's job, through
    ``extra_args``. ``check=False``: a red nested run is the calling test's own assertion, not
    an exception.

    Build ``env`` with ``os_env()`` (``tests/_gitrepo.py``), not ``dict(os.environ)``: when the
    nested run cannot be started, pytest prints the arguments of the call that raised, and a
    plain dict shows its first variables there, values included.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            *extra_args,
            "-q",
            *node_ids,
        ],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
