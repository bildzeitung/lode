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
    ExpandedHit,
    FusedHit,
    TrustTier,
    build_match_query,
    expand_parents,
    lexical_search,
    live_head_versions,
    reciprocal_rank_fusion,
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
    """Insert an external + one snapshot; point head at it iff ``is_head``.

    Externals/snapshots are UNUSED until connectors (schema), so the test seeds the
    rows directly to exercise the current/stale classification. Returns the
    snapshot_id for citation as a ``target_version``.
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


def test_build_match_query_ors_quoted_word_tokens() -> None:
    # Each word becomes a quoted term, OR-ed (not AND-ed) for recall; the trailing
    # "?" and whitespace are separators, never part of a term.
    assert build_match_query("what about auth?") == '"what" OR "about" OR "auth"'


def test_build_match_query_quotes_terms_colliding_with_fts5_operators() -> None:
    # A bare "or"/"and"/"not" would be parsed as an FTS5 operator; quoting keeps it
    # a literal term so the expression stays valid.
    assert build_match_query("alpha or beta") == '"alpha" OR "or" OR "beta"'


def test_build_match_query_empty_when_no_word_tokens() -> None:
    # No \w tokens -> empty expression; the caller skips the lexical leg (an empty
    # MATCH is an FTS5 syntax error, not a match-none).
    assert build_match_query("???  ...") == ""


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
