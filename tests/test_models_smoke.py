"""Smoke test: every pinned local model loads on the fastembed ONNX runtime.

lode-txh.6 (DECIDE-ONCE + spike). The three local models pinned in
:mod:`lode.config` — embedder, reranker, and the NLI/entailment scorer — all run
in-process on the bundled ONNX runtime via ``fastembed`` (docs/stack.md,
docs/configuration.md). This test is the spike's standing proof: it actually
loads each pinned id and runs a tiny inference, asserting the embedder's output
dimension matches the pinned build constant ``embedding_vector_dim``.

Loading downloads the model files (hundreds of MB) from the HF Hub, so the test
is **opt-in**: it is skipped unless ``LODE_SMOKE_MODELS=1`` is set, keeping the
default ``nox -s tests`` gate fast and offline. Run the spike with::

    LODE_SMOKE_MODELS=1 pytest tests/test_models_smoke.py
"""

import os

import pytest

from lode.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("LODE_SMOKE_MODELS") != "1",
    reason="model-download smoke test; set LODE_SMOKE_MODELS=1 to run",
)


def test_embedder_loads_and_dim_matches_pin() -> None:
    from fastembed import TextEmbedding

    s = Settings()
    emb = TextEmbedding(model_name=s.embedding_model)
    vector = next(iter(emb.embed(["search_document: lode smoke test"])))
    assert len(vector) == s.embedding_vector_dim


def test_reranker_loads_and_scores() -> None:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    s = Settings()
    ce = TextCrossEncoder(model_name=s.rerank_model)
    scores = list(ce.rerank("what is lode?", ["lode is a personal KB", "bananas"]))
    # The relevant passage must outscore the irrelevant one.
    assert len(scores) == 2
    assert scores[0] > scores[1]


def test_entailment_model_loads_via_pinned_loader() -> None:
    # fastembed ships no dedicated NLI model, so the cross-encoder is repurposed
    # as the entailment scorer via the same TextCrossEncoder loader.
    assert Settings().entailment_loader == "fastembed-cross-encoder"
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    s = Settings()
    ce = TextCrossEncoder(model_name=s.entailment_model)
    scores = list(ce.rerank("lode stores notes you learn at work", ["lode is a KB"]))
    assert len(scores) == 1
