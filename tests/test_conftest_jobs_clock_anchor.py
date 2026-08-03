"""Regression test for conftest.py's ``_reset_jobs_clock_anchor`` autouse fixture (lode-x10m).

lode-x10m: ``tests/test_cli.py``'s two ``work --wait`` tests USED TO do
``monkeypatch.setattr(cli.time, "monotonic", _fake_monotonic)``. Because ``src/lode/cli.py``
does a plain ``import time``, ``cli.time`` IS the shared ``time`` module object, so that patch
was PROCESS-GLOBAL and reached ``src/lode/jobs.py``'s ``now()``, which ratchets the module-global
``jobs._now_epoch`` forward by hours-to-days. ``monkeypatch`` reverts ``time.monotonic`` but not
``_now_epoch`` -- which it never patched -- so the poisoned anchor outlived the test and every
later test in that ``pytest-xdist`` worker read a clock days ahead of the wall clock.

lode-e8lo then fixed that leak at its source: both tests now rebind the *name* ``time`` inside
``lode.cli`` (``tests/test_cli.py``'s ``_patch_cli_clock_past_deadline``) and never touch the
shared module object. READ THE NON-VACUITY SECTION BELOW BEFORE TRUSTING THIS FILE -- that fix
also disarmed the repro the first test here is built on.

WHY THIS TEST EXISTS AT ALL. The fixture it pins is invisible infrastructure: nothing imports it,
nothing names it, and deleting it turns nothing red on its own. Its absence shows up only as an
intermittent red in ``nox -s tests`` -- the gate every producer and every ``/land`` pass runs --
on branches that cannot possibly have caused it. lode-x10m recorded that failure mode arriving
for real: a ``/land`` pass certified trunk on a run that passed, then the identical tree failed
on the very next run of the same command. A pin that costs one nested pytest session is cheap
against a landing gate that is silently a coin toss.

WHAT IS PINNED, and by which half:

- ``test_poisoner_no_longer_flips_its_victims`` runs lode-x10m's own canonical three-test repro
  in ONE nested process, poisoner first. This WAS the end-to-end mechanism check and the
  regression check lode-x10m's notes explicitly asked to keep; post-lode-e8lo it is a smoke
  check only -- see NON-VACUITY below.
- ``test_the_anchor_reset_fixture_is_armed_for_every_test`` is the cheap in-process half: it
  catches the fixture being renamed, deleted, or losing ``autouse=True`` immediately, without
  paying for a subprocess.

NON-VACUITY -- MEASURED, and NO LONGER HOLDING for the nested half. lode-x10m's technical review
measured this by reverting ``tests/conftest.py`` to its pre-fix state and re-running the command
below verbatim:

    pre-lode-e8lo test_cli.py, fixture body removed -> 2 failed, 1 passed
    pre-lode-e8lo test_cli.py, fixture in place     -> 3 passed

The two failures were ``assert 1 == 0`` (``test_reset_leaves_future_failed_alone``) and
``assert 1 is None`` (``test_claim_respects_future_next_attempt_at``) -- rows that are NOT yet due
reading as overdue, which is the poisoned clock's signature.

lode-e8lo's own build re-ran BOTH arms against the current tree and measured:

    post-lode-e8lo test_cli.py, fixture body removed -> 3 passed
    post-lode-e8lo test_cli.py, fixture in place     -> 3 passed

So ``test_poisoner_no_longer_flips_its_victims`` is now VACUOUS: with the poisoner defused at its
source there is nothing left for the fixture to save, and that test passes whether or not its
subject exists. It is kept as an end-to-end smoke check that the two defences still hold
TOGETHER, not as a pin on either one -- it can only fail if both break at once. The live pin on
the fixture is ``test_the_anchor_reset_fixture_is_armed_for_every_test`` below, unaffected by
lode-e8lo because it asserts autouse membership rather than behaviour. Restoring a genuinely
non-vacuous behavioural pin needs a way to run the nested repro with the fixture disabled, and is
filed as lode-up8x. Do not delete this test to "clean up" ahead of that -- it is currently the
only end-to-end coverage of the pair.

Deliberately NOT asserted: that ``jobs._now_epoch`` equals its sentinel at test-body entry. In a
fresh process the anchor already starts at that sentinel, so the assertion passes with or without
the fixture unless a poisoner happened to run first in the same worker -- i.e. it would be
order-dependent, which is the exact property this ticket exists to remove from the suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

#: Anchored on ``__file__``, never ``Path.cwd()`` -- same convention as
#: ``tests/test_conftest_color_scrub.py`` and ``tests/conftest.py``'s own ``_CHECKOUT_ROOT``. A
#: cwd-relative node id makes the nested run exit 4 ("file or directory not found") for a plain
#: ``pytest`` invoked from a subdirectory, and that reads as "the fixture is broken" -- a false
#: red of exactly the kind lode-x10m exists to eliminate.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: lode-x10m's canonical deterministic repro, in this order: the (since-defused, lode-e8lo)
#: poisoner first, then two victims. Order WAS the whole point pre-lode-e8lo; with the poisoner
#: no longer poisoning it no longer distinguishes anything -- see NON-VACUITY in the module
#: docstring before reasoning about what a red here would mean.
_REPRO = (
    "tests/test_cli.py::test_work_wait_times_out_naming_outstanding_jobs",
    "tests/test_worker.py::test_reset_leaves_future_failed_alone",
    "tests/test_worker.py::test_claim_respects_future_next_attempt_at",
)

#: The nested run measures ~2-4s unloaded (three tests, one process). Raised well above that, and
#: above ``pyproject.toml``'s global ``timeout = 120``, because it competes with 7 xdist siblings;
#: at the default cap it would itself become the load-sensitive false red this ticket is about.
#: Kept under the outer ``@pytest.mark.timeout`` so a wedged subprocess surfaces as a legible
#: ``TimeoutExpired`` from here rather than as an opaque outer kill.
_NESTED_TIMEOUT_S = 150


@pytest.mark.timeout(210)
def test_poisoner_no_longer_flips_its_victims() -> None:
    """Running the poisoner ahead of its two victims in one process must not fail them.

    Single-process and serial on purpose: lode-x10m's own investigation established that neither
    ``-n 8`` nor machine load is required to reproduce, and that ``--dist loadfile`` hides the bug
    by pinning each file to one worker. A nested run with nothing about xdist's fan-out in the way
    is the minimal, deterministic form of the defect.

    Since lode-e8lo this passes for two independent reasons (the seam is fixed AND the fixture
    resets the anchor), so it can no longer fail when only one of them breaks -- see NON-VACUITY
    in the module docstring, and lode-up8x.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            # Keep the nested run off .pytest_cache state shared with the outer run, and
            # single-process so the three node ids stay in the order given.
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            # pytest-randomly is not currently installed, and pytest accepts ``-p no:`` for an
            # absent plugin. Named anyway because this test's subject IS an ordering effect:
            # were that plugin ever added, it would shuffle these three node ids and make this
            # test intermittent -- reintroducing, in the pin itself, the flake it guards against.
            "-p",
            "no:randomly",
            "-q",
            *_REPRO,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_NESTED_TIMEOUT_S,
        check=False,  # a red nested run is this test's own assertion, not an exception
    )
    assert result.returncode == 0, (
        "jobs._now_epoch is no longer isolated: running the test_cli.py poisoner ahead of its "
        "test_worker.py victims flipped them. BOTH defences must have broken to get here -- "
        "check test_cli.py's _patch_cli_clock_past_deadline still rebinds the name `time` "
        "rather than setting an attribute on the shared module (lode-e8lo), AND that "
        "tests/conftest.py's _reset_jobs_clock_anchor still resets the anchor (lode-x10m) -- "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_the_anchor_reset_fixture_is_armed_for_every_test(
    request: pytest.FixtureRequest,
) -> None:
    """The reset must reach every test by being autouse, not by being requested.

    This is what makes the guarantee independent of ``--dist`` mode and worker count: the reset
    runs per TEST, so no scheduling decision can place a victim where it does not reach. A
    narrower fixture that tests had to opt into would protect only the three victims lode-x10m
    happened to find.
    """
    assert "_reset_jobs_clock_anchor" in request.fixturenames, (
        "the autouse jobs-clock anchor reset is not active for this test -- it was renamed, "
        "deleted, or lost autouse=True in tests/conftest.py (lode-x10m)"
    )
