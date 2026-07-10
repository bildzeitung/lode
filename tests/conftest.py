"""Shared test fixtures.

The CLI resolves all on-disk state (DB, vector store, logs) under ``$LODE_HOME``
(default ``~/.lode``, lode-qd9). An autouse fixture pins ``$LODE_HOME`` to a
throwaway per-test directory so a test invocation never reads or writes the real
``~/.lode`` — in particular the log file handler the group callback attaches lands
in tmp, not the developer's home. Tests that exercise the default-path behaviour
explicitly still get an isolated, asserted-against directory.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_lode_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path_factory.mktemp("lode-home")
    monkeypatch.setenv("LODE_HOME", str(home))


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
