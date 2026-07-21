"""Nox sessions for lode's dev loop.

Two entry points run by default, both required before any merge (CLAUDE.md):

    nox -t fix      ruff format + ruff check --fix   (the pre-merge fixer)
    nox -s tests    pytest                            (the test gate — the FULL suite,
                                                         every test, no marker filter;
                                                         this is what /land re-gates with)

Plus two opt-in sessions that are **not** in the default set:

    nox -s unit     pytest -m "not slow"              (fast inner loop, lode-pql)
    nox -s eval     pytest tests/test_eval_live.py    (the golden-set eval, CI-only)

**Fast vs. full split (lode-pql).** ``pytest --durations`` profiling found a small
set of tests dominate wall-clock: end-to-end CLI flows and skeleton-gate tests that
invoke ``lode ask``/``lode retrieve`` without mocking the reranker pay a real
``FastEmbedCrossEncoder`` model-load cost (several seconds each), plus the live eval
integration test below (~300s, when credentialed). Those are tagged
``@pytest.mark.slow`` (registered in ``pyproject.toml``) — see the tests themselves
(``tests/test_skeleton_gate.py``, ``tests/test_cli.py``, ``tests/test_eval_live.py``)
for exactly which and why. ``nox -s unit`` filters them out for a fast code-time
inner loop; ``nox -s tests`` applies **no** marker filter and always runs the FULL
suite — that stays the merge/landing gate (CLAUDE.md, `/land`'s re-gate) so no test
is ever skipped before trunk. See ``docs/onboarding.md`` for the full picture.

``eval`` runs the live integration test (``tests/test_eval_live.py``) end to
end with the real local embedder and a real Anthropic client, so it needs
``ANTHROPIC_API_KEY`` and the network — it ``skip``s itself when the key is
absent. It is deliberately kept out of ``nox.options.sessions`` so a bare
``nox`` run and ``nox -s tests`` stay offline and keyless (the
determinism/network split is settled in ``docs/decisions.md``, the eval-harness
entry).

**Opt-in via env var, not credential-gating alone (lode-b4w.7).** The live
eval test self-skips unless ``LODE_RUN_LIVE_EVAL=1`` is set; this ``eval``
session is the only place that sets it. Credential presence used to be the
*only* gate (``pytest.skip`` when ``ANTHROPIC_API_KEY`` was absent) — that
meant an ambient key in the shell (the normal case in agent environments)
made ``nox -s tests`` silently run the live, ~273s, API-billed pass, since
that session applies no marker filter and was never supposed to include it.
The env var makes the offline gate's exclusion independent of whatever
credentials happen to be sitting in the environment; the credential check
still runs as a second layer once opted in, so ``nox -s eval`` keeps
skipping cleanly without a key.

Sessions run inside the already-built project venv (``./venv`` from
``scripts/python-init.sh``) rather than nox-managed isolated venvs: the
runtime stack is heavy (lancedb, fastembed, textual, ...) and is installed
once via ``-e .[dev]``, so re-provisioning it per session would be slow with
no benefit. Activate the venv first, then run nox.

That reuse has one footgun, and it is guarded rather than designed away
(lode-jh80): because the install is editable, an active venv's ``lode``
resolves to the ``src`` of whichever checkout it was *built* in, so activating
the main checkout's venv while sitting in a worktree silently exercises the
wrong source tree. ``tests/conftest.py``'s ``pytest_configure`` guard 0 fails
the run loudly in that case. It lives there, not here, so it covers a bare
``pytest`` too -- every invocation, not just the sessions below.

**Parallelism (lode-b4w.6).** Both ``tests`` and ``unit`` run under
``pytest-xdist``. Measured on an 8-core dev machine, offline
(``ANTHROPIC_API_KEY`` unset — see the ambient-key determinism note below and
lode-7mq for a related, separately-tracked leak): ``unit`` 151.8s serial ->
33-41s parallel over repeated runs; the full ``tests`` suite 126.7-133.9s
serial -> 39-60s parallel over repeated runs, all green. This suite has no
shared on-disk state to race on — every test gets its own ``$LODE_HOME`` via
the autouse ``_isolate_lode_home`` fixture in ``tests/conftest.py`` (a fresh
``tmp_path_factory`` directory per test), so distributing tests across worker
processes is safe; repeated runs stayed green with no ordering-sensitive
failures. If a future test introduces shared on-disk or global state, xdist
would surface that as a flake — investigate the test's isolation before
assuming xdist itself is at fault.

**Worker count defaults to 8, not "one per core" (lode-bv6y, superseding
lode-b4w.6's original ``-n auto``).** SPIKE lode-mtuy measured, on an idle
24-core/31GiB box, that ``-n auto`` (24 workers there) is the **slowest**
width for this suite — median full-suite wall-clock 25s at auto vs 23s at
``-n 8``, and every 24-worker run was at or above the worst 8-worker run. The
curve knees at 8, stays flat through 16, and rises past ~12: each additional
worker beyond the knee pays more in process-startup + model-load cost than it
recovers in parallelism. Combined with lode-lwx6's measured memory curve
(11.4 GiB peak PSS at 24 workers vs 6.5 GiB at 8 — see
``docs/agents-workflow.md#concurrency-cap-lode-2cf``), auto is worse on
**both** axes, so 8 is the default width here, not a tuned-down compromise.
Override with the ``LODE_TEST_WORKERS`` env var — a positive integer passed
straight through to ``pytest -n``, or the literal ``auto`` to opt back into
one worker per CPU core (the spike's specific knee was measured on one
machine; the finding that auto isn't free should generalize, but the number
8 may not — e.g. a machine with very different core/memory characteristics).
This same effective worker count feeds ``/code``'s concurrency-cap formula
(``docs/agents-workflow.md#concurrency-cap-lode-2cf``) — the two knobs must
stay consistent, so change ``LODE_TEST_WORKERS`` rather than assuming the cap
still means what it used to.

**This knob is for throughput, and for nothing else. Narrowing ``-n`` will
make the known flaky tests fail less often — that is a MUZZLE, NOT A FIX.**
Those flakes (lode-t1y, lode-9vns, lode-64jn) are CPU-starvation races, and a
race that stops losing because you gave it a less contended machine is still a
race: lode-t1y's suspected ``worker._claim_one`` predicate bug can silently
skip a job **in production**, where no ``-n`` setting protects anyone. So: do
not reach for this knob to quiet a flake, do not narrow it further on the
strength of a greener suite, and do not cite a reduced flake rate as evidence
the default is right — the justification above is wall-clock and memory,
measured, and that is the only ground this default stands on. A flake that
went quiet here is a defect that went *unobserved*, not one that went away.
"""

import contextlib
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

import nox

nox.options.default_venv_backend = "none"

# A bare ``nox`` runs only the offline, keyless gates; ``eval`` (network + an API
# key) and ``build`` (packaging, not a code gate) stay explicit, never a default.
nox.options.sessions = ["fix", "tests", "shellcheck", "linkcheck"]


def _xdist_workers() -> str:
    """Effective pytest-xdist worker count for ``-n`` (``LODE_TEST_WORKERS``, lode-bv6y).

    Defaults to ``"8"`` — see the module docstring's "Worker count defaults to
    8" note for why this replaced ``-n auto``. The value is handed straight to
    ``pytest -n``, so anything xdist accepts works: a positive integer, or the
    literal ``"auto"`` / ``"logical"`` to opt back into one worker per (logical)
    CPU core. An unset *or empty* var means the default — an exported-but-empty
    ``LODE_TEST_WORKERS`` is a "not set" in every shell idiom that produces it,
    and would otherwise reach pytest as ``-n ''`` and fail the gate on a usage
    error. Garbage still fails loudly at pytest, by design: this is a developer
    knob, not user input, and a silent fallback would hide the typo.

    Keep this in step with ``/code``'s concurrency cap, which derives its
    per-agent memory budget from this same width
    (``docs/agents-workflow.md#concurrency-cap-lode-2cf``); it treats any
    non-integer width as one-worker-per-core so that it errs tight.
    """
    return os.environ.get("LODE_TEST_WORKERS") or "8"


@nox.session(tags=["fix"])
def fix(session: nox.Session) -> None:
    """Format and lint-fix the tree in place (ruff)."""
    session.run("ruff", "format", ".")
    session.run("ruff", "check", "--fix", ".")


@nox.session
def tests(session: nox.Session) -> None:
    """Run the FULL test suite (pytest, no marker filter) — the merge/landing gate.

    Every test runs here, including ones tagged ``@pytest.mark.slow`` — this is
    the suite ``/land`` re-gates with, so nothing slow is ever skipped before
    trunk (lode-pql). For a fast code-time inner loop, see ``nox -s unit``.

    Runs under ``pytest-xdist`` (``-n`` from ``LODE_TEST_WORKERS``, default
    ``8``, lode-bv6y — see the module docstring) — no marker filter changes,
    no test skipped, just distributed across workers.
    """
    session.run("pytest", "-n", _xdist_workers())


@nox.session
def shellcheck(session: nox.Session) -> None:
    """Lint every tracked shell script (shellcheck, --severity=warning).

    A default gate for the repo's shell — ``.claude/statusline.sh`` and
    ``scripts/*.sh``. The binary ships bundled via the ``shellcheck-py`` dev
    dep (see ``pyproject.toml``), so this needs no system package. Gated at
    ``warning``: errors and warnings (real bugs — unquoted word-splitting, bad
    test operators) fail the gate; ``info``/``style`` notes do not, keeping the
    signal level in line with what ruff enforces for Python. Scoped to tracked
    files via ``git ls-files`` so scratch/vendored scripts never enter the gate.
    """
    files = subprocess.run(
        ["git", "ls-files", "*.sh"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not files:
        session.skip("no tracked shell scripts to check")
    session.run("shellcheck", "--severity=warning", *files)


@nox.session
def linkcheck(session: nox.Session) -> None:
    """Verify every relative markdown link in docs/ and .claude/ resolves (lode-dkdg).

    Complements ``scripts/validate-mermaid.sh`` (diagram *syntax*) with the other
    half of doc rot: cross-document links and ``#anchor`` fragments. GitHub
    derives an anchor slug from a heading's TEXT, so rewording a heading
    silently breaks every inbound link with nothing failing to report it --
    see ``scripts/check_links.py``'s module docstring for a concrete dead
    anchor this gate found already sitting in trunk. Pure Python, no Docker
    and no network, so -- unlike ``validate-mermaid.sh`` -- it belongs in the
    default offline gate set alongside ``fix``/``tests``/``shellcheck``.
    """
    session.run("python", "scripts/check_links.py")


@nox.session
def unit(session: nox.Session) -> None:
    """Run the FAST inner-loop subset — pytest with slow tests excluded (lode-pql).

    Excludes everything tagged ``@pytest.mark.slow`` (real model-load cost:
    the un-mocked ``FastEmbedCrossEncoder`` reranker, or the live eval Q&A
    leg) — see ``tests/test_skeleton_gate.py``, ``tests/test_cli.py``, and
    ``tests/test_eval_live.py`` for exactly which tests and why.  This is a
    code-time convenience only, never a merge gate: it drops no coverage
    permanently, it just defers the slow tier to ``nox -s tests``, which
    every merge (and `/land`'s re-gate) still runs in full.

    Runs under ``pytest-xdist`` (``-n`` from ``LODE_TEST_WORKERS``, default
    ``8``, lode-bv6y — see the module docstring).
    """
    session.run("pytest", "-m", "not slow", "-n", _xdist_workers())


@nox.session
def build(session: nox.Session) -> None:
    """Build a wheel + sdist and assert the shipped package-data is present.

    ``python -m build`` (lode-8vq) is the canonical packaging front-end, but a
    build succeeding doesn't prove package-data made it in — that's exactly
    the lode-1i8.4 footgun (the TUI's ``.tcss`` almost shipped without it).
    Build into a scratch dir, then inspect the wheel's file list directly
    rather than just trusting a clean exit.

    This is the SINGLE implementation of that assertion — both build.yml
    (push/PR) and release.yml (the ``vX.Y.Z`` tag push that ships the
    published wheel, lode-zuqp) call this session rather than each keeping
    their own copy. Pass an output directory as a posarg
    (``nox -s build -- dist``) to keep the built artifacts on disk — release.yml
    needs them to survive for its ``gh release create ... dist/*`` upload. With
    no posarg, builds into a throwaway ``TemporaryDirectory`` and discards the
    artifacts (build.yml's push/PR check only cares about the assertion).
    """
    with contextlib.ExitStack() as stack:
        outdir = (
            session.posargs[0]
            if session.posargs
            else stack.enter_context(tempfile.TemporaryDirectory())
        )
        session.run("python", "-m", "build", "--outdir", outdir)
        wheels = list(Path(outdir).glob("*.whl"))
        sdists = list(Path(outdir).glob("*.tar.gz"))
        # Exactly one of each, not merely "at least one": with a caller-supplied
        # outdir the dir is no longer guaranteed fresh, and a leftover wheel from
        # an earlier run would make the check below inspect an arbitrary
        # glob-order wheel while release.yml's `gh release create ... dist/*`
        # uploads every file present. Asserted artifact == published artifact is
        # the whole point of routing release.yml through this session (lode-zuqp).
        if len(wheels) != 1 or len(sdists) != 1:
            session.error(
                f"expected exactly one wheel and one sdist in {outdir}, found "
                f"{len(wheels)} wheel(s) and {len(sdists)} sdist(s) — a build "
                "failure, or stale artifacts left in a reused output directory"
            )
        with zipfile.ZipFile(wheels[0]) as zf:
            names = zf.namelist()
        for expected in ("lode/schema.sql", "lode/tui/lode.tcss"):
            if expected not in names:
                session.error(f"wheel is missing expected package-data: {expected}")


@nox.session
def eval(session: nox.Session) -> None:
    """Run the live eval integration test — CI-only, needs Anthropic creds.

    Runs ``tests/test_eval_live.py`` with the real local ONNX embedder and a
    real Anthropic client. Sets ``LODE_RUN_LIVE_EVAL=1`` so the test's opt-in
    skip clears (lode-b4w.7) — this is the only session that sets it, which is
    what keeps the live pass out of ``nox -s tests``/``nox -s unit``
    regardless of ambient credentials. Also skips when ``ANTHROPIC_API_KEY``
    is absent: the Q&A leg hits Claude, so this is the credentialed CI-style
    check, never part of the offline test gate (see ``docs/decisions.md``,
    Shape A, lode-5y8.5).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        session.skip("ANTHROPIC_API_KEY not set — eval needs Anthropic credentials")
    session.run(
        "pytest",
        "tests/test_eval_live.py",
        "-v",
        env={"LODE_RUN_LIVE_EVAL": "1"},
    )
