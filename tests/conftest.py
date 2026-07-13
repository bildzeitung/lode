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
   keyed or not.
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

import ipaddress
import socket
import sys

import pytest

from lode.config import model_cache_dir

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
    per model name for the whole test session and handing it to every
    ``FastEmbedCrossEncoder`` that asks for it changes no test's observable
    behavior (identical to a production process that reuses one warm
    reranker across multiple ``ask``s) — it just pays the load cost once
    instead of once per slow test. Scoped to the session (not narrower) so
    it survives across test *functions*; under ``pytest-xdist`` each worker
    is its own process, so the cache is naturally per-worker (loads once per
    worker, not once per test) rather than shared globally.
    """
    from lode.retrieval import FastEmbedCrossEncoder

    cache: dict[str, object] = {}
    original_load = FastEmbedCrossEncoder._load

    def _cached_load(self: FastEmbedCrossEncoder) -> object:
        if self._model is None:
            if self._model_name not in cache:
                cache[self._model_name] = original_load(self)
            self._model = cache[self._model_name]
        return self._model

    patcher = pytest.MonkeyPatch()
    patcher.setattr(FastEmbedCrossEncoder, "_load", _cached_load)
    yield
    patcher.undo()
