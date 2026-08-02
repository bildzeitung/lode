"""Regression test for conftest.py's ``_reset_jobs_clock_anchor`` autouse fixture (lode-x10m).

lode-x10m: ``tests/test_cli.py``'s two ``work --wait`` tests do
``monkeypatch.setattr(cli.time, "monotonic", _fake_monotonic)``. Because ``src/lode/cli.py``
does a plain ``import time``, ``cli.time`` IS the shared ``time`` module object, so that patch
is PROCESS-GLOBAL and reaches ``src/lode/jobs.py``'s ``now()``, which ratchets the module-global
``jobs._now_epoch`` forward by hours-to-days. ``monkeypatch`` reverts ``time.monotonic`` but not
``_now_epoch`` -- which it never patched -- so the poisoned anchor outlives the test and every
later test in that ``pytest-xdist`` worker reads a clock days ahead of the wall clock.

WHY THIS TEST EXISTS AT ALL. The fixture it pins is invisible infrastructure: nothing imports it,
nothing names it, and deleting it turns nothing red on its own. Its absence shows up only as an
intermittent red in ``nox -s tests`` -- the gate every producer and every ``/land`` pass runs --
on branches that cannot possibly have caused it. lode-x10m recorded that failure mode arriving
for real: a ``/land`` pass certified trunk on a run that passed, then the identical tree failed
on the very next run of the same command. A pin that costs one nested pytest session is cheap
against a landing gate that is silently a coin toss.

WHAT IS PINNED, and by which half:

- ``test_poisoner_no_longer_flips_its_victims`` runs lode-x10m's own canonical three-test repro
  in ONE nested process, poisoner first. This is the end-to-end mechanism check, and it is the
  regression check lode-x10m's notes explicitly asked to keep.
- ``test_the_anchor_reset_fixture_is_armed_for_every_test`` is the cheap in-process half: it
  catches the fixture being renamed, deleted, or losing ``autouse=True`` immediately, without
  paying for a subprocess.

NON-VACUITY -- MEASURED, not assumed (verified during lode-x10m's technical review by reverting
``tests/conftest.py`` to its pre-fix trunk state and re-running the command below verbatim):

    with the pre-fix conftest -> 2 failed, 1 passed
    with the fixture in place -> 3 passed

The two failures were ``assert 1 == 0`` (``test_reset_leaves_future_failed_alone``) and
``assert 1 is None`` (``test_claim_respects_future_next_attempt_at``) -- rows that are NOT yet due
reading as overdue, which is the poisoned clock's signature. Dropping the fixture body from
``tests/conftest.py`` reproduces exactly that, so this test fails when its subject is removed.

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

#: lode-x10m's canonical deterministic repro, in this order: the POISONER first, then two victims.
#: Order is the whole point -- the same three tests pass in any order once the fixture is in place,
#: and the two victims pass on their own even without it.
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
        "tests/conftest.py's _reset_jobs_clock_anchor no longer isolates jobs._now_epoch: "
        "running the test_cli.py poisoner ahead of its test_worker.py victims flipped them "
        f"(lode-x10m) -- stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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
