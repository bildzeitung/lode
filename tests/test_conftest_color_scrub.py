"""Regression test for conftest.py's ambient colour/tty env-var scrub (lode-kq4v).

lode-kq4v: an ambient ``FORCE_COLOR=3`` sitting in a `/land` landing session's shell environment
-- not set anywhere in this repo (``grep -rn FORCE_COLOR`` over the tree, excluding ``venv/``,
returns nothing) -- reddened 6 tests in ``tests/test_cli.py`` on an otherwise-unmodified
``trunk``, because rich's ``Console()`` freezes its ``is_terminal``/``no_color`` decision at
CONSTRUCTION, which for ``lode.cli``'s module-level ``console``/``err_console`` singletons is
IMPORT time (lode-xgaa; see also ``src/lode/cli.py``'s ``console`` docstring and
``tests/test_cli_console.py``). ``FORCE_COLOR`` overrides pytest's captured (non-tty) stdout the
same way a real terminal would, so every test asserting plain/uncoloured CLI output failed --
with a `/land` isolation-replay loop that (at the time) trusted a red ``nox -s tests`` as
attributable to whichever branch it had just merged, this was one bounce-and-delete-the-branch
away from destroying reviewed work for a variable this repo never sets.

The fix is a scrub of ``FORCE_COLOR``/``NO_COLOR``/``TTY_COMPATIBLE``/``TTY_INTERACTIVE`` at the
very top of ``tests/conftest.py``, as plain top-level module code (not an autouse fixture -- see
that file for why a fixture would be the exact no-op trap lode-xgaa already documented for
``monkeypatch.setenv`` after import).

This module exercises that scrub END TO END, via a NESTED pytest subprocess. Testing it
in-process is not possible: by the time any test in THIS (outer) pytest process runs, this
process's own ``conftest.py`` has already scrubbed the vars and ``lode.cli`` has (if any earlier
test imported it) already been constructed under that scrubbed environment -- setting
``FORCE_COLOR`` now, from inside a test, cannot retroactively un-scrub anything or un-freeze an
already-constructed ``Console``. Only a FRESH subprocess -- which gets its OWN ``conftest.py``
import, hence its own independent run of the scrub -- can prove the scrub neutralizes an ambient
``FORCE_COLOR`` that was already set BEFORE that subprocess (and so its own ``conftest.py``) ever
started, exactly reproducing lode-kq4v's incident.

NON-VACUOUSNESS, demonstrated by sabotaging the subject and re-verified manually during
lode-kq4v's build: temporarily replacing ``tests/conftest.py``'s scrub loop with a no-op (an
empty iterable) makes ``test_canary_passes_with_force_color_set_in_ambient_env`` below FAIL --
with the exact same ``AssertionError: assert '\\x1b[' not in ...`` lode-kq4v originally reported
-- while the control test still passes; restoring the scrub makes both pass again. Not automated
as a permanent in-repo revert-and-check: there is no live "sabotage the subject" seam here the
way ``test_cli_console.py`` has for flipping a constructor kwarg on the shared ``console`` object
-- the subject IS ``conftest.py``'s own top-level scrub code, and the whole point of testing it
via a nested subprocess is that this outer process cannot see or affect that other process's
``conftest.py`` at runtime. Same manual-verification posture ``test_console_highlight_is_disabled``
already uses for an assertion with no such live toggle.
"""

from __future__ import annotations

import os
import subprocess
import sys

#: One of the six tests lode-kq4v found reddened by an ambient FORCE_COLOR -- any single one is
#: enough to prove the mechanism (they all fail/pass together, per lode-kq4v's own repro); picked
#: for being fast and self-contained. The full set of six is already covered non-vacuously by
#: existing manual verification (this module's docstring) plus the fact that they are ordinary
#: members of `nox -s tests`, which now always runs with the scrub active.
_CANARY_TEST = "tests/test_cli.py::test_config_output_has_no_ansi_when_piped"


def _run_canary_under(*, force_color: str | None) -> subprocess.CompletedProcess[str]:
    """Run the canary test in a FRESH nested pytest subprocess with ``FORCE_COLOR`` controlled.

    A fresh subprocess re-imports ``tests/conftest.py`` (and so re-runs its module-level env
    scrub) from scratch, under an environment this helper controls precisely -- the only way to
    exercise that import-time scrub, which nothing in the OUTER pytest process can do (see
    module docstring).
    """
    env = dict(os.environ)
    if force_color is None:
        env.pop("FORCE_COLOR", None)
    else:
        env["FORCE_COLOR"] = force_color
    # -p no:cacheprovider keeps the nested run from touching .pytest_cache state shared with the
    # outer run; -p no:xdist keeps it single-process -- a direct, minimal repro of the exact
    # failure lode-kq4v observed, with nothing about xdist's own worker-fanout in the way.
    return subprocess.run(
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
        capture_output=True,
        text=True,
        timeout=120,
        check=False,  # asserted on explicitly by the caller (a non-zero return is the point)
    )


def test_canary_passes_with_force_color_set_in_ambient_env() -> None:
    """``tests/conftest.py``'s scrub neutralizes an ambient ``FORCE_COLOR=3`` before the canary
    test's module (and so ``lode.cli``) is ever imported -- reproducing lode-kq4v's exact
    incident and proving it is fixed."""
    result = _run_canary_under(force_color="3")
    assert result.returncode == 0, (
        "canary test failed under FORCE_COLOR=3 despite tests/conftest.py's scrub "
        f"-- stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_canary_passes_with_force_color_unset() -> None:
    """Control for the assertion above: the canary test also passes with ``FORCE_COLOR`` absent
    from the ambient env, so the positive-path test above is not vacuously green from a canary
    that would pass regardless of whether the scrub does anything at all."""
    result = _run_canary_under(force_color=None)
    assert result.returncode == 0, (
        "canary test failed with FORCE_COLOR unset (control) "
        f"-- stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
