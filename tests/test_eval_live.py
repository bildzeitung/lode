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

**Pass bar (lode-7lp).** Baseline recorded 2026-07-02 against this golden fixture
(25 answerable items + 8 abstain items, ``k=20``) over two independent live runs,
both identical:

* recall@20: 1.000
* faithfulness/citation accuracy: 1.000
* abstention correctness: 1.000

Floors are set at ``0.95`` for every metric — one item's worth of tolerance on
the smallest graded population (25 answerable items: 24/25 = 0.960 clears the
floor, 23/25 = 0.920 does not; abstention's 33-item population has an even
wider margin). The Q&A leg is a live, temperature-sampled LLM call, so
byte-for-byte reproducibility isn't guaranteed run to run; pinning the floor at
the observed 1.000 would make the gate flake on a single incidental miss. 0.95
absorbs that noise while still failing any real multi-item regression. See
``docs/decisions.md`` (eval-harness entry) for the weighting/curation policy
this floor implements.
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

#: Per-metric pass-bar floors (lode-7lp) — see the module docstring for the
#: recorded baseline (1.000/1.000/1.000) and the rationale for the 0.95 margin.
RECALL_FLOOR = 0.95
FAITHFULNESS_FLOOR = 0.95
ABSTENTION_FLOOR = 0.95


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

    # Enforce the pass bar: each metric must clear its floor (module docstring
    # has the recorded baseline + rationale). A drop below floor means a real
    # regression in the retrieval pipeline or the cited-answer gate — not
    # bounds-only sanity checking, which let 0% recall pass silently.
    assert score.recall_at_k >= RECALL_FLOOR, (
        f"recall@{score.k} {score.recall_at_k:.3f} below floor {RECALL_FLOOR}"
    )
    assert score.faithfulness_accuracy >= FAITHFULNESS_FLOOR, (
        f"faithfulness accuracy {score.faithfulness_accuracy:.3f} below floor "
        f"{FAITHFULNESS_FLOOR}"
    )
    assert score.abstention_accuracy >= ABSTENTION_FLOOR, (
        f"abstention accuracy {score.abstention_accuracy:.3f} below floor "
        f"{ABSTENTION_FLOOR}"
    )

    # Surface the metrics so they appear in the nox session output.
    print(f"\nrecall@{score.k}: {score.recall_at_k:.3f}")
    print(f"faithfulness/citation accuracy: {score.faithfulness_accuracy:.3f}")
    print(f"abstention correctness: {score.abstention_accuracy:.3f}")
