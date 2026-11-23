"""Shared runner for tests that exercise ``tests/conftest.py``'s import-time code (lode-hgy5h).

``conftest.py``'s import-time code runs once per interpreter, so the only way to test it is a
fresh nested pytest subprocess started from an environment polluted beforehand.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

#: Anchored on ``__file__``, never ``Path.cwd()``: node ids are repo-relative, and a cwd-relative
#: run from a subdirectory would exit 4 ("file or directory not found"), a false red that reads as
#: "the scrub is broken".
REPO_ROOT = Path(__file__).resolve().parent.parent


def run_nested_pytest(
    *node_ids: str,
    env: Mapping[str, str],
    timeout_s: float,
    extra_args: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run ``node_ids`` in a nested pytest and return the result without raising on a red run.

    ``-p no:cacheprovider`` keeps the nested run off the ``.pytest_cache`` state shared with the
    outer run; ``-p no:xdist`` keeps it single-process, so node ids run in the order given and
    nothing about xdist's worker fan-out is in the way. ``extra_args`` go before ``-q``.
    ``check=False``: a red nested run is the calling test's own assertion, not an exception.
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
