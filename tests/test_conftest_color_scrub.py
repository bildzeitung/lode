"""Regression test for conftest.py's ambient colour/tty env-var scrub (lode-kq4v).

lode-kq4v: an ambient ``FORCE_COLOR=3`` sitting in a `/land` landing session's shell environment
-- not set anywhere in this repo -- reddened 6 tests in ``tests/test_cli.py`` on an
otherwise-unmodified ``trunk``. ``lode.cli``'s module-level ``console``/``err_console`` freeze
their ``color_system`` at construction, which for a module-level singleton is IMPORT time, and
``FORCE_COLOR`` flips the ``is_terminal`` that decision is derived from (lode-xgaa; the precise
mechanism, including which attributes are frozen and which are live, is in ``tests/conftest.py``
next to the scrub itself). With a `/land` isolation-replay loop that at the time trusted a red
``nox -s tests`` as attributable to whichever branch it had just merged, this was one
bounce-and-delete-the-branch away from destroying reviewed work for a variable this repo never
sets.

This module exercises the scrub END TO END via a NESTED pytest subprocess. In-process testing is
impossible: by the time any test in THIS process runs, this process's own ``conftest.py`` has
already scrubbed the vars, and setting ``FORCE_COLOR`` from inside a test cannot un-freeze an
already-constructed ``Console``. Only a fresh subprocess gets its own ``conftest.py`` import, and
so its own independent run of the scrub, against an environment polluted BEFORE it started.

NON-VACUITY. There is no live "sabotage the subject" seam to automate -- the subject is
``conftest.py``'s own import-time code, and the point of the nested subprocess is that this outer
process cannot reach into that other process's ``conftest.py`` at runtime. So the check is manual,
and this is the recipe that actually reproduces (verified during lode-kq4v's technical review):

    comment out the four ``os.environ.pop(...)`` lines in ``tests/conftest.py``, then run
    ``pytest tests/test_conftest_color_scrub.py``

The test below then fails, carrying the nested run's own
``AssertionError: assert '\\x1b[' not in ...`` at ``tests/test_cli.py`` -- the exact symptom
lode-kq4v reported. Restoring the pops makes it pass again. No ambient ``FORCE_COLOR`` is needed:
the helper sets it for the subprocess explicitly.

Note the recipe must leave the four pop *statements'* names alone. An earlier version of this
docstring said to replace a scrub ``for`` loop "with an empty iterable"; that raised
``NameError`` on the loop variable's ``del`` and killed conftest import outright, so the outer run
died during collection and never reached the assertion it claimed to demonstrate. The scrub is
four straight ``pop`` calls now, so commenting them out is both the simplest sabotage and a real
one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

#: Anchored on ``__file__``, never ``Path.cwd()`` -- same reason ``conftest.py``'s
#: ``_CHECKOUT_ROOT`` is, and the same convention as ``tests/test_validate_mermaid_gate.py`` and
#: ``tests/test_code_concurrency_cap.py``. A cwd-relative node id here makes the nested run exit 4
#: ("file or directory not found") for a plain ``pytest`` invoked from a subdirectory, and the
#: failure then reads as "the scrub is broken" -- a false red of exactly the kind this ticket
#: exists to eliminate.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: One of the six tests lode-kq4v found reddened by an ambient FORCE_COLOR -- any single one
#: proves the mechanism (they fail and pass together, per lode-kq4v's own repro); picked for being
#: fast and self-contained. The other five are ordinary members of ``nox -s tests``, which now
#: always runs with the scrub active.
_CANARY_TEST = "tests/test_cli.py::test_config_output_has_no_ansi_when_piped"

#: Comfortably above the ~15-20s this nested run measures unloaded, while staying under the outer
#: ``@pytest.mark.timeout`` below so a genuinely wedged subprocess surfaces as a legible
#: ``TimeoutExpired`` from here rather than as an opaque outer kill. Both are raised above
#: ``pyproject.toml``'s global ``timeout = 120`` because this test runs a whole nested pytest
#: session while competing with 7 xdist siblings -- at the default cap it would itself become a
#: load-sensitive false red.
_NESTED_TIMEOUT_S = 240


@pytest.mark.timeout(300)
def test_canary_passes_with_force_color_set_in_ambient_env() -> None:
    """``tests/conftest.py``'s scrub neutralizes an ambient ``FORCE_COLOR=3`` before the canary
    test's module (and so ``lode.cli``) is ever imported -- reproducing lode-kq4v's exact incident
    and proving it is fixed.

    No "control" companion asserting the canary also passes with ``FORCE_COLOR`` *unset*: that
    case is the scrub doing nothing, it asserts the identical ``returncode == 0``, and the outer
    ``nox -s tests`` already runs that very canary in a scrubbed environment every single pass.
    It cannot distinguish a working scrub from a broken one -- confirmed during lode-kq4v's
    technical review, where it stayed green under the sabotage above while this test went red --
    so it bought no coverage for a second ~15-20s nested pytest session on the landing gate.
    Contrast ``test_cli_console.py``'s control, which earns its keep by asserting a *different*
    value (``is False`` against ``is True``).
    """
    env = dict(os.environ)
    env["FORCE_COLOR"] = "3"
    # -p no:cacheprovider keeps the nested run off .pytest_cache state shared with the outer run;
    # -p no:xdist keeps it single-process -- a direct, minimal repro of what lode-kq4v observed,
    # with nothing about xdist's worker fan-out in the way.
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
            _CANARY_TEST,
        ],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_NESTED_TIMEOUT_S,
        check=False,  # a red nested run is this test's own assertion, not an exception
    )
    assert result.returncode == 0, (
        "canary test failed under FORCE_COLOR=3 despite tests/conftest.py's scrub "
        f"-- stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
