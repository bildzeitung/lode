"""Nox sessions for lode's dev loop.

Two entry points run by default, both required before any merge (CLAUDE.md):

    nox -t fix      ruff format + ruff check --fix   (the pre-merge fixer)
    nox -s tests    pytest                            (the test gate)

Plus one opt-in, credential-gated session that is **not** in the default set:

    nox -s eval     lode eval                         (the golden-set eval, CI-only)

``eval`` runs the eval harness end to end with the real local embedder and a
real Anthropic client (``lode eval``), so it needs ``ANTHROPIC_API_KEY`` and the
network — it ``skip``s itself when the key is absent. It is deliberately kept out
of ``nox.options.sessions`` so a bare ``nox`` run and ``nox -s tests`` stay
offline and keyless (the determinism/network split is settled in
``docs/decisions.md``, the eval-harness entry).

Sessions run inside the already-built project venv (``./venv`` from
``scripts/python-init.sh``) rather than nox-managed isolated venvs: the
runtime stack is heavy (lancedb, fastembed, textual, ...) and is installed
once via ``-e .[dev]``, so re-provisioning it per session would be slow with
no benefit. Activate the venv first, then run nox.
"""

import os

import nox

nox.options.default_venv_backend = "none"

# A bare ``nox`` runs only the offline, keyless gates; ``eval`` (network + an API
# key) stays explicit, never a default.
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
def eval(session: nox.Session) -> None:
    """Run the golden-set eval (``lode eval``) — CI-only, needs Anthropic creds.

    Skipped when ``ANTHROPIC_API_KEY`` is absent: the Q&A leg hits Claude, so this
    is the credentialed CI-style check, never part of the offline test gate.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        session.skip("ANTHROPIC_API_KEY not set — eval needs Anthropic credentials")
    session.run("lode", "eval")
