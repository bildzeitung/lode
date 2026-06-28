"""Live eval integration test — real seams, real credentials (lode-5y8.5).

This test wires the *real* seams the scorer injects: the local ONNX embedder
(:class:`lode.embedding.FastEmbedEmbedder` — deterministic, in-process, no
network for inference) builds the corpus + query vectors, and a real-client
answerer (:func:`lode.cited_answer.ask` with the credential-resolved Anthropic
client) sources the cited answers. The Q&A leg therefore needs
``ANTHROPIC_API_KEY`` and the network.

**Not part of the offline gate.** This file is run only by ``nox -s eval``,
which skips itself when ``ANTHROPIC_API_KEY`` is absent.  A bare ``nox`` and
``nox -s tests`` collect this file but the test skips cleanly before touching
the network (``pytest.skip()`` fires at the top of the test body).
The deterministic offline scorer coverage lives in ``tests/test_eval_harness.py``.

Runs the scorer against a fresh ephemeral store — an in-memory SQLite DB and a
throwaway LanceDB dir — so it never touches the user's real notes and leaves
nothing behind.
"""

import os
import tempfile
from pathlib import Path

import pytest

from lode.cited_answer import ask
from lode.config import Settings
from lode.embedding import FastEmbedEmbedder
from lode.eval.harness import score_golden_set
from lode.storage import init_db


def test_eval_golden_set_live() -> None:
    """Score the golden Q&A set end-to-end with real seams.

    Skips cleanly when ``ANTHROPIC_API_KEY`` is absent — the Q&A leg calls
    Claude, so this is the credentialed CI-style check, never part of the
    offline test gate. Run it explicitly via ``nox -s eval``.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set — live eval needs Anthropic credentials")

    settings = Settings()
    # The local ONNX model is deterministic; no network needed for the
    # embedder itself.  The answerer lambda is where the API key is used.
    embedder = FastEmbedEmbedder(settings)

    # Build the seed corpus into a fresh, empty store so eval never touches
    # the user's real notes and leaves nothing on disk after the test.
    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(":memory:")
        try:
            score = score_golden_set(
                conn,
                lance_dir=Path(tmp) / "vectors",
                embedder=embedder,
                answerer=lambda question, context: ask(
                    conn, question, context, settings=settings
                ),
                settings=settings,
            )
        finally:
            conn.close()

    # Sanity-check metric bounds; the exact values depend on the golden set and
    # the live model responses.  Failures here indicate a regression in either
    # the retrieval pipeline or the cited-answer gate.
    assert 0.0 <= score.recall_at_k <= 1.0, (
        f"recall@{score.k} out of bounds: {score.recall_at_k}"
    )
    assert 0.0 <= score.faithfulness_accuracy <= 1.0, (
        f"faithfulness accuracy out of bounds: {score.faithfulness_accuracy}"
    )
    assert 0.0 <= score.abstention_accuracy <= 1.0, (
        f"abstention accuracy out of bounds: {score.abstention_accuracy}"
    )

    # Surface the metrics so they appear in the nox session output.
    print(f"\nrecall@{score.k}: {score.recall_at_k:.3f}")
    print(f"faithfulness/citation accuracy: {score.faithfulness_accuracy:.3f}")
    print(f"abstention correctness: {score.abstention_accuracy:.3f}")
