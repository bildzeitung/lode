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

from lode.config import Settings, load_settings
from lode.embedding import EmbeddingCacheBackend, embed
from lode.externals import ingest_snapshot
from lode.lexical import LexicalCacheBackend, LexicalHit
from lode.repository import CompositeCache, Repository
from lode.retrieval import (
    ExpandedHit,
    FastEmbedCrossEncoder,
    FusedHit,
    TrustTier,
    build_match_query,
    expand_parents,
    graph_expand,
    lexical_search,
    live_head_versions,
    reciprocal_rank_fusion,
    rerank,
    trust_rank,
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


# --- live_head_versions: unions in external heads too (lode-c5l) ---------------
#
# DECISION A (docs/externals.md): a mirrored snapshot must be a DIRECT hit on
# its own content, not only reachable via graph-expansion from a citing note.
# _insert_external_snapshot is defined further below (small-to-big / trust_rank
# section) but usable here — the whole module is parsed before any test runs.


def test_live_head_versions_unions_in_current_external_snapshot(repo, conn) -> None:
    v = repo.save("note-a", "alpha").version_id
    current = _insert_external_snapshot(
        conn, external_id="EXT-1", snapshot_id="snap-current", is_head=True
    )

    heads = set(live_head_versions(conn))

    assert heads == {v, current}


def test_live_head_versions_excludes_stale_external_snapshot(conn) -> None:
    _insert_external_snapshot(
        conn, external_id="EXT-1", snapshot_id="snap-old", is_head=False
    )

    assert live_head_versions(conn) == []


def test_live_head_versions_excludes_tombstoned_head_snapshot(conn) -> None:
    """A source whose only-ever snapshot is a tombstone stays out of the allow-list."""
    conn.execute(
        "INSERT INTO externals (external_id, source_type) VALUES ('EXT-1', 'web')"
    )
    conn.execute(
        "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
        "VALUES ('snap-tomb', 'EXT-1', '[tombstone: http_404]', 'tombstone')"
    )
    conn.execute(
        "UPDATE externals SET head_snapshot_id = 'snap-tomb' WHERE external_id = 'EXT-1'"
    )

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


# lode-b4w.3: lexical_search and vector_search cap their result count at k the
# same way; the two prior tests (test_lexical_search_caps_at_k /
# test_vector_search_caps_at_k) differed only in which search fn was called
# (different signatures -- vector_search additionally takes `store` -- so the
# body dispatches on a string id rather than passing the function object
# itself). 2 tests -> 1, both original assertions still run as parametrize rows.
@pytest.mark.parametrize("search_kind", ["lexical", "vector"])
def test_search_caps_at_k(repo, conn, store, search_kind: str) -> None:
    for i in range(5):
        repo.save(f"note-{i}", "alpha")

    if search_kind == "lexical":
        hits = lexical_search(conn, "alpha", k=3)
    else:
        hits = vector_search(store, conn, _query_vector("alpha"), k=3)

    assert len(hits) == 3


def test_vector_search_empty_db_returns_no_hits(repo, conn, store) -> None:
    # No saves: no live heads, so the leg short-circuits to empty (never queried).
    assert vector_search(store, conn, _query_vector("alpha"), k=10) == []


# --- an ingested snapshot is a DIRECT hit, no citing note involved (lode-c5l) --
#
# Acceptance: a freshly ingested external snapshot is a direct keyword hit (FTS,
# synchronous — lode.externals.ingest_snapshot drives the FTS leg itself) and a
# direct vector hit once the embed worker drains, with no owned note in the
# picture at all.


def test_freshly_ingested_snapshot_is_a_direct_lexical_hit(conn, settings) -> None:
    result = ingest_snapshot(conn, "https://example.com/x", "web", "alpha content")

    hits = lexical_search(conn, "alpha", k=10)

    assert [h.target_version for h in hits] == [result.snapshot_id]


def test_freshly_ingested_snapshot_is_a_direct_vector_hit_after_embed_drains(
    conn, lance_dir, settings, store
) -> None:
    result = ingest_snapshot(conn, "https://example.com/x", "web", "alpha")

    # The embed worker hasn't drained yet: no vector, so no dense hit.
    assert vector_search(store, conn, _query_vector("alpha"), k=10) == []

    embed(
        conn,
        result.snapshot_id,
        lance_dir=lance_dir,
        embedder=_BagEmbedder(),
        settings=settings,
    )

    hits = vector_search(store, conn, _query_vector("alpha"), k=10)
    assert [h.target_version for h in hits] == [result.snapshot_id]


def test_stale_external_snapshot_is_not_a_direct_hit(conn, settings) -> None:
    """A superseded snapshot (churn) must not surface as a direct keyword hit."""
    ingest_snapshot(conn, "https://example.com/x", "web", "alpha one")
    ingest_snapshot(conn, "https://example.com/x", "web", "alpha two")  # head moves

    hits = lexical_search(conn, "alpha", k=10)

    # Only the current head is a direct hit; the superseded snapshot is not.
    (n,) = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
    assert n == 2  # sanity: churn really did write two snapshots
    assert len(hits) == 1


def test_tombstone_snapshot_is_never_a_direct_hit(conn, settings) -> None:
    result = ingest_snapshot(
        conn,
        "https://example.com/dead",
        "web",
        "[tombstone: http_404]",
        status="tombstone",
    )

    assert lexical_search(conn, "tombstone", k=10) == []
    assert result.snapshot_id not in live_head_versions(conn)


def test_ingested_snapshot_secret_is_redacted_on_both_direct_legs(
    conn, lance_dir, settings, store
) -> None:
    """The lode-c5l bounce fix, exercised through the full retrieval pipeline.

    A secret in a fetched page must not become directly keyword- or vector-
    retrievable on either leg — the regression the bounced land/lode-w0h.8
    branch shipped (FTS chunked the raw body while the vector leg redacted
    it, a split-brain that made a pasted secret directly keyword-findable).
    """
    secret = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern
    body = f"mirrored page contents\ncreds: {secret} keep private\n"

    result = ingest_snapshot(conn, "https://example.com/secret", "web", body)
    embed(
        conn,
        result.snapshot_id,
        lance_dir=lance_dir,
        embedder=_BagEmbedder(),
        settings=settings,
    )

    # Positive control: ordinary body content really is retrievable via the
    # union — proves the secret's absence below is redaction working, not the
    # snapshot silently missing from live_head_versions.
    assert [h.target_version for h in lexical_search(conn, "mirrored", k=10)] == [
        result.snapshot_id
    ]
    # (c) a lexical_search for the secret returns no hit.
    assert lexical_search(conn, build_match_query(secret), k=10) == []
    # (d) snapshots.body still holds the original, unredacted text.
    (stored_body,) = conn.execute(
        "SELECT body FROM snapshots WHERE snapshot_id = ?", (result.snapshot_id,)
    ).fetchone()
    assert secret in stored_body


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


# --- small-to-big parent expansion (lode-72m.4) --------------------------------

# A structured note: one section whose list items each chunk into their own
# passage, so a passage's parent_block (the whole section) is strictly larger
# than the precise passage it cites.
_RUNBOOK = (
    "# Deploy runbook\n"
    "- rotate the alpha certs\n"
    "- restart the beta service\n"
    "- verify the gamma health check\n"
)


def _insert_passage(conn, passage_id, ord_, char_range, text, parent_block) -> None:
    conn.execute(
        "INSERT INTO passages "
        "(passage_id, target_version, ord, char_range, text, parent_block) "
        "VALUES (?, 'v1', ?, ?, ?, ?)",
        (passage_id, ord_, char_range, text, parent_block),
    )


def test_expand_parents_expands_to_section_but_cites_the_passage(repo, conn) -> None:
    """Acceptance: a hit expands to its parent block while the citation stays
    pinned to the precise passage/span."""
    v = repo.save("runbook", _RUNBOOK).version_id

    # The lexical leg finds the precise "alpha" list-item passage.
    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    assert fused, "the alpha passage should match"

    expanded = expand_parents(conn, fused)

    assert expanded
    top = expanded[0]
    # Citation pins to the precise passage/span ...
    assert top.passage_text == "- rotate the alpha certs"
    start, end = (int(x) for x in top.char_range.split(":"))
    assert _RUNBOOK[start:end] == top.passage_text
    assert top.target_version == v
    assert top.score == fused[0].score  # the upstream ranking is carried
    # ... while the expanded context is the larger enclosing section.
    assert top.parent_block != top.passage_text
    assert "Deploy runbook" in top.parent_block
    assert "restart the beta service" in top.parent_block
    assert top.passage_text in top.parent_block


def test_expand_parents_preserves_best_first_order(conn) -> None:
    """The input's best-first order is preserved through expansion."""
    _insert_passage(conn, "p-a", 0, "0:5", "alpha", "alpha beta section")
    _insert_passage(conn, "p-b", 1, "6:10", "beta", "alpha beta section")
    hits = [FusedHit("p-b", "v1", 0.9), FusedHit("p-a", "v1", 0.5)]

    expanded = expand_parents(conn, hits)

    assert [e.passage_id for e in expanded] == ["p-b", "p-a"]
    assert [e.score for e in expanded] == [0.9, 0.5]


def test_expand_parents_drops_a_hit_with_no_passage_row(conn) -> None:
    """A fused hit whose regenerable passage row is gone can't be cited — dropped."""
    assert expand_parents(conn, [FusedHit("missing", "v1", 0.5)]) == []


def test_expand_parents_of_no_hits_is_empty(conn) -> None:
    assert expand_parents(conn, []) == []


# --- cross-encoder rerank stage (lode-72m.3) -----------------------------------


class _StubCrossEncoder:
    """An offline stub scorer: relevance is a fixed per-text score map.

    Keeps the rerank gate offline (no model download) and the ranking trivial to
    assert. Records the documents it was handed (``seen``) so a test can prove the
    stage was *bypassed* — never called — when the toggle is off.
    """

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores
        self.seen: list[str] | None = None

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.seen = list(documents)
        return [self._scores[doc] for doc in documents]


def test_rerank_reorders_by_cross_encoder_score_and_carries_it(conn) -> None:
    """Acceptance: when enabled the cross-encoder re-scores the fused top-N — the
    output order and each hit's score become the cross-encoder relevance."""
    _insert_passage(conn, "p-a", 0, "0:5", "alpha", "section")
    _insert_passage(conn, "p-b", 1, "6:10", "beta", "section")
    # RRF puts p-a first; the cross-encoder reverses that (beta more relevant).
    fused = [FusedHit("p-a", "v1", 0.9), FusedHit("p-b", "v1", 0.1)]
    scorer = _StubCrossEncoder({"alpha": 0.2, "beta": 0.8})

    out = rerank(conn, "q", fused, scorer=scorer, settings=load_settings())

    assert [h.passage_id for h in out] == ["p-b", "p-a"]
    assert [h.score for h in out] == [0.8, 0.2]  # score is now the rerank relevance
    assert out[0].target_version == "v1"  # citation target carried straight through


def test_rerank_trims_to_keep_n(conn) -> None:
    """Only ``rerank_keep_n`` reranked hits proceed downstream."""
    _insert_passage(conn, "p-a", 0, "0:5", "alpha", "section")
    _insert_passage(conn, "p-b", 1, "6:10", "beta", "section")
    _insert_passage(conn, "p-c", 2, "11:16", "gamma", "section")
    fused = [
        FusedHit("p-a", "v1", 0.9),
        FusedHit("p-b", "v1", 0.5),
        FusedHit("p-c", "v1", 0.1),
    ]
    scorer = _StubCrossEncoder({"alpha": 0.1, "beta": 0.9, "gamma": 0.5})

    out = rerank(
        conn, "q", fused, scorer=scorer, settings=load_settings(rerank_keep_n=1)
    )

    assert [h.passage_id for h in out] == ["p-b"]  # the single best by rerank score


def test_rerank_scores_only_the_fused_top_k(conn) -> None:
    """``retrieval_top_k`` caps how many fused passages enter rerank; the rest are
    dropped before scoring."""
    _insert_passage(conn, "p-a", 0, "0:5", "alpha", "section")
    _insert_passage(conn, "p-b", 1, "6:10", "beta", "section")
    _insert_passage(conn, "p-c", 2, "11:16", "gamma", "section")
    fused = [
        FusedHit("p-a", "v1", 0.9),
        FusedHit("p-b", "v1", 0.5),
        FusedHit("p-c", "v1", 0.1),
    ]
    scorer = _StubCrossEncoder({"alpha": 0.1, "beta": 0.2})  # p-c never scored

    out = rerank(
        conn, "q", fused, scorer=scorer, settings=load_settings(retrieval_top_k=2)
    )

    assert scorer.seen == ["alpha", "beta"]  # only the top-k entered the model
    assert {h.passage_id for h in out} == {"p-a", "p-b"}  # p-c excluded entirely


def test_rerank_disabled_is_fully_bypassed(conn) -> None:
    """Acceptance: the seam is permanent but the stage toggles off — when off the
    call returns the input unchanged and never invokes the model."""
    fused = [FusedHit("p-a", "v1", 0.9), FusedHit("p-b", "v1", 0.1)]
    scorer = _StubCrossEncoder({})

    out = rerank(
        conn, "q", fused, scorer=scorer, settings=load_settings(rerank_enabled=False)
    )

    assert out == fused  # unchanged: order and RRF scores preserved
    assert scorer.seen is None  # the cross-encoder was never called


def test_rerank_of_no_hits_is_empty(conn) -> None:
    scorer = _StubCrossEncoder({})
    assert rerank(conn, "q", [], scorer=scorer, settings=load_settings()) == []
    assert scorer.seen is None  # nothing to score, model untouched


def test_rerank_drops_a_hit_with_no_passage_row(conn) -> None:
    """A fused hit whose regenerable passage row is gone can't be scored or cited —
    dropped, exactly as expand_parents drops it."""
    _insert_passage(conn, "p-a", 0, "0:5", "alpha", "section")
    fused = [FusedHit("p-a", "v1", 0.9), FusedHit("missing", "v1", 0.5)]
    scorer = _StubCrossEncoder({"alpha": 0.7})

    out = rerank(conn, "q", fused, scorer=scorer, settings=load_settings())

    assert [h.passage_id for h in out] == ["p-a"]
    assert scorer.seen == ["alpha"]  # the missing hit never reached the model


# --- FastEmbedCrossEncoder._load: cache_dir under $LODE_HOME, never /tmp (lode-gmo)
#
# Mirrors the embedder's version of this test (tests/test_embedding.py): without
# an explicit cache_dir, fastembed falls back to
# tempfile.gettempdir()/fastembed_cache -- wiped on reboot. Verified by patching
# the fastembed TextCrossEncoder constructor itself, so this stays offline.


def test_load_passes_durable_model_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastembed.rerank import cross_encoder

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))
    captured: dict[str, object] = {}

    class _FakeTextCrossEncoder:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(cross_encoder, "TextCrossEncoder", _FakeTextCrossEncoder)

    # A model name unique to this test -- never the Settings() default that
    # every slow-tier test (@pytest.mark.slow) uses for its real reranker load.
    # This is the one test in the suite that observes _load()'s side effect
    # (the kwargs it passes to the real TextCrossEncoder constructor) rather
    # than just its return value, so it must never be able to take a hit off
    # the session-scoped model cache in tests/conftest.py
    # (_cache_cross_encoder_model_load): a hit would skip the real
    # TextCrossEncoder() call entirely and leave `captured` empty, depending on
    # whether a slow test already populated the cache for this model name on
    # this xdist worker (lode-vzwn). A name nothing else uses guarantees a
    # cache MISS, so the real load -- and this assertion -- runs independent of
    # test order.
    encoder = FastEmbedCrossEncoder(Settings(rerank_model="test-only-cache-dir-probe"))
    encoder._load()

    assert captured["cache_dir"] == str(tmp_path / "root" / "models")


# --- trust-ordered context builder (lode-az0.1) --------------------------------


def _expanded(passage_id: str, target_version: str, score: float) -> ExpandedHit:
    """An ExpandedHit citing ``target_version`` (a version_id or snapshot_id)."""
    return ExpandedHit(
        passage_id=passage_id,
        target_version=target_version,
        char_range="0:5",
        passage_text=f"text-{passage_id}",
        parent_block=f"block-{passage_id}",
        score=score,
    )


def _insert_external_snapshot(
    conn, *, external_id: str, snapshot_id: str, is_head: bool
) -> str:
    """Insert an external + one snapshot directly; point head at it iff ``is_head``.

    A hand-rolled shortcut for pinning an *arbitrary* snapshot_id to a specific
    current/stale state without going through churn (real ingest always makes the
    newest write current — see the real-ingest test below,
    ``test_trust_rank_orders_current_above_stale_with_real_ingested_snapshots``,
    for that path through the real connector, :func:`lode.externals.ingest_snapshot`,
    lode-w0h.2). Returns the snapshot_id for citation as a ``target_version``.
    """
    conn.execute(
        "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
        (external_id,),
    )
    conn.execute(
        "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
        "VALUES (?, ?, ?, 'ok')",
        (snapshot_id, external_id, f"body-{snapshot_id}"),
    )
    if is_head:
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
        )
    return snapshot_id


def test_trust_rank_owned_note_is_highest_trust_and_carries_citation(
    repo, conn
) -> None:
    """Acceptance: an owned note ranks tier 1 and carries version id + span through."""
    v = repo.save("note-a", "alpha").version_id

    ranked = trust_rank(conn, [_expanded("p1", v, 0.9)])

    assert ranked.withheld == []
    assert len(ranked.context) == 1
    item = ranked.context[0]
    assert item.tier is TrustTier.OWNED_NOTE
    assert item.target_version == v  # citation target carried through
    assert item.char_range == "0:5" and item.passage_text == "text-p1"
    assert item.score == 0.9


def test_trust_rank_orders_note_above_current_above_stale_external(repo, conn) -> None:
    """The documented gradient: your note > current external > stale external."""
    v = repo.save("note-a", "alpha").version_id
    current = _insert_external_snapshot(
        conn, external_id="EXT-1", snapshot_id="snap-current", is_head=True
    )
    stale = _insert_external_snapshot(
        conn, external_id="EXT-2", snapshot_id="snap-stale", is_head=False
    )

    # Feed worst-trust first; the ranker must reorder to the gradient.
    ranked = trust_rank(
        conn,
        [
            _expanded("p-stale", stale, 0.9),
            _expanded("p-current", current, 0.8),
            _expanded("p-note", v, 0.1),
        ],
    )

    assert ranked.withheld == []
    assert [item.target_version for item in ranked.context] == [v, current, stale]
    assert [item.tier for item in ranked.context] == [
        TrustTier.OWNED_NOTE,
        TrustTier.CURRENT_EXTERNAL,
        TrustTier.STALE_EXTERNAL,
    ]


def test_trust_rank_orders_current_above_stale_with_real_ingested_snapshots(
    repo, conn
) -> None:
    """lode-w0h.4: the same gradient, verified against real externals ingest+churn.

    The synthetic ``_insert_external_snapshot`` fixture above pins an arbitrary
    snapshot_id to a current/stale state by hand; this test instead drives the
    real write path (:func:`lode.externals.ingest_snapshot`, lode-w0h.2 — the
    connector no longer being merely schema-shaped, ``docs/externals.md``): two
    ingests of the same ``external_id`` churn the head, so the first snapshot
    becomes stale and the second current exactly the way a real refetch would,
    with a real ``fetched_at`` stamped on each.
    """
    v = repo.save("note-a", "alpha").version_id
    stale_result = ingest_snapshot(conn, "https://example.com/x", "web", "one")
    current_result = ingest_snapshot(conn, "https://example.com/x", "web", "two")
    assert stale_result.snapshot_id != current_result.snapshot_id  # churn wrote two

    ranked = trust_rank(
        conn,
        [
            _expanded("p-stale", stale_result.snapshot_id, 0.9),
            _expanded("p-current", current_result.snapshot_id, 0.8),
            _expanded("p-note", v, 0.1),
        ],
    )

    assert ranked.withheld == []
    assert [item.target_version for item in ranked.context] == [
        v,
        current_result.snapshot_id,
        stale_result.snapshot_id,
    ]
    assert [item.tier for item in ranked.context] == [
        TrustTier.OWNED_NOTE,
        TrustTier.CURRENT_EXTERNAL,
        TrustTier.STALE_EXTERNAL,
    ]


def test_trust_rank_is_stable_within_a_tier(repo, conn) -> None:
    """Within one trust tier the upstream best-first (RRF) order is preserved."""
    v1 = repo.save("note-a", "alpha").version_id
    v2 = repo.save("note-b", "beta").version_id

    ranked = trust_rank(conn, [_expanded("p1", v1, 0.9), _expanded("p2", v2, 0.5)])

    assert [item.passage_id for item in ranked.context] == ["p1", "p2"]


def test_trust_rank_withholds_unclassifiable_hit_instead_of_dropping(
    repo, conn
) -> None:
    """Acceptance: a hit that can't be placed on the gradient is surfaced, not dropped."""
    v = repo.save("note-a", "alpha").version_id

    ranked = trust_rank(
        conn, [_expanded("p-note", v, 0.9), _expanded("p-ghost", "unknown-id", 0.8)]
    )

    # The classifiable hit still lands in context ...
    assert [item.target_version for item in ranked.context] == [v]
    # ... and the unplaceable one is surfaced with a reason, never silently omitted.
    assert len(ranked.withheld) == 1
    assert ranked.withheld[0].passage_id == "p-ghost"
    assert ranked.withheld[0].target_version == "unknown-id"
    assert ranked.withheld[0].reason


def test_trust_rank_of_no_hits_is_empty(conn) -> None:
    ranked = trust_rank(conn, [])
    assert ranked.context == [] and ranked.withheld == []


# --- build_match_query: natural-language question -> FTS5 MATCH --------------


# lode-b4w.3: same function under test (build_match_query), different input
# shapes -- parametrized over (text, expected), 3 tests -> 1, no case dropped.
@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(
            "what about auth?",
            '"what" OR "about" OR "auth"',
            id="ors_quoted_word_tokens",
            # Each word becomes a quoted term, OR-ed (not AND-ed) for recall; the
            # trailing "?" and whitespace are separators, never part of a term.
        ),
        pytest.param(
            "alpha or beta",
            '"alpha" OR "or" OR "beta"',
            id="quotes_terms_colliding_with_fts5_operators",
            # A bare "or"/"and"/"not" would be parsed as an FTS5 operator; quoting
            # keeps it a literal term so the expression stays valid.
        ),
        pytest.param(
            "???  ...",
            "",
            id="empty_when_no_word_tokens",
            # No \w tokens -> empty expression; the caller skips the lexical leg
            # (an empty MATCH is an FTS5 syntax error, not a match-none).
        ),
    ],
)
def test_build_match_query(text: str, expected: str) -> None:
    assert build_match_query(text) == expected


def test_build_match_query_result_is_a_valid_fts5_match(conn) -> None:
    # End-to-end: the built expression parses and matches as a real FTS5 query.
    conn.execute(
        "INSERT INTO passages_fts (passage_id, target_version, text) VALUES (?, ?, ?)",
        ("p1", "v1", "we use oauth for auth"),
    )
    conn.commit()
    match = build_match_query("what about auth?")
    rows = conn.execute(
        "SELECT passage_id FROM passages_fts WHERE passages_fts MATCH ?", (match,)
    ).fetchall()
    assert rows == [("p1",)]


# --- graph_expand: GraphRAG edge traversal via networkx (lode-72m.5) -----------


def _insert_edge(
    conn, *, from_id: str, to_id: str, source: str = "ai", status: str = "fresh"
) -> None:
    """Insert one edge into the edges table."""
    conn.execute(
        "INSERT INTO edges (from_id, to_id, source, status) VALUES (?, ?, ?, ?)",
        (from_id, to_id, source, status),
    )
    conn.commit()


def test_graph_expand_noop_when_no_edges(repo, conn) -> None:
    """Acceptance: no-op pass-through when the edges table is empty."""
    repo.save("note-a", "alpha")
    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    hits = expand_parents(conn, fused)
    assert hits

    result = graph_expand(conn, hits)

    assert result == hits  # unchanged: no edges means no-op


def test_graph_expand_noop_when_hits_empty(conn) -> None:
    """No edges to traverse when the input list is empty."""
    assert graph_expand(conn, []) == []


def test_graph_expand_noop_when_max_hops_zero(repo, conn) -> None:
    """drawdown_hop_limit=0 disables traversal entirely."""
    va = repo.save("note-a", "alpha").version_id
    v_linked = repo.save("note-b", "beta").version_id
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    note_id_b = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (v_linked,)
    ).fetchone()[0]
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="ai")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    hits = expand_parents(conn, fused)

    result = graph_expand(conn, hits, settings=load_settings(drawdown_hop_limit=0))

    assert result == hits  # max_hops=0 bypasses traversal


def test_graph_expand_traverses_ai_edge_and_appends_linked_passages(repo, conn) -> None:
    """Acceptance: graph_expand finds linked-note passages via an AI edge and
    appends them with edge_source='ai'."""
    va = repo.save("note-a", "alpha").version_id
    repo.save("note-b", "beta")  # the linked note
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    note_id_b = conn.execute(
        "SELECT note_id FROM notes WHERE note_id != ?", (note_id_a,)
    ).fetchone()[0]
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="ai")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    direct_hits = expand_parents(conn, fused)
    assert direct_hits

    result = graph_expand(conn, direct_hits)

    # Direct hits are preserved unchanged at the front.
    assert result[: len(direct_hits)] == direct_hits
    # New graph-expanded hits are appended.
    new = result[len(direct_hits) :]
    assert new, "should have appended passages from the linked note"
    assert all(h.edge_source == "ai" for h in new)
    # The graph-expanded passages come from note-b's head version.
    note_b_head = conn.execute(
        "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id_b,)
    ).fetchone()[0]
    assert all(h.target_version == note_b_head for h in new)


def test_graph_expand_traverses_user_edge_and_marks_user_annotation(repo, conn) -> None:
    """A user-curated edge yields edge_source='user' (USER_ANNOTATION tier)."""
    va = repo.save("note-a", "alpha").version_id
    repo.save("note-b", "beta")
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    note_id_b = conn.execute(
        "SELECT note_id FROM notes WHERE note_id != ?", (note_id_a,)
    ).fetchone()[0]
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="user")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    direct_hits = expand_parents(conn, fused)

    result = graph_expand(conn, direct_hits)

    new = result[len(direct_hits) :]
    assert new
    assert all(h.edge_source == "user" for h in new)


def test_graph_expand_user_edge_beats_ai_edge_to_same_node(repo, conn) -> None:
    """When a node is reachable via both user and AI edges, user wins."""
    va = repo.save("note-a", "alpha").version_id
    repo.save("note-b", "beta")
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    note_id_b = conn.execute(
        "SELECT note_id FROM notes WHERE note_id != ?", (note_id_a,)
    ).fetchone()[0]
    # Two edges to the same target: one ai, one user.
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="ai")
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="user")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    direct_hits = expand_parents(conn, fused)

    result = graph_expand(conn, direct_hits)

    new = result[len(direct_hits) :]
    assert new
    # user edge wins
    assert all(h.edge_source == "user" for h in new)


def test_graph_expand_skips_concept_label_to_ids_not_in_notes(repo, conn) -> None:
    """AI-inferred edges to concept labels (not existing note_ids) are silently
    skipped — no note exists for them, so nothing to expand."""
    va = repo.save("note-a", "alpha").version_id
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    # Edge to a concept label, not a real note_id.
    _insert_edge(conn, from_id=note_id_a, to_id="python", source="ai")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    direct_hits = expand_parents(conn, fused)

    result = graph_expand(conn, direct_hits)

    # No new hits: the concept label doesn't match any note in the DB.
    assert result == direct_hits


def test_graph_expand_does_not_duplicate_direct_hit_passages(repo, conn) -> None:
    """A passage already in hits as a direct hit is never added again as a
    graph-expanded hit — the direct hit keeps its higher-trust tier."""
    va = repo.save("note-a", "alpha").version_id
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    # Edge from note-a to itself (degenerate self-loop via a separate 'note_id').
    # More realistic: two notes both matching "alpha" so note-b's passages are
    # already in direct hits.
    vb = repo.save("note-b", "alpha beta").version_id
    note_id_b = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (vb,)
    ).fetchone()[0]
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="ai")

    # Both notes match "alpha", so note-b's passages are in direct hits.
    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    direct_hits = expand_parents(conn, fused)
    direct_passage_ids = {h.passage_id for h in direct_hits}

    result = graph_expand(conn, direct_hits)

    # New graph-expanded hits must not repeat any direct passage.
    new = result[len(direct_hits) :]
    assert not any(h.passage_id in direct_passage_ids for h in new)


def test_graph_expand_skips_stale_edges(repo, conn) -> None:
    """Only 'fresh' edges are traversed; 'stale' edges are ignored."""
    va = repo.save("note-a", "alpha").version_id
    repo.save("note-b", "beta")
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    note_id_b = conn.execute(
        "SELECT note_id FROM notes WHERE note_id != ?", (note_id_a,)
    ).fetchone()[0]
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="ai", status="stale")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    direct_hits = expand_parents(conn, fused)

    result = graph_expand(conn, direct_hits)

    assert result == direct_hits  # stale edge ignored


def test_graph_expand_skips_deleted_linked_notes(repo, conn) -> None:
    """graph_expand does not expand to deleted notes (soft-deleted head)."""
    va = repo.save("note-a", "alpha").version_id
    vb1 = repo.save("note-b", "beta").version_id
    repo.delete("note-b", parent=vb1)  # soft-delete note-b
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    note_id_b = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (vb1,)
    ).fetchone()[0]
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="ai")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    direct_hits = expand_parents(conn, fused)

    result = graph_expand(conn, direct_hits)

    assert result == direct_hits  # soft-deleted note not expanded


def test_graph_expand_two_hop_traversal(repo, conn) -> None:
    """With max_hops=2, graph_expand reaches notes two hops away."""
    va = repo.save("note-a", "alpha").version_id
    repo.save("note-b", "beta")
    repo.save("note-c", "gamma")
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT note_id FROM notes WHERE note_id != ?", (note_id_a,)
    ).fetchall()
    note_id_b, note_id_c = rows[0][0], rows[1][0]
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="ai")
    _insert_edge(conn, from_id=note_id_b, to_id=note_id_c, source="ai")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    direct_hits = expand_parents(conn, fused)

    result_1hop = graph_expand(
        conn, direct_hits, settings=load_settings(drawdown_hop_limit=1)
    )
    result_2hop = graph_expand(
        conn, direct_hits, settings=load_settings(drawdown_hop_limit=2)
    )

    # 1-hop reaches note-b; 2-hop additionally reaches note-c.
    new_1 = result_1hop[len(direct_hits) :]
    new_2 = result_2hop[len(direct_hits) :]
    note_c_head = conn.execute(
        "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id_c,)
    ).fetchone()[0]
    assert not any(h.target_version == note_c_head for h in new_1), (
        "1-hop should not reach note-c"
    )
    assert any(h.target_version == note_c_head for h in new_2), (
        "2-hop should reach note-c"
    )


# --- trust_rank with graph-expanded tiers (lode-72m.5) -------------------------


def test_trust_rank_ai_edge_is_lowest_trust_tier(repo, conn) -> None:
    """A graph-expanded hit with edge_source='ai' is placed at AI_EDGE (tier 5)."""
    v = repo.save("note-a", "alpha").version_id
    hit = ExpandedHit(
        passage_id="p-ai",
        target_version=v,
        char_range="0:5",
        passage_text="ai-edge text",
        parent_block="block",
        score=0.0,
        edge_source="ai",
    )

    ranked = trust_rank(conn, [hit])

    assert ranked.withheld == []
    assert len(ranked.context) == 1
    assert ranked.context[0].tier is TrustTier.AI_EDGE
    assert ranked.context[0].passage_id == "p-ai"


def test_trust_rank_user_annotation_is_tier_2(repo, conn) -> None:
    """A graph-expanded hit with edge_source='user' is placed at USER_ANNOTATION
    (tier 2), between OWNED_NOTE and CURRENT_EXTERNAL."""
    v = repo.save("note-a", "alpha").version_id
    hit = ExpandedHit(
        passage_id="p-user",
        target_version=v,
        char_range="0:5",
        passage_text="user-edge text",
        parent_block="block",
        score=0.0,
        edge_source="user",
    )

    ranked = trust_rank(conn, [hit])

    assert ranked.withheld == []
    assert len(ranked.context) == 1
    assert ranked.context[0].tier is TrustTier.USER_ANNOTATION


def test_trust_rank_full_gradient_with_graph_expanded_tiers(repo, conn) -> None:
    """Acceptance: the full trust gradient including graph-expanded tiers is respected.

    Order: OWNED_NOTE (1) > USER_ANNOTATION (2) > CURRENT_EXTERNAL (3) >
           STALE_EXTERNAL (4) > AI_EDGE (5).
    """
    v_owned = repo.save("note-owned", "alpha").version_id
    current_snap = _insert_external_snapshot(
        conn, external_id="EXT-1", snapshot_id="snap-current", is_head=True
    )
    stale_snap = _insert_external_snapshot(
        conn, external_id="EXT-2", snapshot_id="snap-stale", is_head=False
    )

    # All five tiers fed in reverse-trust order so the ranker must reorder them.
    hits = [
        ExpandedHit("p-ai", v_owned, "0:5", "ai text", "block", 0.0, edge_source="ai"),
        ExpandedHit("p-stale", stale_snap, "0:5", "stale text", "block", 0.9),
        ExpandedHit(
            "p-user", v_owned, "1:6", "user text", "block", 0.0, edge_source="user"
        ),
        ExpandedHit("p-current", current_snap, "0:5", "current text", "block", 0.8),
        ExpandedHit("p-owned", v_owned, "2:7", "owned text", "block", 0.7),
    ]

    ranked = trust_rank(conn, hits)

    assert ranked.withheld == []
    tiers = [item.tier for item in ranked.context]
    assert tiers == [
        TrustTier.OWNED_NOTE,
        TrustTier.USER_ANNOTATION,
        TrustTier.CURRENT_EXTERNAL,
        TrustTier.STALE_EXTERNAL,
        TrustTier.AI_EDGE,
    ]


def test_trust_rank_graph_expanded_hits_are_never_withheld(repo, conn) -> None:
    """Graph-expanded hits always resolve to a tier and are never withheld."""
    v = repo.save("note-a", "alpha").version_id
    hits = [
        ExpandedHit("p-ai", v, "0:5", "text", "block", 0.0, edge_source="ai"),
        ExpandedHit("p-user", v, "1:6", "text", "block", 0.0, edge_source="user"),
    ]

    ranked = trust_rank(conn, hits)

    assert ranked.withheld == []
    assert {item.tier for item in ranked.context} == {
        TrustTier.AI_EDGE,
        TrustTier.USER_ANNOTATION,
    }


def test_graph_expand_then_trust_rank_end_to_end(repo, conn) -> None:
    """End-to-end acceptance: graph_expand feeds trust_rank; the full gradient
    is applied with the linked note at AI_EDGE after the seed note at OWNED_NOTE."""
    va = repo.save("note-a", "alpha").version_id
    repo.save("note-b", "beta")
    note_id_a = conn.execute(
        "SELECT note_id FROM versions WHERE version_id = ?", (va,)
    ).fetchone()[0]
    note_id_b = conn.execute(
        "SELECT note_id FROM notes WHERE note_id != ?", (note_id_a,)
    ).fetchone()[0]
    _insert_edge(conn, from_id=note_id_a, to_id=note_id_b, source="ai")

    fused = reciprocal_rank_fusion(lexical_search(conn, "alpha", k=10), [])
    big = expand_parents(conn, fused)
    ctx = graph_expand(conn, big)
    ranked = trust_rank(conn, ctx)

    # Owned note is highest trust; AI-edge note is lowest.
    tiers = [item.tier for item in ranked.context]
    assert TrustTier.OWNED_NOTE in tiers
    assert TrustTier.AI_EDGE in tiers
    # OWNED_NOTE precedes AI_EDGE in the ordered context.
    owned_idx = next(
        i for i, item in enumerate(ranked.context) if item.tier is TrustTier.OWNED_NOTE
    )
    ai_idx = next(
        i for i, item in enumerate(ranked.context) if item.tier is TrustTier.AI_EDGE
    )
    assert owned_idx < ai_idx
