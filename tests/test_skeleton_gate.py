"""Phase-A exit gate (lode-6w1.1 / lode-6w1.2): add → embed → ask → cited claim / abstain → eval green.

Verification gate for the walking skeleton end-to-end. Three sub-gates together
close Phase A and unblock the deepening tasks:

1. ``lode add`` saves a note **and runs chunk+embed+FTS inline** (lode-x6r.2
   intent, lode-6w1.2 fix) so the note is immediately keyword- and
   vector-findable; ``lode ask`` retrieves it, sends the context to the Q&A
   step, and the faithfulness gate verifies the claimed ``quoted_span`` is
   verbatim in the cited version's stored body — the cited claim survives and
   renders. No in-test derive-job stand-in: the real CLI ``add`` command
   performs the embed.

2. An out-of-corpus question against the same corpus ends in honest abstention
   when the Q&A step asserts nothing: the gate finds no surviving claim and the
   abstention line is printed.

3. The deterministic offline eval scorer (``score_golden_set`` over the golden
   fixture, stub embedder + oracle answerer) reports perfect recall@k,
   faithfulness, and abstention — the offline analog of ``lode eval``.

All three run offline and reproducibly. The two non-deterministic seams are
replaced:

- **Embedder**: ``_ConstantEmbedder`` — a deterministic, constant-direction stub
  that produces dim-768 vectors (the LanceDB table width the embed job creates
  under the default ``Settings``).  No fastembed model download.  Injected via
  ``monkeypatch`` into ``lode.embedding.FastEmbedEmbedder`` so it is used by
  both ``lode add`` (inline embed) and ``lode ask`` (query embedding).
- **Q&A client**: ``_FakeClient`` — returns known claims for the in-corpus question
  and no claims for the out-of-corpus question.  No Anthropic API call.

The faithfulness gate is REAL and runs inline: every ``quoted_span`` that survives
must be verbatim in the retrieved version body.  The span check in the assertion
below is *the same predicate the gate uses* — so if the assertion passes, the gate
would have passed too (it already did, else the CLI would have abstained).
"""

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lode import cli
from lode.answer import Claim, Support
from lode.cli import app
from lode.config import Settings, load_settings
from lode.eval.golden import golden_set
from lode.eval.harness import score_golden_set
from lode.faithfulness import span_occurs
from lode.storage import init_db

runner = CliRunner()

# ---------------------------------------------------------------------------
# In-corpus note: the skeleton is exercised on a known, controlled body.
# The quoted_span must be a verbatim substring of the body — it is what the
# fake Q&A returns and what the faithfulness gate verifies.
# ---------------------------------------------------------------------------
_NOTE_BODY = (
    "We settled on exponential backoff for retry logic in the API client, "
    "doubling the delay on each attempt up to a 30-second ceiling."
)
_QUOTED_SPAN = "exponential backoff for retry logic in the API client"
_IN_CORPUS_QUESTION = "What retry strategy did we settle on for the API client?"
_OUT_OF_CORPUS_QUESTION = "How does quantum computing leverage superposition?"


# ---------------------------------------------------------------------------
# Stubs: a deterministic embedder (no model download) and a fake Q&A client
# ---------------------------------------------------------------------------


class _ConstantEmbedder:
    """A deterministic, constant-direction stub: every text maps to [1, 0, 0, ...].

    Dim matches whatever ``settings.embedding_vector_dim`` says (768 by default
    when the CLI uses ``Settings()``), so the query vector and the indexed passage
    vectors live in the same space — the cosine ANN will find them.  No semantic
    signal, but the dense leg can surface the one indexed note.
    """

    def __init__(self, settings: Settings) -> None:
        self._dim = settings.embedding_vector_dim

    def _vector(self) -> list[float]:
        return [1.0] + [0.0] * (self._dim - 1)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector() for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector()


class _HashEmbedder:
    """Offline eval stub: same text → same fixed-dim vector (no semantic meaning).

    Used for the golden-set eval (Gate 3) where the scoring harness exercises
    the metric plumbing, not retrieval quality.  Recall is carried by the
    model-free FTS5 leg.  Both methods hash into the same space so query and
    passage vectors are directly comparable under the cosine ANN.
    """

    DIM = 8

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append([digest[i] / 255.0 for i in range(self.DIM)])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[i] / 255.0 for i in range(self.DIM)]


class _FakeMessages:
    """Records every parse() call and returns ``claimed`` claims."""

    def __init__(self, claimed: list[Claim]) -> None:
        self._claimed = claimed
        self.calls: list[dict] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=SimpleNamespace(claims=self._claimed))


class _FakeClient:
    """Mock Anthropic client — returns ``claimed`` for every call, no network."""

    def __init__(self, claimed: list[Claim]) -> None:
        self.messages = _FakeMessages(claimed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_head_version_id(db_path: Path, note_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        assert row is not None, f"note {note_id!r} not found in DB"
        return row[0]
    finally:
        conn.close()


def _oracle_answerer(question, context):
    """Oracle for the eval harness: returns known-good citations for the golden set."""
    from lode.cited_answer import CitedAnswer

    by_question = {item.question: item for item in golden_set()}
    item = by_question[question]
    claims = tuple(
        Claim(
            text=c.quoted_span,
            support=[Support(version_id=c.version_id, quoted_span=c.quoted_span)],
        )
        for c in item.citations
    )
    return CitedAnswer(claims=claims, withheld_citations=())


# ---------------------------------------------------------------------------
# Gate 1: lode add → (inline embed) → lode ask → verbatim-grounded cited claim
# ---------------------------------------------------------------------------


def test_gate1_add_ask_yields_cited_claim_with_verbatim_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode add (inline embed) + lode ask → cited claim; quoted_span is verbatim in body.

    The acceptance criterion (lode-6w1.1 / lode-6w1.2): a scripted ``lode add``
    of a known note followed by ``lode ask`` returns a cited claim whose
    ``quoted_span`` occurs verbatim in the cited version's stored body. No
    in-test derive-job stand-in — the real ``add`` command runs embed inline.

    Step by step:
    1. Stub ``lode.embedding.FastEmbedEmbedder`` (before ``add`` runs, since the
       CLI now embeds inline on capture) with ``_ConstantEmbedder`` — deterministic,
       no model download, same dim-768 space for both document and query vectors.
    2. ``lode add`` saves the note, enqueues the derive jobs, and runs chunk+embed
       +FTS inline — no separate worker step, no in-test ``_run_derive_job``.
    3. ``lode ask`` retrieves the indexed note (both legs), sends the context to the
       Q&A step (fake client returning a known claim), the faithfulness gate verifies
       the span, and the surviving cited claim is printed.
    4. We assert the span is present in the output AND verbatim in the body using
       the same ``span_occurs`` predicate the gate uses.
    """
    db_path = tmp_path / "lode.db"

    # Stub the embedder BEFORE ``lode add`` runs — the CLI now embeds inline on
    # capture, so the stub must be in place when add is invoked.  The same stub
    # is used by ``lode ask``'s query-embedding step (FastEmbedEmbedder is patched
    # in lode.embedding, so both the add and ask code paths see it).
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _ConstantEmbedder)

    # Step 1+2: add the note via the real CLI (embed runs inline — no stand-in).
    add_result = runner.invoke(app, ["add", _NOTE_BODY, "--db", str(db_path)])
    assert add_result.exit_code == 0, add_result.output
    note_id = add_result.stdout.strip()
    assert note_id, "lode add should print the note_id"

    # Step 3: get the version_id for the fake client's citation support.
    version_id = _get_head_version_id(db_path, note_id)

    # Step 4: ask via the CLI with the fake Q&A client.
    # The fake client returns a claim whose quoted_span is a verbatim substring of
    # the body — so the faithfulness gate will verify it and let it through.
    fake_client = _FakeClient(
        [
            Claim(
                text="Use exponential backoff.",
                support=[Support(version_id=version_id, quoted_span=_QUOTED_SPAN)],
            )
        ]
    )
    monkeypatch.setattr("lode.qa.build_client", lambda: fake_client)

    ask_result = runner.invoke(app, ["ask", _IN_CORPUS_QUESTION, "--db", str(db_path)])
    assert ask_result.exit_code == 0, ask_result.output

    # Step 5: the claim survived the faithfulness gate and was rendered.
    output = ask_result.stdout
    assert cli._ABSTAIN_LINE not in output, "expected a cited claim, not abstention"
    assert _QUOTED_SPAN in output, f"quoted_span not in ask output: {output!r}"
    assert "version_id" in output, "expected a version_id citation in ask output"

    # The quoted_span is verbatim in the note body (the same check the gate ran).
    assert span_occurs(_QUOTED_SPAN, _NOTE_BODY), (
        f"span {_QUOTED_SPAN!r} is not verbatim in body {_NOTE_BODY!r} — "
        "gate would have abstained"
    )


# ---------------------------------------------------------------------------
# Gate 2: out-of-corpus question → abstain
# ---------------------------------------------------------------------------


def test_gate2_out_of_corpus_question_abstains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-corpus question against the corpus returns honest abstention.

    The acceptance criterion (lode-6w1.1 / lode-6w1.2): an out-of-corpus
    question abstains.  The embedder stub is injected before ``lode add`` so the
    inline embed uses it; the fake Q&A asserts nothing (empty claims) — which is
    the correct behaviour when the sources don't support an answer.  The gate
    finds no surviving claim and the abstention line is printed.
    """
    db_path = tmp_path / "lode.db"

    # Stub the embedder before ``lode add`` — the CLI now embeds inline on capture.
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _ConstantEmbedder)

    # Add and index the note via the real CLI (embed runs inline — no stand-in).
    add_result = runner.invoke(app, ["add", _NOTE_BODY, "--db", str(db_path)])
    assert add_result.exit_code == 0

    # The fake client returns no claims for the out-of-corpus question — the Q&A
    # step can't assert anything grounded, so the gate abstains.
    monkeypatch.setattr("lode.qa.build_client", lambda: _FakeClient([]))

    ask_result = runner.invoke(
        app, ["ask", _OUT_OF_CORPUS_QUESTION, "--db", str(db_path)]
    )
    assert ask_result.exit_code == 0, ask_result.output
    assert cli._ABSTAIN_LINE in ask_result.stdout, (
        f"expected abstention for out-of-corpus question; got: {ask_result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Gate 3: lode eval — deterministic offline scorer reports green metrics
# ---------------------------------------------------------------------------


def test_gate3_eval_scorer_reports_green_on_golden_fixture(tmp_path: Path) -> None:
    """The eval scorer reports recall@k=1, faithfulness=1, abstention=1.

    The acceptance criterion (lode-6w1.1): ``lode eval`` reports recall@k /
    faithfulness / abstention on the fixture without error.

    The scorer is driven here directly (not via the CLI's ``lode eval`` command,
    which needs Anthropic credentials for the real Q&A leg; the credential-gated
    ``nox -s eval`` session covers that path). The three metrics are graded against
    the oracle answerer that returns the known-good citations for every golden item
    — so a correct scorer reports perfect scores.
    """
    settings = load_settings(embedding_vector_dim=_HashEmbedder.DIM)
    embedder = _HashEmbedder()

    conn = init_db(":memory:")
    try:
        score = score_golden_set(
            conn,
            lance_dir=tmp_path / "vectors",
            embedder=embedder,
            answerer=_oracle_answerer,
            settings=settings,
        )
    finally:
        conn.close()

    # Every known-good retrieval target is surfaced by the lexical leg.
    assert score.recall_at_k == pytest.approx(1.0), (
        f"recall@{score.k} = {score.recall_at_k:.3f}; expected 1.0"
    )
    # Every golden answer cites the correct version with a verbatim span.
    assert score.faithfulness_accuracy == pytest.approx(1.0), (
        f"faithfulness = {score.faithfulness_accuracy:.3f}; expected 1.0"
    )
    # In-corpus questions answer; out-of-corpus ones abstain — both correct.
    assert score.abstention_accuracy == pytest.approx(1.0), (
        f"abstention = {score.abstention_accuracy:.3f}; expected 1.0"
    )
    assert score.k == settings.retrieval_top_k
