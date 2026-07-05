"""Nox sessions for lode's dev loop.

Two entry points run by default, both required before any merge (CLAUDE.md):

    nox -t fix      ruff format + ruff check --fix   (the pre-merge fixer)
    nox -s tests    pytest                            (the test gate)

Plus one opt-in, credential-gated session that is **not** in the default set:

    nox -s eval     pytest tests/test_eval_live.py    (the golden-set eval, CI-only)

``eval`` runs the live integration test (``tests/test_eval_live.py``) end to
end with the real local embedder and a real Anthropic client, so it needs
``ANTHROPIC_API_KEY`` and the network — it ``skip``s itself when the key is
absent. It is deliberately kept out of ``nox.options.sessions`` so a bare
``nox`` run and ``nox -s tests`` stay offline and keyless (the
determinism/network split is settled in ``docs/decisions.md``, the eval-harness
entry).

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
    """Run the test suite (pytest)."""
    session.run("pytest")


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
    real Anthropic client.  Skipped when ``ANTHROPIC_API_KEY`` is absent: the
    Q&A leg hits Claude, so this is the credentialed CI-style check, never part
    of the offline test gate (see ``docs/decisions.md``, Shape A, lode-5y8.5).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        session.skip("ANTHROPIC_API_KEY not set — eval needs Anthropic credentials")
    session.run("pytest", "tests/test_eval_live.py", "-v")
