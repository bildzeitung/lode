"""Tests for the eval scorer (lode-5y8.1).

Acceptance: a scorer computes retrieval **recall@k**, citation/**faithfulness**
accuracy, and **abstention** correctness against the golden set, and is
**deterministic for a fixed corpus**.

Everything here runs offline and reproducibly. The two non-deterministic pieces
of the real pipeline are pinned via the scorer's injection seams: a deterministic
hashing **embedder** (no model download) builds the corpus + query vectors, and a
deterministic **answerer** stands in for the Q&A LLM call. Recall@k leans on the
model-free FTS5 lexical leg, so it scores the real seed prose correctly even with
a stub embedder; the faithfulness/abstention legs are graded against the injected
answerer. One answerer wraps the *real* :func:`lode.cited_answer.ask` (with a mock
Anthropic client) to prove the scorer drives the landed gate end to end.
"""

import hashlib
from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from lode.answer import Claim, Support
from lode.cited_answer import CitedAnswer, ask
from lode.config import load_settings
from lode.eval.golden import golden_set
from lode.eval.harness import GoldenScore, score_golden_set
from lode.retrieval import ContextItem
from lode.storage import init_db

# A tiny vector dim keeps the stub embedder cheap; recall is carried by the
# model-free FTS5 leg, so the dense leg only needs to be deterministic.
DIM = 8


class _HashEmbedder:
    """A deterministic, offline stub: each text hashes to a fixed-dim unit-ish vector.

    No model, no network -- the same text always yields the same vector, so the
    dense leg (and therefore the whole scorer) is reproducible. The values carry no
    semantic meaning; the lexical leg does the real retrieval work.
    """

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([digest[i] / 255.0 for i in range(DIM)])
        return vectors


@pytest.fixture
def settings():
    return load_settings(embedding_vector_dim=DIM)


@pytest.fixture
def conn():
    connection = init_db(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def _golden_answerer(question: str, context: Sequence[ContextItem]) -> CitedAnswer:
    """An oracle: answer each in-corpus question with its known-good citations.

    For an answerable golden item it returns a surviving claim per golden citation
    (cited to the right version, verbatim span); for an out-of-corpus item it
    abstains (no claims). So a correct scorer reports perfect scores -- the fixture
    that validates the metric plumbing and the all-green baseline.
    """
    by_question = {item.question: item for item in golden_set()}
    item = by_question[question]
    claims = tuple(
        Claim(
            text=citation.quoted_span,
            support=[
                Support(
                    version_id=citation.version_id, quoted_span=citation.quoted_span
                )
            ],
        )
        for citation in item.citations
    )
    return CitedAnswer(claims=claims, withheld_citations=())


def _always_abstain(question: str, context: Sequence[ContextItem]) -> CitedAnswer:
    """A degenerate answerer that never answers -- abstains on everything."""
    return CitedAnswer(claims=(), withheld_citations=())


def _score(conn, settings, answerer, **kwargs) -> GoldenScore:
    return score_golden_set(
        conn,
        lance_dir=kwargs["lance_dir"],
        embedder=_HashEmbedder(),
        answerer=answerer,
        settings=settings,
    )


# --- recall@k over the real seed corpus ----------------------------------------


def test_oracle_run_scores_perfect_on_all_three_metrics(conn, settings, tmp_path):
    """With a correct answerer, the scorer reports perfect recall, faithfulness, abstention."""
    score = _score(conn, settings, _golden_answerer, lance_dir=tmp_path / "vec")

    # Every answerable question's known-good notes are surfaced by the lexical leg.
    assert score.recall_at_k == pytest.approx(1.0)
    # Every answer cites exactly the known-good versions, all gate-verifiable.
    assert score.faithfulness_accuracy == pytest.approx(1.0)
    # In-corpus questions answer; out-of-corpus ones abstain.
    assert score.abstention_accuracy == pytest.approx(1.0)
    assert score.k == settings.retrieval_top_k


def test_recall_is_model_free_and_finds_known_good_notes(conn, settings, tmp_path):
    """recall@k surfaces the golden target version(s) per answerable item."""
    score = _score(conn, settings, _golden_answerer, lance_dir=tmp_path / "vec")

    by_q = {it.question: it for it in score.items}
    for item in golden_set():
        if item.abstain:
            continue
        scored = by_q[item.question]
        # The known-good versions are all present in the retrieved set.
        assert item.relevant_version_ids <= set(scored.retrieved), item.question
        assert scored.recall == pytest.approx(1.0)


# --- determinism: a fixed corpus yields a fixed score --------------------------


def test_score_is_deterministic_for_a_fixed_corpus(settings, tmp_path):
    """Two independent runs over the same corpus + seams produce identical scores."""
    first_conn = init_db(":memory:")
    second_conn = init_db(":memory:")
    try:
        first = score_golden_set(
            first_conn,
            lance_dir=tmp_path / "v1",
            embedder=_HashEmbedder(),
            answerer=_golden_answerer,
            settings=settings,
        )
        second = score_golden_set(
            second_conn,
            lance_dir=tmp_path / "v2",
            embedder=_HashEmbedder(),
            answerer=_golden_answerer,
            settings=settings,
        )
    finally:
        first_conn.close()
        second_conn.close()

    assert first == second


# --- the metrics actually move when the system is wrong ------------------------


def test_always_abstain_tanks_faithfulness_and_partial_abstention(
    conn, settings, tmp_path
):
    """A never-answering system: faithfulness 0, abstention only the abstain items right."""
    score = _score(conn, settings, _always_abstain, lance_dir=tmp_path / "vec")

    items = golden_set()
    abstain_n = sum(1 for it in items if it.abstain)

    # Retrieval is unaffected by the answerer -- recall still perfect.
    assert score.recall_at_k == pytest.approx(1.0)
    # No answer ever cites anything, so no answerable item is faithful.
    assert score.faithfulness_accuracy == pytest.approx(0.0)
    # Only the out-of-corpus items have the correct (abstain) decision.
    assert score.abstention_accuracy == pytest.approx(abstain_n / len(items))


def test_off_target_citation_is_not_faithful(conn, settings, tmp_path):
    """An answer citing a real-but-wrong note scores 0 faithfulness for that item."""
    # Pick one answerable item and cite a note from a *disjoint* item (a real but
    # off-target version), so cited </= relevant.
    items = [it for it in golden_set() if not it.abstain]
    target = items[0]
    other = next(
        it
        for it in items
        if it.relevant_version_ids.isdisjoint(target.relevant_version_ids)
    )
    wrong_version = next(iter(other.relevant_version_ids))
    wrong_span = other.citations[0].quoted_span

    def answerer(question: str, context: Sequence[ContextItem]) -> CitedAnswer:
        if question == target.question:
            return CitedAnswer(
                claims=(
                    Claim(
                        text=wrong_span,
                        support=[
                            Support(version_id=wrong_version, quoted_span=wrong_span)
                        ],
                    ),
                ),
                withheld_citations=(),
            )
        return _golden_answerer(question, context)

    score = _score(conn, settings, answerer, lance_dir=tmp_path / "vec")
    scored = next(it for it in score.items if it.question == target.question)

    assert scored.faithful is False
    # It did answer (so abstention is "correct" for an in-corpus item) ...
    assert scored.abstention_correct is True
    # ... but the off-target citation drops faithfulness below perfect overall.
    assert score.faithfulness_accuracy < 1.0


# --- the scorer drives the REAL gate end to end (mock LLM client) --------------


class _FakeMessages:
    def __init__(self, claims_for):
        self._claims_for = claims_for

    def parse(self, **kwargs) -> SimpleNamespace:
        prompt = kwargs["messages"][0]["content"]
        return SimpleNamespace(
            parsed_output=SimpleNamespace(claims=self._claims_for(prompt))
        )


class _FakeClient:
    """Mock anthropic client: returns golden claims keyed off the question in the prompt."""

    def __init__(self, claims_for):
        self.messages = _FakeMessages(claims_for)


def test_scorer_drives_the_real_cited_answer_gate(conn, settings, tmp_path):
    """An answerer wrapping the real ask(): the landed faithfulness gate runs in-loop."""
    by_question = {item.question: item for item in golden_set()}

    def claims_for(prompt: str) -> list[Claim]:
        # The question is embedded verbatim in the user prompt (qa._request_claims).
        for question, item in by_question.items():
            if question in prompt and not item.abstain:
                return [
                    Claim(
                        text=c.quoted_span,
                        support=[
                            Support(version_id=c.version_id, quoted_span=c.quoted_span)
                        ],
                    )
                    for c in item.citations
                ]
        return []

    client = _FakeClient(claims_for)

    def answerer(question: str, context: Sequence[ContextItem]) -> CitedAnswer:
        return ask(conn, question, context, client=client, settings=settings)

    score = score_golden_set(
        conn,
        lance_dir=tmp_path / "vec",
        embedder=_HashEmbedder(),
        answerer=answerer,
        settings=settings,
    )

    # The real gate verified every surviving span against stored bodies and
    # abstained on the out-of-corpus questions: a clean end-to-end pass.
    assert score.recall_at_k == pytest.approx(1.0)
    assert score.faithfulness_accuracy == pytest.approx(1.0)
    assert score.abstention_accuracy == pytest.approx(1.0)
