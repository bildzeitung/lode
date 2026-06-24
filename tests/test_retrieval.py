"""Tests for lode.retrieval — the E4 read side: two search legs, heads only (lode-72m.1).

Covers the acceptance criteria: the lexical (FTS5/BM25) and dense (LanceDB cosine
ANN) legs each return a ranked passage list for a query over the **same passage
unit**, filtered to **live heads only** — so a note's stale prior-head passages
(left as soft history by an update) and a soft-deleted note's content (its
pre-delete version is no longer a head) never surface, even though both indexes
still physically hold those rows.

The corpus is built end-to-end through ``Repository`` + ``CompositeCache`` so both
legs index the same saves the production path would. A tiny deterministic
bag-of-words stub stands in for the embedder (the gate never downloads a model);
the dense vectors it produces share a direction with the matching query vector so
cosine ranking is trivial to reason about.
"""

from pathlib import Path

import pytest

from lode.config import load_settings
from lode.embedding import EmbeddingCacheBackend
from lode.lexical import LexicalCacheBackend, LexicalHit
from lode.repository import CompositeCache, Repository
from lode.retrieval import (
    lexical_search,
    live_head_versions,
    reciprocal_rank_fusion,
    vector_search,
)
from lode.storage import init_db
from lode.vectorstore import VectorHit, VectorStore

# A four-word vocabulary mapped 1:1 onto the (overridden, tiny) vector dimension.
_VOCAB = ("alpha", "beta", "gamma", "delta")
DIM = len(_VOCAB)


class _BagEmbedder:
    """A deterministic, offline stub: a passage embeds to its vocab word counts.

    So "alpha" -> [1, 0, 0, 0] and "alpha alpha" -> [2, 0, 0, 0] — same direction,
    so cosine ranks them identically near the [1, 0, 0, 0] query. Keeps the dense
    leg's ranking trivial to assert without a real model.
    """

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[float(text.split().count(word)) for word in _VOCAB] for text in texts]


def _query_vector(word: str) -> list[float]:
    """The query vector for a single vocab ``word`` (what the embedder would yield)."""
    return [1.0 if word == vocab else 0.0 for vocab in _VOCAB]


@pytest.fixture
def settings():
    return load_settings(embedding_vector_dim=DIM)


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def lance_dir(tmp_path: Path) -> Path:
    return tmp_path / "vectors"


@pytest.fixture
def repo(conn, lance_dir, settings) -> Repository:
    """A repository whose saves drive both index legs, as production does."""
    return Repository(
        conn,
        CompositeCache(
            [
                LexicalCacheBackend(conn, settings=settings),
                EmbeddingCacheBackend(
                    conn,
                    lance_dir=lance_dir,
                    embedder=_BagEmbedder(),
                    settings=settings,
                ),
            ]
        ),
    )


@pytest.fixture
def store(lance_dir, settings) -> VectorStore:
    return VectorStore(lance_dir, settings)


# --- live_head_versions: the allow-list both legs are scoped to ----------------


def test_live_head_versions_are_current_non_deleted_heads(repo, conn) -> None:
    a1 = repo.save("note-a", "alpha").version_id
    b1 = repo.save("note-b", "beta").version_id
    a2 = repo.save("note-a", "alpha alpha", parent=a1).version_id  # supersedes a1
    repo.delete("note-b", parent=b1)  # soft-deletes note-b

    heads = set(live_head_versions(conn))

    # Only note-a's current head — the superseded a1 and the tombstoned note-b out.
    assert heads == {a2}


def test_live_head_versions_empty_on_empty_db(conn) -> None:
    assert live_head_versions(conn) == []


# --- the lexical leg: ranked, same unit, heads only ----------------------------


def test_lexical_search_ranks_and_excludes_stale_prior_head(repo, conn) -> None:
    a1 = repo.save("note-a", "alpha").version_id
    a2 = repo.save("note-a", "delta", parent=a1).version_id  # a1 ("alpha") now stale
    b1 = repo.save("note-b", "alpha").version_id  # a live head matching the query

    hits = lexical_search(conn, "alpha", k=10)

    versions = [h.target_version for h in hits]
    assert b1 in versions  # the live head that matches is returned...
    assert (
        a1 not in versions
    )  # ...but the superseded prior head is not, despite matching
    assert all(v in {a2, b1} for v in versions)  # nothing outside the live-head set


def test_lexical_search_excludes_soft_deleted_note(repo, conn) -> None:
    repo.save("note-keep", "alpha")  # a live head, so heads is non-empty
    g1 = repo.save("note-gone", "gamma").version_id
    repo.delete("note-gone", parent=g1)

    assert lexical_search(conn, "gamma", k=10) == []


def test_lexical_search_caps_at_k(repo, conn) -> None:
    for i in range(5):
        repo.save(f"note-{i}", "alpha")

    assert len(lexical_search(conn, "alpha", k=3)) == 3


def test_lexical_search_empty_db_returns_no_hits(conn) -> None:
    assert lexical_search(conn, "alpha", k=10) == []


# --- the dense leg: ranked, same unit, heads only ------------------------------


def test_vector_search_ranks_and_excludes_stale_prior_head(repo, conn, store) -> None:
    a1 = repo.save("note-a", "alpha").version_id
    a2 = repo.save("note-a", "delta", parent=a1).version_id  # a1's vector now stale
    b1 = repo.save("note-b", "alpha").version_id

    hits = vector_search(store, conn, _query_vector("alpha"), k=10)

    versions = [h.target_version for h in hits]
    assert versions[0] == b1  # the matching live head ranks first (nearest)
    assert a1 not in versions  # the stale prior head's vector is filtered out
    assert all(v in {a2, b1} for v in versions)


def test_vector_search_excludes_soft_deleted_note(repo, conn, store) -> None:
    repo.save("note-keep", "alpha")  # a live head, so heads is non-empty
    g1 = repo.save("note-gone", "gamma").version_id
    repo.delete("note-gone", parent=g1)

    # The ANN leg has no relevance cutoff (it returns the nearest live heads even
    # when far), so the assertion is that the soft-deleted version is *absent* —
    # not that the result is empty.
    hits = vector_search(store, conn, _query_vector("gamma"), k=10)
    assert g1 not in [h.target_version for h in hits]


def test_vector_search_caps_at_k(repo, conn, store) -> None:
    for i in range(5):
        repo.save(f"note-{i}", "alpha")

    assert len(vector_search(store, conn, _query_vector("alpha"), k=3)) == 3


def test_vector_search_empty_db_returns_no_hits(repo, conn, store) -> None:
    # No saves: no live heads, so the leg short-circuits to empty (never queried).
    assert vector_search(store, conn, _query_vector("alpha"), k=10) == []


# --- both legs rank the SAME passage unit --------------------------------------


def test_both_legs_rank_the_same_passage_unit(repo, conn, store) -> None:
    """Acceptance: the two legs return the same passage unit for a query."""
    repo.save("note-a", "alpha")

    lex = lexical_search(conn, "alpha", k=10)
    vec = vector_search(store, conn, _query_vector("alpha"), k=10)

    assert lex and vec
    # Identical passage ids: both legs rank over the one shared passage unit.
    assert {h.passage_id for h in lex} == {h.passage_id for h in vec}


# --- app-side RRF fusion of the two ranked legs (lode-72m.2) --------------------


def _lex(passage_id: str) -> LexicalHit:
    return LexicalHit(
        passage_id=passage_id, target_version=f"v-{passage_id}", score=-1.0
    )


def _vec(passage_id: str) -> VectorHit:
    return VectorHit(
        passage_id=passage_id, target_version=f"v-{passage_id}", distance=0.1
    )


def test_rrf_fuses_both_legs_best_first() -> None:
    """A passage ranked in both legs scores highest; output is score-descending."""
    lexical = [_lex("p1"), _lex("p2")]  # ranks 1, 2
    vector = [_vec("p2"), _vec("p3")]  # ranks 1, 2

    fused = reciprocal_rank_fusion(lexical, vector)

    # p2 is in both legs (lexical rank 2 + dense rank 1 = 1/62 + 1/61); p1 only
    # lexical (rank 1 = 1/61); p3 only dense (rank 2 = 1/62).
    assert [h.passage_id for h in fused] == ["p2", "p1", "p3"]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].score == pytest.approx(1 / 61)  # p1 at rank 1 of the lexical leg
    assert fused[2].score == pytest.approx(1 / 62)  # p3 at rank 2 of the dense leg


def test_rrf_keeps_a_passage_present_in_only_one_leg() -> None:
    """Acceptance: a lexical-only hit (vector not yet landed) still survives fusion."""
    lexical = [_lex("just-saved")]  # FTS5 is synchronous
    vector: list[VectorHit] = []  # the async vector hasn't landed yet

    fused = reciprocal_rank_fusion(lexical, vector)

    assert [h.passage_id for h in fused] == ["just-saved"]
    assert fused[0].target_version == "v-just-saved"  # carried straight from the leg


def test_rrf_default_k_is_60_and_is_tunable() -> None:
    """The smoothing constant defaults to 60 and changes the score when overridden."""
    lexical = [_lex("p1")]

    assert reciprocal_rank_fusion(lexical, [])[0].score == pytest.approx(1 / 61)
    assert reciprocal_rank_fusion(lexical, [], k=9)[0].score == pytest.approx(1 / 10)


def test_rrf_of_two_empty_legs_is_empty() -> None:
    assert reciprocal_rank_fusion([], []) == []
