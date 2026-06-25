"""The eval scorer: run the golden set through the pipeline, score it (lode-5y8.1).

``docs/design.md`` §7 makes a small held-out Q&A set a first-class step-1
deliverable, scored on three things -- **retrieval recall@k**, **citation /
faithfulness accuracy**, and **abstention correctness**. The golden set
(``lode.eval.golden``) and its deterministic seed corpus (``lode.eval.seed``) are
the *data*; this module is the **scorer** that loads that data into a fresh store,
drives the landed retrieval + cited-Q&A pipeline over it, and measures the three
metrics. It is the regression surface every tune knob (rerank, the entailment
threshold, chunk size) is measured against, so it must be **reproducible for a
fixed corpus**.

It reuses the landed pieces verbatim and reimplements none of them: the corpus is
built through :class:`lode.repository.Repository` + :class:`~lode.repository.CompositeCache`
(the same save path production drives, so both index legs see the same saves);
retrieval is :func:`lode.retrieval.lexical_search` / :func:`~lode.retrieval.vector_search`
-> :func:`~lode.retrieval.reciprocal_rank_fusion` -> :func:`~lode.retrieval.expand_parents`
-> :func:`~lode.retrieval.trust_rank`; the cited-answer loop is the injected
:data:`Answerer` (in production :func:`lode.cited_answer.ask`).

**Determinism (the load-bearing property).** The scorer is parameterised by two
seams so a fixed corpus yields a fixed score:

* the **embedder** (:class:`lode.embedding.Embedder`) -- builds the corpus vectors
  and embeds each query for the dense leg. Local embeddings are deterministic, so
  the whole retrieval leg (FTS5 BM25 + cosine ANN) is deterministic over the fixed
  seed corpus -- which makes **recall@k corpus-deterministic and model-free in the
  lexical leg** (no network needed to score retrieval).
* the **answerer** (:data:`Answerer`) -- sources the cited answer for the
  LLM-dependent legs (faithfulness + abstention). The Q&A LLM call is *not*
  deterministic, so rather than hit the network the scorer injects this seam (the
  same mock seam :func:`lode.cited_answer.ask` already exposes via its ``client``
  parameter): a fixed answerer over a fixed corpus yields a fixed score. Tests
  inject deterministic stubs; production wires ``lambda q, ctx: ask(conn, q, ctx,
  client=..., settings=...)``. (See ``docs/decisions.md``, the eval-harness entry.)

The three scores are each a fraction in ``[0, 1]``:

* **recall@k** -- mean over answerable items of ``|relevant & retrieved@k| /
  |relevant|``: did retrieval surface the known-good target version(s) among the
  top ``k`` passages' notes?
* **faithfulness accuracy** -- mean over answerable items of an indicator: the
  answer produced at least one surviving (gate-verified) claim **and** every
  surviving citation targets a known-good version. The gate already guarantees
  every survivor's span is verbatim-present; this additionally checks the answer
  cited the *right* notes (a wrong abstention or an off-target citation scores 0).
* **abstention correctness** -- mean over *all* items of an indicator: the answer
  abstained iff the item is out-of-corpus. In-corpus questions must answer;
  out-of-corpus ones must abstain.
"""

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from lode.cited_answer import CitedAnswer
from lode.config import Settings
from lode.embedding import Embedder, EmbeddingCacheBackend
from lode.eval.golden import GoldenItem, golden_set
from lode.eval.seed import seed_notes
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.retrieval import (
    ContextItem,
    expand_parents,
    lexical_search,
    reciprocal_rank_fusion,
    trust_rank,
    vector_search,
)
from lode.vectorstore import VectorStore

#: The cited-answer source the scorer measures: maps a question + its trust-ranked
#: context to a :class:`lode.cited_answer.CitedAnswer`. This is the injected seam
#: for the LLM-dependent legs (faithfulness + abstention) -- production wraps
#: :func:`lode.cited_answer.ask` with a real client; tests pass a deterministic
#: stub so a fixed corpus yields a fixed score and no network is hit.
Answerer = Callable[[str, Sequence[ContextItem]], CitedAnswer]

_WORD = re.compile(r"[0-9a-z]+")


@dataclass(frozen=True)
class ItemScore:
    """The per-item scoring breakdown behind the aggregate scores.

    ``recall`` and ``faithful`` are ``None`` for an out-of-corpus (abstain) item --
    it has no relevant version to retrieve and no known-good citation to match; its
    only graded leg is ``abstention_correct``.
    """

    question: str
    abstain_expected: bool
    retrieved: tuple[str, ...]
    abstained: bool
    recall: float | None
    faithful: bool | None
    abstention_correct: bool


@dataclass(frozen=True)
class GoldenScore:
    """The three aggregate eval scores plus the per-item breakdown.

    Each aggregate is a fraction in ``[0, 1]`` (see the module docstring for the
    exact definition). ``k`` is the retrieval cutoff the recall metric used.
    ``items`` is the per-item detail, in golden-set order, so a regression can be
    traced to the question that moved.
    """

    k: int
    recall_at_k: float
    faithfulness_accuracy: float
    abstention_accuracy: float
    items: tuple[ItemScore, ...]


def _fts_query(question: str) -> str | None:
    """Build an FTS5 ``MATCH`` expression from a natural-language ``question``.

    No query-builder has landed on the read side yet (``lode.retrieval`` takes the
    MATCH expression as given), so the scorer -- the first caller to retrieve from a
    free-text question -- builds one: the question's alphanumeric word tokens
    ``OR``-ed together. ``OR`` (not the FTS5 default ``AND``) keeps recall honest --
    a passage sharing any salient keyword is a candidate, and BM25 ranks them -- and
    stripping to ``[0-9a-z]+`` tokens makes the expression safe (question marks,
    apostrophes and quotes never reach the parser). ``None`` when the question has
    no usable token, so the caller skips the lexical leg rather than issue an empty
    MATCH.
    """
    tokens = _WORD.findall(question.lower())
    if not tokens:
        return None
    return " OR ".join(tokens)


def _build_seed_store(
    conn: sqlite3.Connection,
    *,
    lance_dir: str | Path,
    embedder: Embedder,
    settings: Settings,
) -> None:
    """Load the deterministic seed corpus into a fresh store, both index legs live.

    Saves every :func:`lode.eval.seed.seed_notes` note through a
    :class:`~lode.repository.Repository` whose cache fans out to the FTS5 lexical
    leg and the LanceDB embed leg -- the same save path production drives, so the
    head ``version_id`` each save produces is exactly the id the golden set cites
    (both derive it from :func:`lode.hashing.content_version_id`). ``conn`` must be
    an empty initialised store.
    """
    repo = Repository(
        conn,
        CompositeCache(
            [
                LexicalCacheBackend(conn, settings=settings),
                EmbeddingCacheBackend(
                    conn,
                    lance_dir=lance_dir,
                    embedder=embedder,
                    settings=settings,
                ),
            ]
        ),
    )
    for note in seed_notes():
        repo.save(note.note_id, note.body, settings=settings)


def _retrieve(
    conn: sqlite3.Connection,
    store: VectorStore,
    question: str,
    *,
    embedder: Embedder,
    settings: Settings,
    k: int,
) -> list[ContextItem]:
    """Run the full read pipeline for ``question`` and return its trust-ranked context.

    The landed read side end to end: the lexical (FTS5/BM25) and dense (cosine ANN,
    query embedded by ``embedder``) legs each capped at ``k``, fused app-side
    (:func:`~lode.retrieval.reciprocal_rank_fusion`), the top ``k`` fused passages
    expanded small-to-big (:func:`~lode.retrieval.expand_parents`) and ordered by
    the trust gradient (:func:`~lode.retrieval.trust_rank`). Deterministic for a
    fixed corpus + embedder.

    The query is embedded through the same ``embedder.embed_passages`` seam the
    documents use. With the production model (nomic-embed-text-v1.5) that applies
    the ``search_document:`` prefix to the query too, where the model expects
    ``search_query:`` for the asymmetric pair -- a dense-leg bias tracked in
    lode-7yw (the ``Embedder`` seam exposes no query side yet). Recall@k stays
    sound because the model-free lexical leg carries it; the bias only softens the
    dense leg's contribution.
    """
    match = _fts_query(question)
    lexical = lexical_search(conn, match, k=k) if match else []
    query_vector = embedder.embed_passages([question])[0]
    vector = vector_search(store, conn, query_vector, k=k)
    fused = reciprocal_rank_fusion(lexical, vector, k=settings.rrf_k)[:k]
    expanded = expand_parents(conn, fused)
    return trust_rank(conn, expanded).context


def _retrieved_versions(context: Sequence[ContextItem]) -> tuple[str, ...]:
    """The distinct cited target versions in ``context``, best-first order kept."""
    seen: dict[str, None] = {}
    for item in context:
        seen.setdefault(item.target_version, None)
    return tuple(seen)


def _score_item(
    conn: sqlite3.Connection,
    store: VectorStore,
    item: GoldenItem,
    *,
    embedder: Embedder,
    answerer: Answerer,
    settings: Settings,
    k: int,
) -> ItemScore:
    """Retrieve, answer, and grade one golden item on the three metrics."""
    context = _retrieve(
        conn, store, item.question, embedder=embedder, settings=settings, k=k
    )
    retrieved = _retrieved_versions(context)
    answer = answerer(item.question, context)

    if item.abstain:
        return ItemScore(
            question=item.question,
            abstain_expected=True,
            retrieved=retrieved,
            abstained=answer.abstained,
            recall=None,
            faithful=None,
            abstention_correct=answer.abstained,
        )

    relevant = item.relevant_version_ids
    found = relevant & set(retrieved)
    recall = len(found) / len(relevant)

    cited = {support.target_id for claim in answer.claims for support in claim.support}
    faithful = bool(cited) and cited <= relevant

    return ItemScore(
        question=item.question,
        abstain_expected=False,
        retrieved=retrieved,
        abstained=answer.abstained,
        recall=recall,
        faithful=faithful,
        abstention_correct=not answer.abstained,
    )


def _mean(values: Sequence[float]) -> float:
    """Mean of ``values``; ``0.0`` for an empty sequence (no items to score)."""
    return sum(values) / len(values) if values else 0.0


def score_golden_set(
    conn: sqlite3.Connection,
    *,
    lance_dir: str | Path,
    embedder: Embedder,
    answerer: Answerer,
    settings: Settings | None = None,
    k: int | None = None,
) -> GoldenScore:
    """Score the golden Q&A set end to end: recall@k, faithfulness, abstention.

    Builds the deterministic seed corpus into the fresh store ``conn`` (+ the
    LanceDB vectors under ``lance_dir``), then runs every :func:`lode.eval.golden`
    item through the retrieval + cited-answer pipeline and grades the three
    metrics. ``conn`` must be an empty initialised store; ``embedder`` and
    ``answerer`` are the injected seams that keep the result reproducible for a
    fixed corpus (see the module docstring). ``k`` defaults to
    ``settings.retrieval_top_k``.

    Deterministic by construction: identical ``conn`` state, ``embedder`` and
    ``answerer`` yield identical :class:`GoldenScore` on every run.
    """
    settings = settings or Settings()
    k = k if k is not None else settings.retrieval_top_k

    _build_seed_store(conn, lance_dir=lance_dir, embedder=embedder, settings=settings)
    store = VectorStore(lance_dir, settings)

    items = tuple(
        _score_item(
            conn,
            store,
            item,
            embedder=embedder,
            answerer=answerer,
            settings=settings,
            k=k,
        )
        for item in golden_set()
    )

    answerable = [it for it in items if not it.abstain_expected]
    return GoldenScore(
        k=k,
        recall_at_k=_mean([it.recall for it in answerable]),
        faithfulness_accuracy=_mean([float(it.faithful) for it in answerable]),
        abstention_accuracy=_mean([float(it.abstention_correct) for it in items]),
        items=items,
    )
