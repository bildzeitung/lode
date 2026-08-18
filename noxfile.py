"""Nox sessions for lode's dev loop.

Two entry points are REQUIRED before any merge (CLAUDE.md) -- a narrower claim
than "runs by default": a bare ``nox`` invocation actually runs all SIX
sessions in ``nox.options.sessions`` below (``fix``, ``tests``, ``shellcheck``,
``linkcheck``, ``docstringcheck``, ``docs``), but CLAUDE.md's merge gate only names these two:

    nox -t fix      ruff format + ruff check --fix   (the pre-merge fixer)
    nox -s tests    pytest                           (the test gate — the FULL suite,
                                                        every test, no marker filter;
                                                        this is what /land re-gates with)

The other four sessions in the default set, not required-before-merge by name
but still part of a bare ``nox`` run:

    nox -s shellcheck      lint every tracked shell script (--severity=warning)
    nox -s linkcheck       verify every relative markdown link in docs/ and .claude/ resolves (lode-dkdg)
    nox -s docstringcheck  verify every symbol-naming Sphinx role naming a lode.* symbol
                             in src/ and tests/ resolves to a real symbol (lode-8oeu)
    nox -s docs            build the mkdocs site and fail on a broken intra-doc anchor (lode-fhql.20)

Plus FIVE opt-in sessions that are **not** in the default set:

    nox -s unit            pytest -m "not slow"                     (fast inner loop, lode-pql)
    nox -s eval            pytest tests/test_eval_live.py           (the golden-set eval, CI-only)
    nox -s coverage        pytest --cov=lode --cov-report=xml ...   (coverage measurement, CI-only, lode-qxdn.3)
    nox -s lock_currency   verify requirements.lock is current      (local mirror of CI's lock-currency
                                                                       job, lode-sys4 -- run by /land's
                                                                       re-gate so a stale lock never lands)
    nox -s build           build a wheel + sdist, assert package-data ships (packaging, not a code gate)

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
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import NoReturn

import nox
import nox.command

nox.options.default_venv_backend = "none"

# Exit status meaning "this gate could not RUN" -- a machine fault, not a content
# failure. `/land` must never isolate or bounce a branch on it (lode-9i2p; the
# original carrier of this contract is ``scripts/validate-mermaid.sh``). nox
# collapses every ordinary session failure to exit 1, so signalling anything
# else means leaving the process directly -- see ``_machine_fault`` below.
GATE_MACHINE_FAULT = 2

# A bare ``nox`` runs only the offline, keyless gates; ``eval`` (network + an API
# key) and ``build`` (packaging, not a code gate) stay explicit, never a default.
nox.options.sessions = [
    "fix",
    "tests",
    "shellcheck",
    "linkcheck",
    "docstringcheck",
    "docs",
]

# The project's own venv, always at this fixed location relative to this file
# (CLAUDE.md: "The venv lives at ./venv (repo root)").
_VENV_BIN = Path(__file__).parent.resolve() / "venv" / "bin"


def _venv_tool(session: nox.Session, name: str) -> str:
    """Resolve ``name`` to its path inside the project's own ``./venv/bin`` (lode-0yfn).

    ``venv_backend = "none"`` (above) means every session inherits whatever
    PATH the invoking shell happens to have, so a bare ``"ruff"`` resolves
    whichever copy is *earliest* on it -- which, on a box with a stale
    system-wide install (pip ``--user``, pipx, ...) ahead of the venv, is
    silently NOT the pinned one, and the gate reports success anyway. Full
    narrative, including the reproduction and why the ``dev`` extra pins ruff
    at all: ``docs/stack.md``'s dependency-locking section.

    Deriving the path from this file's own location rather than searching
    PATH makes that ambient ordering irrelevant. Fails the session loudly if
    the tool isn't there: these tools only ever come from the project venv, so
    a missing venv means the session cannot do its job regardless -- surfacing
    that beats silently falling through to whatever ambient copy exists.
    **A consequence worth knowing before you edit CI: any workflow running one
    of these sessions must build ``./venv`` first** (``scripts/python-init.sh``);
    installing the dev extra into the runner's ambient interpreter is not
    enough. ``tests.yml`` and ``coverage.yml`` both do.

    Deliberately NOT applied to two sessions, both of which mean to reach a
    tool outside ``./venv``: ``build`` (ambient ``python -m build``, which
    ``build.yml``/``release.yml`` run with no ``./venv`` at all, and which
    resolves its own isolated PEP 517 env) and ``lock_currency`` (``uv``, a
    system-wide tool never installed into ``./venv``, already checked via
    ``shutil.which`` and failed closed if absent). That exemption is enforced,
    not just documented -- ``tests/test_noxfile_venv_tool.py`` fails if any
    *other* session shells out to a dev-extra tool by bare name.
    """
    tool = _VENV_BIN / name
    if not tool.is_file():
        session.error(
            f"expected the project venv's {name!r} at {tool}, not found -- "
            "run ./scripts/python-init.sh first (see CLAUDE.md) before "
            "running this session"
        )
    return str(tool)


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
    """Format and lint-fix the tree in place (ruff).

    Resolves ``ruff`` through ``_venv_tool`` (lode-0yfn) — see its docstring.
    This is the session that bug was found in: a stale system-wide ``ruff``
    earlier on PATH silently shadowed the pinned copy and this gate still
    went green.
    """
    ruff = _venv_tool(session, "ruff")
    session.run(ruff, "format", ".")
    session.run(ruff, "check", "--fix", ".")


@nox.session
def tests(session: nox.Session) -> None:
    """Run the FULL test suite (pytest) — the merge/landing gate.

    Every test runs here, including ones tagged ``@pytest.mark.slow`` — this is
    the suite ``/land`` re-gates with, so nothing slow is ever skipped before
    trunk (lode-pql). For a fast code-time inner loop, see ``nox -s unit``.

    Runs under ``pytest-xdist`` (``-n`` from ``LODE_TEST_WORKERS``, default
    ``8``, lode-bv6y — see the module docstring), split into two invocations
    that exhaustively partition the suite on ``@pytest.mark.serial`` (lode-887o,
    registered in ``pyproject.toml``): everything else in the parallel pool,
    then the serial tests with no xdist workers at all. A ``serial`` test
    asserts a wall-clock budget that sibling workers' scheduler noise would
    make flaky. No test is skipped and none runs twice — the partition is the
    only thing the marker changes.

    Resolves ``pytest`` through ``_venv_tool`` (lode-0yfn) — see its docstring.
    """
    pytest = _venv_tool(session, "pytest")
    session.run(pytest, "-m", "not serial", "-n", _xdist_workers())
    session.run(pytest, "-m", "serial", "-n", "0")


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

    Resolves ``shellcheck`` (the ``shellcheck-py`` dev dep) through
    ``_venv_tool`` (lode-0yfn) — see its docstring.
    """
    files = subprocess.run(
        ["git", "ls-files", "*.sh"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not files:
        session.skip("no tracked shell scripts to check")
    session.run(_venv_tool(session, "shellcheck"), "--severity=warning", *files)


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

    Resolves ``python`` through ``_venv_tool`` (lode-0yfn) — see its docstring;
    an ambient interpreter would not have the project's deps (e.g. ``typer``).
    """
    session.run(_venv_tool(session, "python"), "scripts/check_links.py")


@nox.session
def docstringcheck(session: nox.Session) -> None:
    """Verify every symbol-naming Sphinx role (``:func:``, ``:class:``,
    ``:data:``, ``:meth:``, ``:attr:``, ``:mod:``, ``:exc:``, ``:obj:``) naming
    a ``lode.*`` symbol in a docstring/comment under ``src/`` or ``tests/``
    resolves to a real symbol (lode-8oeu).

    A single rename (``lode-ekqh``) left four dangling refs across two
    branches that merged in the same ``/land`` pass, caught only by a hand
    sweep (``lode-2hfd``) -- one of the four had never named a real symbol
    at all. ``scripts/check_links.py``/``linkcheck`` is markdown-only and
    does not reach these. Hard gate, in the default offline set alongside
    ``linkcheck`` -- pure Python, no Docker, no network; imports the real
    ``lode`` package to resolve refs, which the shared ``./venv`` already
    has installed editable (same as ``tests``).

    Resolves ``python`` through ``_venv_tool`` (lode-0yfn) — an ambient
    interpreter would not have ``lode`` (or ``typer``) installed.
    """
    session.run(_venv_tool(session, "python"), "scripts/check_docstring_refs.py")


@nox.session
def docs(session: nox.Session) -> None:
    """Build the mkdocs site and fail on a broken intra-doc anchor (lode-fhql.20).

    ``scripts/check_links.py``/``linkcheck`` resolves ``#anchor`` fragments with the same
    slug algorithm GitHub uses -- but mkdocs-material's own renderer slugs heading text
    differently (punctuation in particular), so a link that resolves cleanly on GitHub can
    still 404 on the built site. A local ``mkdocs serve`` surfaced exactly this (two
    anchors, filed as ``lode-fhql.21``) with nothing in the existing gate set able to catch
    it -- this session is that missing validator.

    All of the gate's own logic lives in ``mkdocs.yml``'s ``validation:`` block, NOT here:
    it sets ``links.anchors: warn`` (mkdocs 1.6 logs anchor breakage at INFO by default, so
    ``--strict`` alone would not catch it) and every other link/nav check to ``ignore``, so
    ``--strict`` reddens on a broken anchor and on nothing else. See that block for why each
    ``ignore`` is deliberate. Keeping the predicate in mkdocs' own config rather than
    grepping its log matters both ways round: a renamed validation key is itself a
    ``--strict`` config error (loud), whereas a grep goes silently green the day mkdocs
    rewords a message.

    **Coverage boundary:** ``mkdocs.yml``'s ``docs_dir`` points at the STAGED output of
    ``scripts/build_docs_site.py`` (lode-fhql.9's HUMAN DECISION 2026-08-14 superseded the
    ``exclude_docs`` allowlist this docstring used to describe), so this session only ever
    sees the PUBLISHED set (``index``/``design``/``storage``/``retrieval``/``externals``/
    ``brand``/``keymap``/``settings`` + ``how-to/``) -- nothing else can ship on ``docs_dir``
    by construction. Anchors in and into unpublished pages -- ``decisions.md``, ``stack.md``, ``configuration.md``, ...
    -- are ``linkcheck``'s job alone. These two gates are complements, not duplicates.

    Stages with ``--no-mermaid`` first (copy-only, no Docker) so this session -- in the
    default ``nox`` set -- stays offline; the real, Docker-backed Mermaid pre-render is
    exclusive to ``.github/workflows/docs.yml``. A raw mermaid code fence that ships
    unrendered here is fine: this session gates anchors/links, not diagram rendering.

    Resolves ``mkdocs``/``python`` through ``_venv_tool`` (lode-0yfn) -- an ambient
    interpreter would not have ``mkdocs-material`` (or ``lode``'s other deps) installed.
    """
    python = _venv_tool(session, "python")
    mkdocs = _venv_tool(session, "mkdocs")
    with tempfile.TemporaryDirectory() as site_dir:
        session.run(
            python, "scripts/build_docs_site.py", "--no-mermaid", ".docs-site-src"
        )
        session.run(mkdocs, "build", "--strict", "-d", site_dir)


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

    Resolves ``pytest`` through ``_venv_tool`` (lode-0yfn) — see its docstring.
    """
    session.run(_venv_tool(session, "pytest"), "-m", "not slow", "-n", _xdist_workers())


def _machine_fault(session: nox.Session, why: str) -> NoReturn:
    """Abort a gate with ``GATE_MACHINE_FAULT`` -- the gate could not run (lode-9i2p).

    Deliberately **not** ``session.error``: that is nox's *content*-failure
    channel and exits 1, which ``/land`` reads as "this branch is bad" and
    bounces on. Nothing about the branch's content failed here.
    """
    session.warn(
        f"{session.name}: {why} Exiting {GATE_MACHINE_FAULT}: the GATE could not "
        "run -- this is NOT a verdict on any branch's content."
    )
    sys.exit(GATE_MACHINE_FAULT)


@nox.session
def lock_currency(session: nox.Session) -> None:
    """Verify requirements.lock is still what pyproject.toml resolves to (lode-sys4).

    Local mirror of CI's ``lock-currency`` job (``.github/workflows/tests.yml``)
    -- catches a stale lock before a GitHub Action does, and before ``/land``
    merges a branch carrying one into ``trunk`` (``.claude/skills/land/SKILL.md``
    runs this alongside ``nox -t fix``/``nox -s tests`` in its combined re-gate).

    Recompiles into a **scratch copy seeded with the current committed lock**
    (mirrors CI's in-place recompile: uv feeds an existing output file's own
    pins back to the resolver as its preference set by default, so an
    unrelated upstream release alone reproduces the committed lock
    byte-for-byte, and only a real ``pyproject.toml``-forced move changes it),
    via the single shared ``scripts/compile-lock.sh`` -- never a second copy
    of the ``uv pip compile`` invocation -- then diffs the scratch copy
    against the real committed file.

    Kept OUT of the default ``nox`` session set (like ``eval``/``build``):
    this needs network (PyPI) to resolve, so a bare ``nox`` / ``nox -s tests``
    stays offline.

    **Fails closed** if ``uv`` is not on PATH, per this ticket's decided
    offline/uv-absent behaviour (docs/stack.md, lode-g2741): a silently
    skipped lock check is worse than a noisy one here, since the alternative
    is a stale lock landing on ``trunk`` unnoticed until the public CI badge
    catches it later.

    **Failing closed is not the same as failing indistinguishably** -- so the
    two failure kinds carry two different exit statuses, exactly as
    ``scripts/validate-mermaid.sh`` does (lode-9i2p):

    - **exit 1 -- CONTENT.** The committed lock genuinely disagrees with what
      ``pyproject.toml`` resolves to. Some branch's diff caused this; ``/land``
      may isolate and bounce on it.
    - **exit 2 -- MACHINE.** The gate could not run: ``uv`` is absent, or
      ``scripts/compile-lock.sh`` could not resolve at all (PyPI unreachable, a
      5xx, DNS). This says *nothing* about the content being gated, and
      ``/land`` must stop the pass rather than isolate -- otherwise a transient
      network blip bounces (and deletes) every reviewed branch in the pass,
      each with a fabricated "stale lock" finding. That distinction matters
      more here than for any other gate this repo has: unlike ``nox -t
      fix``/``nox -s tests`` (the offline, keyless default set), this one needs
      ``uv`` present and PyPI reachable on *every* invocation, so its machine
      fault is a live possibility on the one machine that writes ``trunk``.
    """
    if shutil.which("uv") is None:
        _machine_fault(
            session,
            "'uv' not found on PATH -- cannot verify requirements.lock "
            "currency locally (fails closed, lode-sys4). Install uv "
            "(pip install -U uv) and re-run. CI's lock-currency job "
            "(tests.yml) still catches a stale lock on push, but only after "
            "this local gate was skipped.",
        )
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "requirements.lock"
        candidate.write_bytes(Path("requirements.lock").read_bytes())
        try:
            # -q: uv pip compile otherwise echoes the ENTIRE compiled lock
            # (every package + every hash, ~2500 lines) to stdout on top of
            # writing -o -- which would bury the surrounding gate output
            # /land reads to decide which branch to bounce. uv strips -q from
            # the header comment it autogenerates, so the byte-for-byte diff
            # below is unaffected.
            session.run(
                "scripts/compile-lock.sh", "-q", "-o", str(candidate), external=True
            )
        except nox.command.CommandFailed:
            _machine_fault(
                session,
                "scripts/compile-lock.sh could not resolve pyproject.toml -- "
                "PyPI unreachable, a 5xx, or a DNS failure (its own output "
                "above names the cause). The committed lock has NOT been shown "
                "to be stale.",
            )
        # uv's autogenerated header comment records the literal `-o PATH` it
        # was invoked with -- normalize the leaked scratch path back to the
        # real committed filename before diffing, same as
        # scripts/update-deps.sh's own candidate-promotion step does, or
        # every run would show a spurious header-only diff regardless of
        # whether the actual dependency set changed.
        candidate.write_text(
            candidate.read_text().replace(str(candidate), "requirements.lock")
        )
        diff = subprocess.run(
            [
                "git",
                "diff",
                "--no-index",
                "--quiet",
                "requirements.lock",
                str(candidate),
            ],
            check=False,  # non-zero means "lock is stale" -- inspected below, not an error to raise
        )
        if diff.returncode != 0:
            # session.error -> nox exit 1: a CONTENT failure, the one status
            # /land is allowed to attribute to a branch and bounce on.
            session.error(
                "requirements.lock is STALE -- pyproject.toml resolves to "
                "something different than the committed lock. Run "
                "scripts/update-deps.sh (or scripts/compile-lock.sh -o "
                "requirements.lock) and commit the regenerated lock."
            )


@nox.session
def coverage(session: nox.Session) -> None:
    """Run the FULL test suite under pytest-cov and emit a report — CI-only (lode-qxdn.3).

    Measures the SAME suite ``nox -s tests`` certifies — full, slow markers
    included, no ``-m`` filter — so the coverage number describes the same
    suite the tests badge backs, not a narrower one.

    This is a SEPARATE invocation, not an addition to ``tests`` itself: the
    shared ``tests`` session stays bare ``pytest -n`` with no ``--cov``, so
    coverage instrumentation never rides `/land`'s re-gate or a developer's
    local ``nox -s tests`` (settled on lode-qxdn.3 — coverage is reporting,
    not a merge gate).

    Runs under ``pytest-xdist`` (``-n`` from ``LODE_TEST_WORKERS``, default
    ``8``, lode-bv6y — see the module docstring); pytest-cov combines
    coverage data across xdist workers automatically once xdist is active, no
    extra flags needed. Emits a terminal summary plus ``coverage.xml``
    (Cobertura format, written to the cwd) for upload to a coverage service.
    Report-only: enforces no threshold — a low percentage does not fail this
    session.

    Resolves ``pytest`` through ``_venv_tool`` (lode-0yfn) — see its docstring.
    """
    session.run(
        _venv_tool(session, "pytest"),
        "--cov=lode",
        "--cov-report=xml",
        "--cov-report=term",
        "-n",
        _xdist_workers(),
    )


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

    Resolves ``pytest`` through ``_venv_tool`` (lode-0yfn) — see its docstring.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        session.skip("ANTHROPIC_API_KEY not set — eval needs Anthropic credentials")
    session.run(
        _venv_tool(session, "pytest"),
        "tests/test_eval_live.py",
        "-v",
        env={"LODE_RUN_LIVE_EVAL": "1"},
    )
