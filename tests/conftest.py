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
2. **Generic outbound socket egress** — patches ``socket.socket.connect`` to fail
   on any non-loopback destination (catches accidental egress through anything
   other than the Anthropic SDK, e.g. a broken ``webfetch`` mock).

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
"""

import socket

import pytest

from lode.config import model_cache_dir

#: Destinations guard 2 still permits — loopback-only. Existing offline tests
#: deliberately connect to a refused loopback port to exercise a real
#: ``ConnectionRefusedError`` without a fake server (e.g.
#: ``tests/test_webfetch.py::TestHttpxFetcher::test_connection_error_is_transient``
#: against ``127.0.0.1:1``); this keeps that pattern legal while still blocking
#: any *real*, non-loopback egress.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


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
    if not marked_network and not marked_slow:
        real_connect = socket.socket.connect

        def _guarded_connect(
            self: socket.socket, address: object, *args: object, **kwargs: object
        ) -> object:
            host = address[0] if isinstance(address, tuple) else address
            if host in _LOOPBACK_HOSTS:
                return real_connect(self, address, *args, **kwargs)
            pytest.fail(
                f"test attempted a real outbound network connection to "
                f"{address!r} -- no fake was installed for it. If this test "
                "genuinely needs live network access, opt in with "
                "@pytest.mark.network (tests/conftest.py)."
            )
            return None

        monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


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
