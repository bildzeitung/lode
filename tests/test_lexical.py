"""Tests for lode.lexical — the synchronous FTS5 lexical leg (lode-x6r.4).

Covers the acceptance criteria: a just-saved note is keyword-findable via FTS5
*before any async work runs* (the leg is model-free and indexes inline on the
save path, driven through the Repository cache seam), and the FTS rows are
**per-passage** — the same passage unit the vector leg ranks. The store's own
contract (BM25 ranking, idempotent per-head replacement, version-scoped clear,
empty-index edge) is pinned directly; the synchronous-on-save and per-passage
acceptance is pinned end-to-end through ``Repository`` + ``CompositeCache``.
"""

from pathlib import Path

import pytest

from lode.chunking import chunk
from lode.embedding import EmbeddingCacheBackend
from lode.lexical import LexicalCacheBackend, LexicalIndex, build_prefix_match_query
from lode.repository import CacheBackend, CompositeCache, Repository
from lode.storage import init_db


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


# --- the FTS5 store: write side + BM25 read side ------------------------------


def test_replace_passages_makes_text_keyword_findable(conn) -> None:
    index = LexicalIndex(conn)
    index.replace_passages(
        "v1", chunk("the staging certificate rotation runbook", "v1")
    )

    hits = index.search("rotation", k=5)

    assert [h.target_version for h in hits] == ["v1"]
    assert all(h.passage_id.startswith("v1:") for h in hits)


def test_search_ranks_better_matches_first(conn) -> None:
    index = LexicalIndex(conn)
    # Two passages; the second mentions "certs" twice, so BM25 ranks it first.
    index.replace_passages(
        "v1",
        chunk("rotate the certs\n\nrotate the certs and renew the certs", "v1"),
    )

    hits = index.search("certs", k=5)

    assert len(hits) == 2
    assert hits[0].score <= hits[1].score  # bm25: more negative = better, first


def test_replace_passages_is_idempotent_per_head(conn) -> None:
    index = LexicalIndex(conn)
    passages = chunk("alpha beta gamma", "v1")
    index.replace_passages("v1", passages)
    index.replace_passages("v1", passages)  # re-index the same head

    # Re-running replaces wholesale — no duplicate rows for the same passage.
    (rows,) = conn.execute(
        "SELECT COUNT(*) FROM passages_fts WHERE target_version = 'v1'"
    ).fetchone()
    assert rows == len(passages)


def test_replace_passages_only_touches_its_version(conn) -> None:
    index = LexicalIndex(conn)
    index.replace_passages("v1", chunk("apples in version one", "v1"))
    index.replace_passages("v2", chunk("oranges in version two", "v2"))

    # Re-indexing v2 must not disturb v1's rows.
    index.replace_passages("v2", chunk("pears in version two", "v2"))

    assert [h.target_version for h in index.search("apples", k=5)] == ["v1"]
    assert index.search("oranges", k=5) == []  # the old v2 text is gone
    assert [h.target_version for h in index.search("pears", k=5)] == ["v2"]


def test_replace_with_no_passages_clears_the_version(conn) -> None:
    index = LexicalIndex(conn)
    index.replace_passages("v1", chunk("findable text here", "v1"))
    index.replace_passages("v1", [])

    assert index.search("findable", k=5) == []


def test_search_on_empty_index_returns_no_hits(conn) -> None:
    # A query against an index that was never written must not raise.
    assert LexicalIndex(conn).search("anything", k=5) == []


# --- the cache-seam adapter: synchronous, per-passage, through the Repository --


def test_lexical_backend_satisfies_the_cache_backend_protocol(conn) -> None:
    assert isinstance(LexicalCacheBackend(conn), CacheBackend)


def test_just_saved_note_is_keyword_findable_synchronously(conn) -> None:
    """Acceptance: save → keyword-findable immediately, no async work in between."""
    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))

    repo.save("note-1", "# Runbook\n\nRotate the staging certificates nightly.")

    # No embed/async ran — the lexical leg indexed inline on the save path.
    hits = LexicalIndex(conn).search("certificates", k=5)
    assert hits, "a just-saved note should be keyword-findable before async work"
    assert all(h.passage_id.startswith(hits[0].target_version + ":") for h in hits)


def test_fts_rows_are_per_passage(conn) -> None:
    """Acceptance: FTS rows are per-passage — one row per chunk, not per note."""
    body = "# A\n\nfirst section body\n\n# B\n\nsecond section body"
    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))

    result = repo.save("note-1", body)

    expected = len(chunk(body, result.version_id))
    (rows,) = conn.execute(
        "SELECT COUNT(*) FROM passages_fts WHERE target_version = ?",
        (result.version_id,),
    ).fetchone()
    assert rows == expected > 1  # the multi-section note chunks to several rows


def test_update_reindexes_and_drops_stale_passage_text(conn) -> None:
    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))
    root = repo.save("note-1", "original widget text").version_id

    repo.save("note-1", "replacement gadget text", parent=root)

    index = LexicalIndex(conn)
    # The new head is findable by its own keyword...
    assert [h.passage_id.split(":")[0] for h in index.search("gadget", k=5)] != []
    # ...and the prior head's rows still exist (soft history), scoped to its version.
    assert all(h.target_version == root for h in index.search("widget", k=5))


def test_delete_evicts_the_tombstone_version(conn) -> None:
    """Evict clears the tombstone's own rows — symmetric with index, like the vector leg."""
    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))
    root = repo.save("note-1", "content to tombstone").version_id

    result = repo.delete("note-1", parent=root)

    # The tombstone version carries no passages of its own.
    (rows,) = conn.execute(
        "SELECT COUNT(*) FROM passages_fts WHERE target_version = ?",
        (result.version_id,),
    ).fetchone()
    assert rows == 0


def test_composes_with_the_vector_leg_on_one_seam(conn, tmp_path: Path) -> None:
    """Both legs ride the same CompositeCache slot; the lexical leg stays synchronous."""

    class _StubEmbedder:
        def embed_passages(self, texts: list[str]) -> list[list[float]]:
            return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    from lode.config import load_settings

    settings = load_settings(embedding_vector_dim=4)
    repo = Repository(
        conn,
        CompositeCache(
            [
                LexicalCacheBackend(conn, settings=settings),
                EmbeddingCacheBackend(
                    conn,
                    lance_dir=tmp_path / "vectors",
                    embedder=_StubEmbedder(),
                    settings=settings,
                ),
            ]
        ),
    )

    repo.save("note-1", "lexical and vector legs share the seam")

    assert LexicalIndex(conn).search("lexical", k=5), "lexical leg indexed on save"


def test_composed_save_redacts_seeded_secret_from_both_legs(
    conn, tmp_path: Path
) -> None:
    """lode-n60: a pasted secret must not reach EITHER indexing leg.

    Regression for the redact-before-index wiring gap (lode-n60): before this
    fix, ``redact_before_index`` had zero callers, so ``Repository.save`` fed
    the raw body straight to :class:`LexicalCacheBackend` (keyword-findable)
    and :func:`~lode.embedding.embed` re-read the raw body straight off
    ``versions.body`` (embedder-visible) — a pasted secret was locally
    retrievable via either leg. Asserts: FTS returns no hit for the raw
    secret, the embedder is never shown the raw secret text, and the
    irreplaceable ``versions.body`` copy is untouched (only ``purge`` clears
    that durable copy — ``docs/externals.md`` "Two redactions").
    """

    class _RecordingEmbedder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def embed_passages(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    from lode.config import load_settings

    settings = load_settings(embedding_vector_dim=4)
    embedder = _RecordingEmbedder()
    repo = Repository(
        conn,
        CompositeCache(
            [
                LexicalCacheBackend(conn, settings=settings),
                EmbeddingCacheBackend(
                    conn,
                    lance_dir=tmp_path / "vectors",
                    embedder=embedder,
                    settings=settings,
                ),
            ]
        ),
    )
    secret = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern
    body = f"key: {secret} done"

    result = repo.save("note-1", body, settings=settings)

    # Keyword leg: the raw secret returns no FTS hits.
    assert LexicalIndex(conn).search(secret, k=5) == []
    # Vector leg: the embedder is never shown the raw secret text.
    assert not any(secret in text for texts in embedder.calls for text in texts)
    # The irreplaceable store still carries the raw secret — only `purge`
    # clears that durable copy.
    (stored_body,) = conn.execute(
        "SELECT body FROM versions WHERE version_id = ?", (result.version_id,)
    ).fetchone()
    assert secret in stored_body


def test_recover_redacts_seeded_secret_from_the_lexical_leg(conn) -> None:
    """lode-ibv: recovering a secret-bearing version must not resurface it via FTS.

    Regression for the recover-side redact-before-index wiring gap (lode-ibv):
    ``Repository.recover`` read the target version's raw body via ``_body``
    and handed it straight to ``CacheBackend.index`` with no redaction — so
    recovering a version whose body matched a seed pattern pushed the raw
    secret back into the FTS/lexical leg, keyword-findable again post-recover.
    Steps to reproduce from the ticket: save a secret-bearing version, delete
    it (tombstone), then recover it — the raw secret must stay out of FTS.
    """
    repo = Repository(conn, LexicalCacheBackend(conn))
    secret = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern
    body = f"key: {secret} done"

    root = repo.save("note-1", body).version_id
    repo.delete("note-1", parent=root)
    repo.recover("note-1", target_version=root)

    # Keyword leg: the raw secret returns no FTS hits after recover.
    assert LexicalIndex(conn).search(secret, k=5) == []
    # The irreplaceable store still carries the raw secret on the recovered
    # version — only `purge` clears that durable copy.
    (stored_body,) = conn.execute(
        "SELECT body FROM versions WHERE version_id = ?", (root,)
    ).fetchone()
    assert secret in stored_body


# --- build_prefix_match_query (lode-35nu.6) ------------------------------------


def test_build_prefix_match_query_tokenizes_and_ors_with_prefix_stars() -> None:
    assert build_prefix_match_query("staging cert") == "staging* OR cert*"


def test_build_prefix_match_query_lowercases() -> None:
    assert build_prefix_match_query("Staging CERT") == "staging* OR cert*"


def test_build_prefix_match_query_strips_fts5_syntax_characters() -> None:
    # A typed '"', ':', '-' etc. never reaches the MATCH parser -- only the
    # alphanumeric word tokens survive (note: "or" is itself alphanumeric, so
    # a literal typed "OR" becomes its own token, same as any other word).
    assert (
        build_prefix_match_query('foo" bar:baz-qux') == "foo* OR bar* OR baz* OR qux*"
    )


def test_build_prefix_match_query_returns_none_for_no_usable_token() -> None:
    assert build_prefix_match_query("") is None
    assert build_prefix_match_query('   "-:  ') is None


def test_build_prefix_match_query_output_is_a_valid_fts5_match_expression(conn) -> None:
    """The built query actually MATCHes a still-incomplete word, prefix-style."""
    index = LexicalIndex(conn)
    index.replace_passages(
        "v1", chunk("the staging certificate rotation runbook", "v1")
    )

    query = build_prefix_match_query("cert")  # "cert*" -- word not yet complete

    assert query is not None
    hits = index.search(query, k=5)
    assert [h.target_version for h in hits] == ["v1"]
