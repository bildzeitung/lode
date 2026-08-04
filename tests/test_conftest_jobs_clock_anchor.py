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
shared module object. That defused the only real poisoner this repo has ever had -- which is why
the pin below (lode-up8x) no longer reuses ``test_cli.py``'s tests as its repro; see NON-VACUITY.

WHY THIS TEST EXISTS AT ALL. The fixture it pins is invisible infrastructure: nothing imports it,
nothing names it, and deleting it turns nothing red on its own. Its absence shows up only as an
intermittent red in ``nox -s tests`` -- the gate every producer and every ``/land`` pass runs --
on branches that cannot possibly have caused it. lode-x10m recorded that failure mode arriving
for real: a ``/land`` pass certified trunk on a run that passed, then the identical tree failed
on the very next run of the same command. A pin that costs one nested pytest session is cheap
against a landing gate that is silently a coin toss.

WHAT IS PINNED, and by which half:

- ``test_poisoner_no_longer_flips_its_victims`` runs a SYNTHETIC poisoner/victim pair (lode-up8x;
  see THE SYNTHETIC POISONER below) through a nested subprocess TWICE -- once with the fixture
  disabled via env var, once with it left enabled -- and asserts the two runs disagree. This is
  the end-to-end mechanism check.
- ``test_the_anchor_reset_fixture_is_armed_for_every_test`` is the cheap in-process half: it
  catches the fixture being renamed, deleted, or losing ``autouse=True`` immediately, without
  paying for a subprocess -- and, since lode-up8x added the escape hatch below, also catches that
  hatch being set ambiently, which would leave the fixture attached but resetting nothing.

THE SYNTHETIC POISONER (lode-up8x). lode-x10m's original repro chained onto ``test_cli.py``'s and
``test_worker.py``'s real tests, which was fine while ``test_cli.py`` still carried a real,
process-global leak. lode-e8lo fixed that leak at its source, so as of lode-e8lo there is no live
poisoner left anywhere in the suite to borrow -- confirmed below (OLD REPRO, FOR THE RECORD):
re-running the old repro with ``tests/conftest.py``'s fixture body replaced with ``pass`` still
passes, because nothing pokes the shared ``time`` module any more for the fixture to have anything
to undo. Reproducing the ORIGINAL mechanism (a monkeypatched, process-global
``time.monotonic()``) would also depend on the real, environment-dependent seconds-since-boot
value that made the ratchet land "hours to days" ahead in the wild -- not deterministic across
machines or containers. So this pin poisons the one thing a real poisoner actually leaves behind,
directly and deterministically: ``jobs._now_epoch`` itself, set to a sentinel far enough in the
future that only a reset (not ordinary wall-clock drift) can explain its absence in the next
reading. That is exactly the residual state a real poisoner leaves -- ``monkeypatch`` reverts
``time.monotonic``, never ``_now_epoch`` -- so undoing it is exactly what
``_reset_jobs_clock_anchor`` exists to do, whatever poisoned it.

``_SYNTHETIC_REPRO`` below names two ordinary, always-collected test functions in this same file
(``test_synthetic_poisoner_leaves_now_epoch_in_the_far_future`` then
``test_synthetic_victim_reads_a_sane_now_after_the_poisoner``) and runs only those two, in that
order, single-process -- same reasoning as lode-x10m's original repro: xdist's default
``--dist load`` does not guarantee two tests in the same file land on the same worker, let alone
adjacently, so only a nested, ordered, single-process run pins the "poisoner immediately before
victim" scenario at all.

NON-VACUITY -- MEASURED, both arms, for the CURRENT (lode-up8x) synthetic repro:

    fixture disabled (env var set)   -> 1 passed, 1 failed
    fixture enabled  (normal)        -> 2 passed

The one failure with the fixture disabled is the victim's own assertion that ``jobs.now()`` reads
close to the wall clock -- it instead reads the poisoner's far-future sentinel, because nothing
reset ``jobs._now_epoch`` between the two tests. ``test_poisoner_no_longer_flips_its_victims``
below runs both arms itself and asserts they disagree, so this is a live, executable pin rather
than a recorded one-off measurement -- it re-verifies on every run, not just at review time.

That disagreement is asserted SPECIFICALLY, not as "the disabled arm exited non-zero somehow": the
disabled arm must exit 1 (pytest's "tests failed") *and* its output must carry
``_VICTIM_POISON_SURVIVED_MARKER``, the victim's own assertion text. A bare ``returncode != 0``
would also be satisfied by a subprocess that never started, an import or collection error, a
renamed node id (exit 4), or a timeout kill -- each of which would leave this arm passing while
measuring nothing, which is the very defect (a vacuous pin) that this file exists to not have. The
enabled arm's ``returncode == 0`` independently catches any breakage symmetric across both arms.

OLD REPRO, FOR THE RECORD (lode-x10m / lode-e8lo, retired by lode-up8x rather than kept alongside
the new one -- chaining onto ``test_cli.py``'s and ``test_worker.py``'s tests is now vacuous, so a
second repro next to the synthetic one would buy nothing):

    pre-lode-e8lo  test_cli.py, fixture body removed -> 2 failed, 1 passed
    post-lode-e8lo test_cli.py, fixture body removed -> 3 passed
    post-lode-e8lo test_cli.py, fixture in place      -> 3 passed

i.e. it now passes whether or not ``_reset_jobs_clock_anchor`` does anything.

Deliberately NOT asserted: that ``jobs._now_epoch`` equals its sentinel at test-body entry, checked
in a single standalone test relying on natural suite ordering. In a fresh process the anchor
already starts at that sentinel, so the assertion would pass with or without the fixture unless a
poisoner happened to run first in the same worker -- i.e. it would be order-dependent, which is the
exact property this ticket exists to remove from the suite. The two-test, nested-subprocess form
above gets the same coverage without that dependency, by controlling the order itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import _DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR

from lode import jobs

#: Anchored on ``__file__``, never ``Path.cwd()`` -- same convention as
#: ``tests/test_conftest_color_scrub.py`` and ``tests/conftest.py``'s own ``_CHECKOUT_ROOT``. A
#: cwd-relative node id makes the nested run exit 4 ("file or directory not found") for a plain
#: ``pytest`` invoked from a subdirectory, and that reads as "the fixture is broken" -- a false
#: red of exactly the kind lode-x10m exists to eliminate.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Far enough in the future that no plausible wall-clock drift during a test run could explain a
#: victim reading it -- distinguishing "the poison survived" from "the machine is slow".
_POISON_SENTINEL = datetime(2999, 1, 1, tzinfo=UTC)

#: How close to the real wall clock a NOT-poisoned reading must land. Generous on purpose -- this
#: guards against the far-future sentinel above, not against ordinary test-run latency.
_SANE_DRIFT_TOLERANCE = timedelta(minutes=5)

#: A substring of the victim's own assertion message, SHARED with it rather than hand-copied, so a
#: later reword of that message cannot silently decouple the two (nor produce a confusing false red
#: here). Why the exit code alone is not enough to trust the fixture-disabled arm: see the module
#: docstring's NON-VACUITY section.
_VICTIM_POISON_SURVIVED_MARKER = "jobs._now_epoch was not reset before this test ran"

#: lode-up8x's synthetic repro, in this order: the poisoner first, then its one victim. Order IS
#: the whole point -- see the module docstring's THE SYNTHETIC POISONER section.
_SYNTHETIC_REPRO = (
    "tests/test_conftest_jobs_clock_anchor.py::"
    "test_synthetic_poisoner_leaves_now_epoch_in_the_far_future",
    "tests/test_conftest_jobs_clock_anchor.py::"
    "test_synthetic_victim_reads_a_sane_now_after_the_poisoner",
)

#: Wall-clock cap on ONE nested arm. Measured unloaded in this worktree: ~2.1s per arm end to end
#: (pytest's own reported time is ~1.0s; the rest is interpreter start, conftest import and plugin
#: registration, which ``subprocess.run`` pays and pytest does not count), so ~4.3s for both arms.
#: Raised far above that, and above ``pyproject.toml``'s global ``timeout = 120``, because it
#: competes with 7 xdist siblings; at the default cap it would itself become the load-sensitive
#: false red this ticket is about. Kept under the outer ``@pytest.mark.timeout`` so a wedged
#: subprocess surfaces as a legible ``TimeoutExpired`` from here rather than as an opaque outer
#: kill -- note that budget covers BOTH arms (2 x 150 = 300 < 360), so the outer mark must stay
#: above twice this value, not once.
_NESTED_TIMEOUT_S = 150


def _run_synthetic_repro(*, fixture_disabled: bool) -> subprocess.CompletedProcess[str]:
    """Run ``_SYNTHETIC_REPRO`` in a nested subprocess, with the anchor-reset fixture on or off."""
    env = dict(os.environ)
    if fixture_disabled:
        env[_DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR] = "1"
    else:
        env.pop(_DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR, None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            # Keep the nested run off .pytest_cache state shared with the outer run, and
            # single-process so the two node ids stay in the order given.
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            # pytest-randomly is not currently installed, and pytest accepts ``-p no:`` for an
            # absent plugin. Named anyway because this test's subject IS an ordering effect:
            # were that plugin ever added, it would shuffle these two node ids and make this
            # test intermittent -- reintroducing, in the pin itself, the flake it guards against.
            "-p",
            "no:randomly",
            "-q",
            *_SYNTHETIC_REPRO,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_NESTED_TIMEOUT_S,
        check=False,  # a red nested run is this test's own assertion, not an exception
        env=env,
    )


@pytest.mark.timeout(360)
def test_poisoner_no_longer_flips_its_victims() -> None:
    """The anchor-reset fixture is load-bearing: the synthetic repro fails without it, passes with it.

    Runs ``_SYNTHETIC_REPRO`` twice -- fixture disabled, then enabled -- and asserts the outcomes
    disagree. Sabotage discriminates: see the module docstring's NON-VACUITY section for the
    measured (1 passed, 1 failed) vs (2 passed) split this is pinning.
    """
    disabled = _run_synthetic_repro(fixture_disabled=True)
    assert disabled.returncode == 1, (
        "expected the synthetic poisoner/victim repro to FAIL with "
        "tests/conftest.py's _reset_jobs_clock_anchor fixture disabled -- got exit "
        f"{disabled.returncode} instead. Exit 0 means the repro no longer exercises the fixture at "
        "all; any other exit (2 interrupted, 3 internal error, 4 usage/bad node id, 5 nothing "
        "collected) means the nested run broke before it could measure anything, which would make "
        f"this arm vacuous rather than red. stdout:\n{disabled.stdout}\nstderr:\n{disabled.stderr}"
    )
    assert _VICTIM_POISON_SURVIVED_MARKER in disabled.stdout, (
        "the fixture-disabled repro exited 1, but not from the victim's own clock-anchor "
        f"assertion -- {_VICTIM_POISON_SURVIVED_MARKER!r} is absent from its output, so some other "
        "test failure is standing in for the measurement and this arm proves nothing. "
        f"stdout:\n{disabled.stdout}\nstderr:\n{disabled.stderr}"
    )

    enabled = _run_synthetic_repro(fixture_disabled=False)
    assert enabled.returncode == 0, (
        "jobs._now_epoch is no longer isolated: running the synthetic poisoner ahead of its "
        "victim flipped it even with tests/conftest.py's _reset_jobs_clock_anchor fixture "
        f"enabled. stdout:\n{enabled.stdout}\nstderr:\n{enabled.stderr}"
    )


def test_synthetic_poisoner_leaves_now_epoch_in_the_far_future() -> None:
    """Poison ``jobs._now_epoch`` directly, standing in for lode-x10m's real (now-fixed) poisoner.

    Only exercised for real via ``_SYNTHETIC_REPRO``'s nested subprocess, where it is forced to
    run immediately before its victim below (see the module docstring's THE SYNTHETIC POISONER
    section for why a monkeypatched ``time.monotonic()`` isn't used here instead). It also runs
    harmlessly as an ordinary member of the outer suite -- whatever runs after it in the same
    worker still gets a freshly reset anchor from the always-on autouse fixture, same as any other
    test; nothing about this test depends on running adjacent to its victim outside the nested run.

    No monkeypatch involved on purpose: the real bug's signature is that nothing ever reverted
    ``_now_epoch`` once poisoned (``monkeypatch`` only reverts what it itself patched), so a raw
    module-attribute write reproduces exactly what survives a real poisoner, without also
    reproducing the part lode-e8lo already fixed.
    """
    jobs._now_epoch = _POISON_SENTINEL


def test_synthetic_victim_reads_a_sane_now_after_the_poisoner() -> None:
    """Assert ``jobs.now()`` reads close to the wall clock, not the previous test's poison.

    Only meaningful when run immediately after
    ``test_synthetic_poisoner_leaves_now_epoch_in_the_far_future`` -- see ``_SYNTHETIC_REPRO``.
    Passing here means something reset ``jobs._now_epoch`` between the two tests; in the nested
    repro that something is ``tests/conftest.py``'s ``_reset_jobs_clock_anchor`` fixture (or, with
    it disabled, nothing -- and this fails).
    """
    reading = jobs.now()
    drift = abs(reading - datetime.now(UTC))
    assert drift < _SANE_DRIFT_TOLERANCE, (
        f"jobs.now() returned {reading.isoformat()}, {drift} away from the wall clock -- "
        f"{_VICTIM_POISON_SURVIVED_MARKER}, i.e. the previous test's poison survived. "
        "Check tests/conftest.py's _reset_jobs_clock_anchor fixture."
    )


def test_the_anchor_reset_fixture_is_armed_for_every_test(
    request: pytest.FixtureRequest,
) -> None:
    """The reset must reach every test by being autouse, not by being requested, AND be armed.

    This is what makes the guarantee independent of ``--dist`` mode and worker count: the reset
    runs per TEST, so no scheduling decision can place a victim where it does not reach. A
    narrower fixture that tests had to opt into would protect only the three victims lode-x10m
    happened to find.

    "Armed" is two conditions, not one, since lode-up8x gave the fixture an env-var escape hatch:
    it must be attached to this test, AND that hatch must not be set. Being attached is worthless
    if the fixture returns early -- an ambient
    ``_DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR`` exported into a shell, a CI job, or a
    ``.env`` would disable the protection for EVERY test in the run while
    ``request.fixturenames`` still lists it, i.e. exactly the silent re-opening of lode-x10m that
    the hatch's defensive naming only makes unlikely rather than impossible. Asserting it here
    costs nothing and converts that silence into an immediate red.

    Safe to assert unconditionally: ``_SYNTHETIC_REPRO`` names only the two synthetic tests, so
    the nested run that legitimately sets the hatch never collects this test.
    """
    assert "_reset_jobs_clock_anchor" in request.fixturenames, (
        "the autouse jobs-clock anchor reset is not active for this test -- it was renamed, "
        "deleted, or lost autouse=True in tests/conftest.py (lode-x10m)"
    )
    # Mirrors the fixture's own truthiness check exactly (``os.environ.get``, not ``in
    # os.environ``): an empty-string value does NOT disable it, so it must not fail here either.
    assert not os.environ.get(_DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR), (
        f"{_DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR} is set in this test run's environment, so "
        "tests/conftest.py's _reset_jobs_clock_anchor fixture is returning early and resetting "
        "nothing -- the whole suite is running unprotected against a poisoned jobs._now_epoch "
        "(lode-x10m). That variable is for tests/test_conftest_jobs_clock_anchor.py's own nested "
        "subprocess ONLY (lode-up8x); unset it."
    )
