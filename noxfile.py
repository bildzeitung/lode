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
"""

import os
import tempfile
import zipfile
from pathlib import Path

import nox

nox.options.default_venv_backend = "none"

# A bare ``nox`` runs only the offline, keyless gates; ``eval`` (network + an API
# key) and ``build`` (packaging, not a code gate) stay explicit, never a default.
nox.options.sessions = ["fix", "tests"]


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
    """
    session.run("pytest")


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
    """
    session.run("pytest", "-m", "not slow")


@nox.session
def build(session: nox.Session) -> None:
    """Build a wheel + sdist and assert the shipped package-data is present.

    ``python -m build`` (lode-8vq) is the canonical packaging front-end, but a
    build succeeding doesn't prove package-data made it in — that's exactly
    the lode-1i8.4 footgun (the TUI's ``.tcss`` almost shipped without it).
    Build into a scratch dir, then inspect the wheel's file list directly
    rather than just trusting a clean exit.
    """
    with tempfile.TemporaryDirectory() as outdir:
        session.run("python", "-m", "build", "--outdir", outdir)
        wheels = list(Path(outdir).glob("*.whl"))
        sdists = list(Path(outdir).glob("*.tar.gz"))
        if not wheels or not sdists:
            session.error("python -m build did not produce both a wheel and an sdist")
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
