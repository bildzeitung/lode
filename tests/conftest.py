"""Shared test fixtures.

The CLI resolves all on-disk state (DB, vector store, logs) under ``$LODE_HOME``
(default ``~/.lode``, lode-qd9). An autouse fixture pins ``$LODE_HOME`` to a
throwaway per-test directory so a test invocation never reads or writes the real
``~/.lode`` — in particular the log file handler the group callback attaches lands
in tmp, not the developer's home. Tests that exercise the default-path behaviour
explicitly still get an isolated, asserted-against directory. That no-touch rule
holds for every piece of lode's *state*; it has exactly one deliberate exception,
the model-weights cache, for the reason below.

**The model-weights cache is the one thing that must NOT be isolated (lode-gmo).**
``$LODE_HOME/models/`` is where every ``fastembed`` loader now caches its ONNX
weights (:func:`lode.config.model_cache_dir`). The slow tier deliberately loads a
*real*, un-mocked ``FastEmbedCrossEncoder`` (see below), so if the throwaway root
carried the weights cache too, every real load would face an empty directory: the
landing gate (``nox -s tests``, which runs the slow tier) would re-download
``BAAI/bge-reranker-base`` from HuggingFace on *every run*, and fail outright with
no network. So the fixture resolves the machine's real cache *before* it overrides
``$LODE_HOME`` and links the throwaway root's ``models/`` at it — the DB, vectors,
and logs stay isolated per test, while the weights are downloaded once per machine
and shared with production.

**A network-touching test must FAIL, never pass silently (lode-85q).** Surfaced by
lode-8xg: a test whose mock was silently a no-op (it patched a symbol ``cli.py``
doesn't have) still reached the *real* ``enrich_version`` -> ``anthropic.Anthropic``
path, and the test still passed. That happens in both directions and neither one
reports "this test touched the network":

- **unkeyed** (CI, no ``ANTHROPIC_API_KEY``): ``anthropic.Anthropic()`` raises
  ``anthropic.AnthropicError`` at *construction* — its own ``active_config``
  profile lookup fails before a socket is ever opened — and
  ``lode.worker.run_one``'s ``except Exception`` (a legitimate, broad job-failure
  handler, not a bug this ticket fixes) swallows it as an ordinary job failure.
  A socket block alone would never fire here: no socket is ever opened.
- **keyed** (a dev machine with the key exported): construction succeeds and a
  real, billed Haiku/Sonnet call goes out.

``_block_unmocked_network_and_llm_access`` below closes both gaps with two
independent guards, each raising ``pytest.fail(...)`` — which raises
``_pytest.outcomes.Failed``, a ``BaseException`` (**not** an ``Exception``)
subclass, so it is guaranteed to blow straight through ``run_one``'s
``except Exception`` (and any other broad ``except Exception`` in the call
chain) rather than being swallowed as just another job failure:

1. **LLM-client construction** — patches ``anthropic.Anthropic.__init__``
   (+ ``AsyncAnthropic`` if present) to fail unconditionally, before the SDK's own
   credential-chain logic runs, so it fires identically whether the environment is
   keyed or not. Also patches ``openai.OpenAI.__init__`` and
   ``openai.AzureOpenAI.__init__`` the same way (lode-568v.3): a test that
   reaches the real ``OpenAIProvider`` construction path with no fake installed
   must fail the same way an unmocked Anthropic path does, not silently
   construct a real client.
2. **Generic outbound socket egress** — patches ``socket.socket.connect`` *and*
   ``socket.socket.connect_ex`` (independent C methods; the latter does not call
   the former, so guarding only ``connect`` would leave the guard failing open)
   to fail on any non-loopback destination. Catches accidental egress through
   anything other than the Anthropic SDK — e.g. a broken ``webfetch`` mock.
   Verified empirically to fire on ``httpx`` (sync *and* async), ``urllib``,
   ``socket.create_connection`` and ``asyncio.open_connection``: they all bottom
   out in a plain ``socket.connect`` (for HTTPS, ``httpcore`` connects the TCP
   socket first and only then wraps it in TLS, so ``ssl.SSLSocket.connect`` is
   never reached).

   Known limits, accepted: a connect made in a **subprocess** is out of reach
   (separate interpreter), and one made in a **thread** prevents the call but
   cannot itself fail the test (``pytest.fail`` in a non-main thread does not
   propagate to the test). lode makes no such calls today; the guard is a net
   for *accidents*, not an adversary.

**Escape hatch (explicit, greppable): ``@pytest.mark.network``** (registered in
``pyproject.toml``) lifts *both* guards for a test that deliberately needs real
Anthropic-SDK / network access — currently
``tests/test_eval_live.py::test_eval_golden_set_live``,
``tests/test_models_smoke.py`` (real HF Hub downloads), and two
``tests/test_auth.py`` cases that construct a real ``anthropic.Anthropic()`` on
purpose to test ``lode.auth.build_client``'s own credential-resolution wrapping.

``@pytest.mark.slow`` additionally lifts *only* guard 2 (not guard 1): the
real-reranker-model-load tier (lode-pql/lode-gmo, see above) may need one HF Hub
download on a cold model cache, a pre-existing, accepted exception this fixture
must not regress — every ``slow`` test already mocks its own Anthropic/QA client,
so guard 1 still protects it.

That relaxation is the guard's **widest residual hole, and it is deliberate**: a
``slow`` test may connect to *any* host, not merely HuggingFace. Host-allowlisting
is not available to us — by the time ``connect()`` is reached the destination has
already been resolved to a bare IP, so there is no hostname left to match on. The
hole is bounded by keeping the ``slow`` tier small (a handful of tests, all of
which load a real model on purpose) and by guard 1 still covering every one of
them. Do not reach for ``slow`` as a way to quiet guard 2 on a test that is not
about a real model load — use ``@pytest.mark.network``, which is greppable and
says what it means.
"""

import asyncio
import importlib.util
import ipaddress
import logging
import os
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from textual.pilot import Pilot

import lode
from lode.config import model_cache_dir

#: lode-kq4v: scrub ambient colour/tty-forcing env vars BEFORE any test module can import
#: ``lode.cli`` and construct its shared ``console``/``err_console`` (see that module's
#: ``console`` docstring, and lode-xgaa). An ambient ``FORCE_COLOR`` in the shell that launched
#: pytest silently reddens every test asserting plain, uncoloured CLI output, on an
#: otherwise-unmodified tree -- OBSERVED landing a real /land pass (lode-kq4v).
#:
#: MECHANISM -- verified by execution against the installed rich (15.0.0), and stated precisely
#: because the loose version ("``Console()`` freezes its TTY check at construction") invites
#: exactly the wrong simplification. ``is_terminal`` is NOT frozen: it is a live property
#: (``rich/console.py``:931) that re-reads ``os.environ`` on every access. What IS frozen is
#: ``_color_system`` -- computed once in ``Console.__init__`` (:708-712) FROM ``is_terminal``,
#: surfaced by the ``color_system`` property (:909) -- and ``color_system`` is what gates whether
#: any ANSI is emitted at all. ``no_color`` and ``is_interactive`` are frozen too, as plain
#: instance attributes.
#:
#: THAT is why this must be top-level module code and NOT an autouse fixture. A fixture runs at
#: test SETUP, after collection has already imported every test module. Scrubbing there does
#: flip ``console.is_terminal`` back to ``False`` -- which is precisely the trap: it LOOKS like
#: it worked, while ``console.color_system`` stays pinned at ``truecolor`` from import and ANSI
#: keeps flowing. Same shape as the ``monkeypatch.setenv("NO_COLOR", "1")`` no-op lode-xgaa
#: documented. ``conftest.py`` is always imported before any test module in its directory
#: (pytest's collection order), including once per ``pytest-xdist`` worker process -- which is
#: what makes module level early enough (verified under ``-n 8``, the landing gate's config).
#:
#: ORDERING CONSTRAINT, for whoever edits the import block above: ruff keeps imports first, so
#: this scrub necessarily sits below them, and is early enough only while nothing imported above
#: constructs a rich ``Console``. True today -- neither ``lode``, ``lode.config`` nor
#: ``textual.pilot`` reaches ``lode.cli`` or ``rich.console``. Adding an import that does would
#: silently half-disable this scrub; ``tests/test_conftest_color_scrub.py`` is what would catch
#: it.
#:
#: ``CLICOLOR_FORCE`` -- named in lode-kq4v's audit requirement -- is read nowhere in this rich
#: version, so there is nothing for it to force and it is deliberately not scrubbed.
#: ``COLORTERM``/``TERM`` only choose a colour *system* once ``is_terminal`` is already ``True``,
#: so they need no scrubbing either.
#:
#: NOT CLOSED BY THIS SCRUB, deliberately (lode-kq4v acceptance A.3): ``pytest -s`` /
#: ``--capture=no`` from a REAL terminal. stdout is then a genuine tty, so ``is_terminal`` is
#: ``True`` at import with none of these four set -- confirmed under a pty -- and the same tests
#: fail. ``docs/stack.md``'s ``rich`` row already records that constraint. Left unfixed ON
#: PURPOSE: the only way to force colour off is to SET ``NO_COLOR=1`` rather than clear it, which
#: would make ``test_config_output_has_no_ansi_when_piped`` vacuous -- it would then pass because
#: colour was forced off, not because the pipe was detected, which is the one thing it exists to
#: assert. ``nox -s tests`` (the landing gate) never passes ``-s``, so no gate is exposed; only a
#: human running pytest by hand is.
#:
#: What each of the four does, and why all four: ``FORCE_COLOR`` forces ``is_terminal`` True over
#: captured (non-tty) stdout -- the one that actually caused lode-kq4v; ``TTY_COMPATIBLE`` forces
#: it True on ``"1"`` and False on ``"0"``; ``TTY_INTERACTIVE`` forces ``is_interactive``.
#: ``NO_COLOR`` forces colour OFF, so it could not have caused lode-kq4v; it is cleared anyway so
#: the decision is deterministic in BOTH directions rather than ambient -- not because any test
#: here wants colour on.
#:
#: Kept as four straight ``pop`` calls rather than a loop over a tuple: commenting them out is
#: then the whole sabotage recipe ``tests/test_conftest_color_scrub.py`` documents for proving
#: itself non-vacuous. (A loop needed a ``del`` of its own control variable, and deleting only
#: the loop body left that ``del`` raising ``NameError`` -- which broke conftest import outright
#: instead of disabling the scrub, so the recipe demonstrated nothing.)
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("TTY_COMPATIBLE", None)
os.environ.pop("TTY_INTERACTIVE", None)
os.environ.pop("NO_COLOR", None)

#: Root of the checkout that owns *this* conftest — the anchor guard 0 compares
#: against. Deliberately derived from ``__file__`` rather than ``Path.cwd()``:
#: the invariant being asserted is "the tests being collected and the ``lode``
#: being imported come from the same checkout", and ``__file__`` states that
#: directly. ``cwd`` only coincides with it when pytest happens to be invoked
#: from the repo root, so anchoring on it would false-positive on a plain
#: ``pytest tests/foo.py`` run from a subdirectory.
_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent


def _wrong_source_tree_message(lode_file: str, checkout_root: Path) -> str | None:
    """Guard 0: is the imported ``lode`` from a *different* checkout than these tests?

    Returns the operator-facing message when it is, or ``None`` when all is
    well. Split out from :func:`pytest_configure` as a pure function purely so
    it is directly testable — the hook itself can only be exercised by running
    pytest under pytest.

    The hazard (lode-jh80, discovered reviewing lode-7abi): ``noxfile.py`` sets
    ``default_venv_backend = "none"``, so gates run in whatever venv is already
    active rather than provisioning one — deliberate, for speed. But
    ``scripts/python-init.sh`` always installs the local package editable
    (``-e .``, whether via the locked default path or ``--unlocked`` --
    lode-g274.1), so a venv's ``lode`` resolves to the ``src`` of *whichever
    checkout it was built in*. Activate the main checkout's venv while sitting
    in a worktree and
    pytest collects **this** checkout's ``tests/`` while importing **that**
    one's ``src``. Nothing warns, and the run reports a result for the wrong
    tree in either direction: a false FAIL when this branch's fix is never
    exercised, or a false PASS when this branch's regression is masked by the
    other checkout's already-correct code.

    The comparison is against ``<checkout_root>/src``, not ``checkout_root``
    itself, and that precision is load-bearing in both directions. Worktrees
    live *inside* the main checkout (``.claude/worktrees/`` — see CLAUDE.md),
    so a plain "is it under the checkout root?" containment test waves through
    the mirror-image mistake: sitting in the main checkout with a *worktree's*
    venv active imports ``/repo/.claude/worktrees/x/src/lode`` while collecting
    ``/repo/tests`` — still under ``/repo``, still the wrong tree, equally
    silent. Anchoring on ``src`` states the real invariant ("the ``lode`` being
    imported is the one whose source this checkout owns") as one rule instead
    of two special cases, and it also rejects a stale non-editable copy
    installed into a ``site-packages`` inside this same checkout. The repo only
    ever installs editable (``scripts/python-init.sh`` always passes ``-e``,
    locked or ``--unlocked`` -- lode-g274.1), so the ``src`` layout is the
    only shape that legitimately occurs.
    """
    expected_src = checkout_root / "src"
    resolved = Path(lode_file).resolve()
    if expected_src in resolved.parents:
        return None
    return (
        "the active venv's `import lode` resolves to\n"
        f"    {resolved}\n"
        f"which is not under the source tree of the checkout that owns these "
        f"tests ({expected_src}).\n"
        "pytest would collect this checkout's tests/ but exercise the other "
        "checkout's src -- a false FAIL if this branch's fix is never run, or "
        "a false PASS if its regression is masked by the other checkout's "
        "already-correct code (lode-jh80).\n"
        "Fix: build and activate THIS checkout's own venv --\n"
        "    ./scripts/python-init.sh && . ./venv/bin/activate"
    )


def pytest_configure(config: pytest.Config) -> None:
    """Fail the run outright if ``lode`` resolves outside this checkout (guard 0).

    Lives here rather than as a ``nox`` preflight so it covers **every** pytest
    invocation — ``nox -s tests``/``unit``/``eval`` and a bare ``pytest -k foo``
    alike — with nothing to remember to wire up per session. A per-session
    opt-in reproduces the very bug class it guards: a silent omission nothing
    catches (the first cut of lode-jh80 wired two of the three source-exercising
    sessions and missed ``eval``).

    Skipped in ``pytest-xdist`` workers (``workerinput``): the controller has
    already made this check, and letting 8 workers each raise the same
    ``UsageError`` would bury one clear message under eight copies.
    """
    if hasattr(config, "workerinput"):
        return
    message = _wrong_source_tree_message(lode.__file__, _CHECKOUT_ROOT)
    if message is not None:
        raise pytest.UsageError(message)


#: Socket families guard 2 polices. Anything else (``AF_UNIX``, ``AF_NETLINK``,
#: …) cannot reach a remote host at all, so blocking it would be a pure false
#: positive — and a baffling one, since the failure message talks about network
#: egress.
_EGRESS_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})


def _is_loopback(address: object) -> bool:
    """Is this ``connect()`` destination the local machine?

    Loopback stays permitted: existing offline tests deliberately connect to a
    *refused* loopback port to exercise a real ``ConnectionRefusedError``
    without standing up a fake server (e.g.
    ``tests/test_webfetch.py::TestHttpxFetcher::test_connection_error_is_transient``
    against ``127.0.0.1:1``). Decided by ``ipaddress``, not a hardcoded set of
    strings, so the whole ``127.0.0.0/8`` block counts — ``127.0.1.1`` is the
    stock Debian/Ubuntu ``/etc/hosts`` alias for the machine's own hostname and
    is every bit as loopback as ``127.0.0.1``.
    """
    host = address[0] if isinstance(address, tuple) else address
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        # Not a literal IP -- a hostname. ``connect()`` is normally handed an
        # already-resolved address (``socket.create_connection`` resolves via
        # ``getaddrinfo`` first), so this is the rare direct-``connect()`` case.
        return str(host) == "localhost"


def _make_guarded_connect(method_name: str):
    """Wrap ``socket.socket.<method_name>`` so non-loopback egress fails the test.

    Passes straight through for a loopback destination, and for any socket
    family that cannot reach a remote host in the first place (see
    :data:`_EGRESS_FAMILIES`).
    """
    real = getattr(socket.socket, method_name)

    def _guarded(
        self: socket.socket, address: object, *args: object, **kwargs: object
    ) -> object:
        if self.family in _EGRESS_FAMILIES and not _is_loopback(address):
            pytest.fail(
                f"test attempted a real outbound network connection to "
                f"{address!r} (socket.{method_name}) -- no fake was installed "
                "for it. If this test genuinely needs live network access, opt "
                "in with @pytest.mark.network (tests/conftest.py)."
            )
        return real(self, address, *args, **kwargs)

    return _guarded


@pytest.fixture(autouse=True)
def _isolate_lode_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolve the real, durable weights cache BEFORE $LODE_HOME is redirected --
    # this is the machine-level cache production uses. Via model_cache_dir(), so
    # the layout is resolved in exactly one place: hand-building $LODE_HOME/models
    # here would silently aim the symlink at a stale path the day that function
    # moves, reinstating the very re-download this fixes (lode-gmo).
    durable_models = model_cache_dir()
    durable_models.mkdir(parents=True, exist_ok=True)

    home = tmp_path_factory.mktemp("lode-home")
    (home / "models").symlink_to(durable_models, target_is_directory=True)
    monkeypatch.setenv("LODE_HOME", str(home))


@pytest.fixture(autouse=True)
def _restore_root_logger_state():
    """Snapshot and restore the root logger's level + handler set (lode-kmes).

    ``lode.logconfig.configure_logging`` sets the root logger's level directly
    (``root.setLevel(resolved)``) and never restores it — a global,
    process-wide mutation with no matching teardown. Under ``pytest-xdist``, a
    worker process runs many tests in sequence, so this is not scoped to one
    test: ``tests/test_cli.py``'s ``--debug`` flag tests (e.g.
    ``test_debug_flag_sets_debug_log_level``) resolve an explicit ``DEBUG``
    level, which — with nothing restoring it — leaves the ENTIRE root logger
    at ``DEBUG`` for every later test that worker process happens to run.

    That is the confirmed root cause of an intermittent whole-suite hang at
    ~98% completion under xdist (OBSERVED TWICE in one ``/land`` pass, see the
    ticket): a later test in the same worker that mounts
    :class:`~lode.tui.screens.capture.CaptureScreen` unknowingly enables its
    DEBUG-gated, never-returning latency-probe worker
    (:func:`lode.tui.latency_probe.probe_event_loop_lag` — its own docstring:
    "Run forever ... it never exits on its own", stopped only by Textual's
    worker cancellation on a clean screen unmount). If that test's teardown
    doesn't get a clean unmount, the worker keeps that test's asyncio event
    loop alive indefinitely and the whole session never finishes — exactly the
    observed symptom (a live, DEBUG-logging Textual event loop with zero CPU
    progress on the pytest master).

    Restoring the level — and closing/removing any handler the test attached
    that wasn't there before it (``configure_logging`` also attaches a file
    handler when given a ``log_dir``, e.g. via the CLI group callback under
    ``$LODE_HOME/logs``) — after every test closes the leak at its source,
    independent of which specific test happens to trigger it. This deliberately
    does more than restore the level — it also closes/removes any handler a
    test attached (it does not re-add a handler a test *removed*, nor strip a
    filter added to a pre-existing handler; neither leak is known to matter
    here) — per the ticket's acceptance criterion to check for other global
    state leaking the same restore-nothing way.
    """
    root = logging.getLogger()
    level_before = root.level
    handlers_before = list(root.handlers)
    yield
    root.setLevel(level_before)
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()


@pytest.fixture
def set_console_width(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[int], None]:
    """Force ``lode.cli``'s shared ``console`` to a given width for one test.

    rich's ``Console()`` reads ``COLUMNS`` from the environment ONCE, at
    CONSTRUCTION -- but only CONDITIONALLY: ``Console.__init__`` sets
    ``self._width`` from ``os.environ["COLUMNS"]`` whenever the ``width``
    constructor arg is ``None`` (lode's case) AND ``COLUMNS`` happens to be
    present in the environment at that moment (verified against the installed
    rich 15.0.0). Once baked, ``Console.size`` short-circuits on ``self._width
    is not None`` and never re-reads the environment again -- for the REST OF
    THE PROCESS'S LIFETIME.

    For a real one-shot ``lode config`` invocation that is harmless (import time
    and render time are the same moment). It is NOT harmless in this test suite:
    pytest-xdist imports ``lode.cli`` ONCE per worker process and reuses the same
    ``console`` singleton across every test that worker runs -- so whichever
    ``COLUMNS`` happened to be in THAT worker's environment at its first import
    of ``lode.cli`` (observed here: '80', inherited from outside pytest's own
    control) freezes the console's width for every subsequent test in that
    worker, and a later ``runner.invoke(..., env={"COLUMNS": ...})`` override has
    NO effect -- confirmed empirically: it changes ``os.environ`` for the call,
    but ``console.size``'s early-return never re-reads it.

    This reaches past that freeze by monkeypatching the private
    ``_width``/``_height`` attributes (auto-reverted after the test). Reaching
    into a private attribute is deliberate: lode-l38d.1 decided AGAINST adding a
    production test seam to the shared ``Console`` (``docs/stack.md``), so a
    test-only reach-in is the compromise that decision implies.

    It is also deliberately NOT the NO_COLOR subprocess technique used in
    tests/test_cli_console.py: that module exists to verify rich's *env detection
    mechanism itself*, whereas callers of this fixture only care about
    Table/``overflow="fold"`` *rendering* behaviour at a given width, which does
    not require re-proving env detection.

    Shared here rather than copied per module (lode-l38d.4 review): the rationale
    above is pinned to rich's internals, so two copies would silently drift apart
    on a rich upgrade.
    """

    def _set(width: int) -> None:
        # Imported lazily: lode.cli's import graph is expensive (~1.3s), and a
        # module-level import here would charge every test session for it.
        from lode.cli import console

        monkeypatch.setattr(console, "_width", width)
        monkeypatch.setattr(console, "_height", 24)

    return _set


#: Every module that must NOT be resident for an "is the SDK imported?" assertion
#: to mean anything: the SDK itself, plus the two lode modules whose job is to stay
#: cheap to import so a credential-free drain never pulls it in (lode-4q97).
_SDK_IMPORT_GRAPH = ("anthropic", "lode.auth", "lode.enrich")


@pytest.fixture
def forget_sdk_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evict the Anthropic import graph from ``sys.modules`` (lode-4q97).

    Tests that assert "this code path did not import the SDK" are **vacuously
    green** unless the modules are evicted first -- an earlier test in the session
    (or the asserting test file's own module-level imports) will already have them
    resident. Centralised here so the eviction set has ONE home: a hand-copied set
    that misses a newly-added SDK-importing module does not fail, it silently stops
    guarding, which is the expensive failure mode.

    **Restoring `sys.modules` alone is not enough.** A test using this fixture may
    still legitimately re-import one of these modules (``worker.drain``, for one,
    imports ``lode.auth`` unconditionally on every drain). Importing ``lode.X``
    binds the new module object as an **attribute of the ``lode`` package** as well
    as into ``sys.modules`` -- and ``monkeypatch.delitem`` only restores the latter.
    Left unrestored, ``lode.enrich`` (the attribute) and ``sys.modules[...]`` end up
    as two *different* module objects, and a later test that patches one while the
    code under test resolves the other silently talks to the real Anthropic SDK.
    That is not hypothetical: it broke lode-9yy's
    ``test_drain_still_runs_embed_jobs_when_credentials_are_missing``, which patches
    ``lode.enrich.build_client``. So record the package attributes too -- setting
    each to its current value makes monkeypatch restore that value at teardown.
    """
    for name in _SDK_IMPORT_GRAPH:
        monkeypatch.delitem(sys.modules, name, raising=False)

    for name in _SDK_IMPORT_GRAPH:
        pkg_name, _, attr = name.rpartition(".")
        pkg = sys.modules.get(pkg_name)
        if pkg is not None and hasattr(pkg, attr):
            monkeypatch.setattr(pkg, attr, getattr(pkg, attr))


@pytest.fixture(autouse=True)
def _block_unmocked_network_and_llm_access(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail loudly, not silently, if a test reaches real network/LLM access.

    See the module docstring ("A network-touching test must FAIL...", lode-85q)
    for the full rationale. ``@pytest.mark.network`` lifts both guards below;
    ``@pytest.mark.slow`` additionally lifts guard 2 only.
    """
    marked_network = request.node.get_closest_marker("network") is not None
    marked_slow = request.node.get_closest_marker("slow") is not None

    # Guard 1: real anthropic.Anthropic()/AsyncAnthropic() construction.
    # Unconditional regardless of ANTHROPIC_API_KEY -- it fires before the SDK's
    # own credential-chain logic runs, so it closes the unkeyed-CI gap (where
    # construction itself raises, before any socket opens) exactly as it closes
    # the keyed-dev-machine gap.
    if not marked_network:
        import anthropic

        def _blocked_init(self: object, *args: object, **kwargs: object) -> None:
            pytest.fail(
                "test constructed a real anthropic.Anthropic client -- no fake "
                "was installed for it. If this test genuinely needs live "
                "Anthropic access, opt in with @pytest.mark.network "
                "(tests/conftest.py)."
            )

        monkeypatch.setattr(anthropic.Anthropic, "__init__", _blocked_init)
        if hasattr(anthropic, "AsyncAnthropic"):
            monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _blocked_init)

        # Same guard for the OpenAI/Azure provider (lode-568v.3) -- a test
        # reaching real openai.OpenAI()/AzureOpenAI() construction with no
        # fake installed must fail loudly, the same as the Anthropic case
        # above.
        import openai

        def _blocked_openai_init(self: object, *args: object, **kwargs: object) -> None:
            pytest.fail(
                "test constructed a real openai.OpenAI/AzureOpenAI client -- "
                "no fake was installed for it. If this test genuinely needs "
                "live OpenAI/Azure access, opt in with @pytest.mark.network "
                "(tests/conftest.py)."
            )

        monkeypatch.setattr(openai.OpenAI, "__init__", _blocked_openai_init)
        monkeypatch.setattr(openai.AzureOpenAI, "__init__", _blocked_openai_init)
        if hasattr(openai, "AsyncOpenAI"):
            monkeypatch.setattr(openai.AsyncOpenAI, "__init__", _blocked_openai_init)
        if hasattr(openai, "AsyncAzureOpenAI"):
            monkeypatch.setattr(
                openai.AsyncAzureOpenAI, "__init__", _blocked_openai_init
            )

    # Guard 2: generic outbound socket egress (any non-loopback destination).
    # Relaxed for @pytest.mark.slow too -- see module docstring.
    #
    # Both connect() AND connect_ex() are patched: they are independent C
    # methods (connect_ex does not call connect), so guarding only the former
    # would leave the guard failing *open* on the latter -- and a guard that
    # silently misses is worse than no guard, because it licenses false
    # confidence.
    if not marked_network and not marked_slow:
        for _method in ("connect", "connect_ex"):
            monkeypatch.setattr(
                socket.socket, _method, _make_guarded_connect(_method), raising=True
            )


@pytest.fixture(scope="session", autouse=True)
def _cache_cross_encoder_model_load():
    """Session-cache the real, un-mocked ``FastEmbedCrossEncoder`` model load.

    Slow-tier tests (``@pytest.mark.slow``, lode-pql) deliberately leave the
    reranker un-mocked to exercise the real path, but ``retrieval.rerank``
    constructs a *fresh* ``FastEmbedCrossEncoder(settings)`` per call (no
    ``scorer`` passed in) — so each slow test paid the full ``fastembed``
    ``TextCrossEncoder`` model load again, which ``pytest --durations``
    measured as the dominant per-test cost (3-9s each, lode-b4w.6).

    The loaded model is stateless inference weights, so caching one instance
    per cache key for the whole test session and handing it to every
    ``FastEmbedCrossEncoder`` that asks for it changes no test's observable
    behavior (identical to a production process that reuses one warm reranker
    across multiple ``ask``s) — it just pays the load cost once instead of once
    per slow test. Scoped to the session (not narrower) so it survives across
    test *functions*; under ``pytest-xdist`` each worker is its own process,
    so the cache is naturally per-worker (loads once per worker, not once per
    test) rather than shared globally.

    That "no observable behavior change" claim holds only because the key is
    ``(model name, TextCrossEncoder class)`` and not the model name alone
    (lode-vzwn). The cached value is a product of *both*: ``_load`` builds it by
    calling whichever ``TextCrossEncoder`` is bound on the ``fastembed`` module
    at call time. So a test that observes a *side effect of the real load* rather
    than its return value — by monkeypatching that constructor to record its
    kwargs, the way ``test_load_passes_durable_model_cache_dir`` (in
    tests/test_retrieval.py) does — is keyed on its own fake class, always
    MISSES, and always gets a real load whose constructor call actually runs.

    Keying on the model name alone under-keys the cache: that test would take a
    HIT off whatever slow-tier test already loaded the same model name on that
    xdist worker, the real constructor would never run, its recorded side effect
    would never happen, and the assertion would fail with ``KeyError:
    'cache_dir'`` — a coin flip on test order (``pytest-randomly``). That was
    the bug. The key, not a convention, is what prevents it: a test that fakes
    the constructor cannot take a hit even if it uses the default model name.

    Two things this does NOT cover, so don't read it as a blanket guarantee:

    * It only protects a side effect that is observed *through the constructor*.
      A test that asserted on some other side effect of a real load (a file
      appearing on disk, say) without faking ``TextCrossEncoder`` would still
      take a hit and still be order-dependent.
    * It only applies to ``FastEmbedCrossEncoder``. ``FastEmbedEntailmentScorer``
      (lode/faithfulness.py) has a near-identical ``_load`` and a near-identical
      test, but is deliberately *not* cached here — which is the only reason its
      test is safe. If that load is ever session-cached too, key it the same way,
      or that test starts flaking exactly as this one did.
    """
    from lode.retrieval import FastEmbedCrossEncoder

    cache: dict[tuple[str, object], object] = {}
    original_load = FastEmbedCrossEncoder._load

    def _cached_load(self: FastEmbedCrossEncoder) -> object:
        if self._model is None:
            from fastembed.rerank import cross_encoder

            # The class, not just the name: see the docstring. A monkeypatched
            # TextCrossEncoder is a different key, so it can never take a hit.
            key = (self._model_name, cross_encoder.TextCrossEncoder)
            if key not in cache:
                cache[key] = original_load(self)
            self._model = cache[key]
        return self._model

    patcher = pytest.MonkeyPatch()
    patcher.setattr(FastEmbedCrossEncoder, "_load", _cached_load)
    yield
    patcher.undo()


# --- Load a repo-root script/module by explicit file path (lode-7ed9) ------
#
# scripts/ is a plain directory of standalone scripts, not an installed
# package, and noxfile.py sits one level above tests/ -- neither is reachable
# by a name-based import, so both are loaded straight from their file path.
#
# Three call sites (tests/test_check_links.py, tests/test_model_cache_key_
# script.py, tests/test_noxfile_venv_tool.py) had independently reimplemented
# this spec_from_file_location -> module_from_spec -> exec_module sequence,
# and had already drifted: the first two registered the loaded module in
# sys.modules BEFORE exec_module and the third did not, while only the third
# narrowed the Optional spec. This helper is the union, so a fourth call site
# gets both without whoever writes it having to know about either.


def load_module_from_path(name: str, path: Path) -> ModuleType:
    """Load the script/module at ``path`` under module name ``name``.

    Registers the module in ``sys.modules`` before executing it. That is
    load-bearing, not defensive: a module defining a dataclass fails outright
    without it, because ``dataclasses`` looks the class's own module up via
    ``sys.modules`` during class creation. scripts/check_links.py has a frozen
    ``@dataclass`` and dies with ``AttributeError: 'NoneType' object has no
    attribute '__dict__'`` if it is executed unregistered.

    The registration is permanent for the session -- nothing evicts it -- so
    ``name`` must not be a name anything else imports: a hypothetical
    scripts/build.py loaded as ``"build"`` would displace the real ``build``
    distribution for every later test in that worker. The assert below does
    NOT catch that; the module being displaced is typically not yet resident
    when the load happens. It catches the collision that always *is*
    detectable -- loading the same name a second time -- which is a genuine
    hazard for any caller whose module has import-time side effects.
    """
    assert name not in sys.modules, (
        f"{name!r} is already in sys.modules -- loading {path} under that "
        f"name would replace it for the rest of the session"
    )
    spec = importlib.util.spec_from_file_location(name, path)
    # Type-narrowing only, not a real failure mode: spec_from_file_location
    # returns None only when no loader claims the suffix, which cannot happen
    # for the .py paths this is called with.
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- TUI test settle helpers (lode-lcju) -----------------------------------
#
# The ONE home for both of lode's settle-under-load patterns for driving a
# Textual pilot -- see docs/tui.md's "Settling TUI tests under load" section
# for the ruling (which helper applies when) and the verified mechanism
# (wait_for_idle's CPU-vs-wall-clock heuristic is the only load-sensitive
# element in the path; asyncio's ready-queue ordering is not perturbed by OS
# starvation). Moved here verbatim from tests/test_tui_reconcile_screen.py
# (_wait_until, lode-64jn) and tests/test_tui_browse_screen.py
# (_press_and_settle, lode-9y68), which had independently invented the same
# fix twice with no cross-reference. Do not add a third dialect in a new test
# file -- import one of these two instead.

#: How often :func:`_wait_until` re-checks its predicate.
_POLL_INTERVAL = 0.01


async def _wait_until(
    predicate: Callable[[], bool], description: str, *, timeout: float = 5.0
) -> None:
    """Poll ``predicate`` until true, bounded by a real ``timeout`` (lode-64jn).

    Use for a PRECONDITION (e.g. "the new screen has finished composing"),
    never for the test's own expected assertion value -- see docs/tui.md's
    "Settling TUI tests under load" section (lode-lcju) for the full rule and
    why: baking the assertion's expected value into the predicate is the
    retry-on-assertion antipattern, which masks a real bug as a slow-to-settle
    one instead of failing where it happens.

    ``description`` names the condition, so a timeout says *which* wait hung.

    Yields the event loop via ``asyncio.sleep`` between checks -- a genuine
    cooperative yield -- rather than Textual's ``pilot.pause()`` no-arg form,
    which ultimately waits on a CPU-idle *heuristic*
    (``textual._wait.wait_for_idle``): it compares this process's own CPU time
    against wall-clock time and calls it "idle" once CPU time stops advancing.
    Under real machine contention (several agents gating at once, e.g.
    ``/code`` fan-out) that heuristic can misfire -- a process starved of
    scheduler time by unrelated load barely advances its own CPU time
    *regardless* of whether the screen transition it is supposed to be waiting
    for has finished, and the heuristic reads that starvation as idleness.
    Polling the real condition instead waits exactly as long as it takes, and
    fails loudly (an explicit ``AssertionError``, never a silent false-idle
    pass) if the condition genuinely never becomes true.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout}s waiting until {description}"
            )
        await asyncio.sleep(_POLL_INTERVAL)


async def _press_and_settle(pilot: Pilot, *keys: str) -> None:
    """Press each key one at a time, settling after EACH one (lode-9y68).

    Use for a STATEFUL, read-back keystroke cascade -- a later key's behavior
    depends on state an earlier key's cascade produced (e.g. incremental
    search, where a scan reads the cursor's current row as its start) -- where
    a precondition predicate would have to restate the test's own expected
    value and so ``_wait_until`` is the wrong tool. See docs/tui.md's
    "Settling TUI tests under load" section (lode-lcju) for the full rule.

    ``pilot.press(*keys)`` (``pilot.py``) is::

        await self._app._press_keys(keys)   # ALL keys, paced by heuristic only
        await self._wait_for_screen()       # ONE real drain, at the very end

    ``App._press_keys`` (``app.py``) paces BETWEEN keystrokes with nothing but
    ``wait_for_idle()`` -- and that is a wall-clock-vs-``process_time()``
    comparison, so a process merely *starved of timeslices* (this machine
    legitimately runs several concurrent ``nox -s tests`` invocations) reads as
    "idle" while a cascade is still in flight. The real drain comes only once,
    after the last key. So the next key can be dispatched mid-cascade.

    The fix is just to press ONE key per call: ``pilot.press(key)`` ends with
    its own ``_wait_for_screen()``, so a real message-count drain -- not the
    CPU heuristic -- separates every keystroke from the next.

    NARROW BY DESIGN: a plain multi-key ``pilot.press("down", "down", "down")``
    is fine and deliberately left alone where used -- cursor moves are
    order-preserving and carry no read-back dependency between keys, and
    ``press()``'s trailing drain covers the final read. The trigger is the
    stateful read-back above, not multi-key presses in general.

    The trailing ``pilot.pause()`` below is NOT part of that mechanism -- it is
    just this suite's ordinary post-keystroke dialect, kept so call sites read
    like their ~90 siblings. ``press(key)`` alone is already sufficient. Do not
    read it as load-bearing, and do not add more drains to settle a future
    flake: a fixed count of drains neither waits longer under worse load nor
    reports anything when it is insufficient. ``wait_for_idle``'s clock
    comparison is the only load-sensitive element in this path.
    """
    for key in keys:
        await pilot.press(key)
        await pilot.pause()
