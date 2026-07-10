"""Tests for lode.vectorstore — the LanceDB passage-vector store (lode-x6r.3).

Covers the acceptance criteria: vectors persist at the pinned dimension with
their passage metadata, a plain ANN query returns the nearest passages
(nearest-first), and metadata filtering scopes the query. Idempotency of the
write side (re-embedding the same head converges, no duplicate rows) and the
empty-store edges are pinned too. The vector dim is overridden small so the test
vectors are trivial to reason about; the production dim is the pinned build
constant (``Settings.embedding_vector_dim``).
"""

from pathlib import Path

import lancedb

from lode.config import load_settings
from lode.vectorstore import VectorStore

# Small vector dim so the test vectors are trivial; the real dim is the pinned
# build constant.
DIM = 4


def _settings():
    return load_settings(embedding_vector_dim=DIM)


def _row(passage_id: str, target_version: str, vector: list[float]):
    return {
        "passage_id": passage_id,
        "target_version": target_version,
        "vector": vector,
        "model": _settings().embedding_model,
    }


def _store(tmp_path: Path) -> VectorStore:
    return VectorStore(tmp_path / "vectors", _settings())


def test_replace_vectors_persists_at_pinned_dim_with_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_vectors(
        "v1",
        [
            _row("a", "v1", [1.0, 0.0, 0.0, 0.0]),
            _row("b", "v1", [0.0, 1.0, 0.0, 0.0]),
        ],
    )

    table = lancedb.connect(tmp_path / "vectors").open_table("embeddings")
    rows = table.to_arrow().to_pylist()
    assert {r["passage_id"] for r in rows} == {"a", "b"}
    assert all(r["target_version"] == "v1" for r in rows)
    assert all(len(r["vector"]) == DIM for r in rows)
    assert all(r["model"] == _settings().embedding_model for r in rows)


def test_search_returns_nearest_passages_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_vectors(
        "v1",
        [
            _row("near", "v1", [1.0, 0.0, 0.0, 0.0]),
            _row("mid", "v1", [0.9, 0.1, 0.0, 0.0]),
            _row("far", "v1", [0.0, 1.0, 0.0, 0.0]),
        ],
    )

    hits = store.search([1.0, 0.0, 0.0, 0.0], k=2)

    # k caps the result, and they come back nearest-first.
    assert [h.passage_id for h in hits] == ["near", "mid"]
    assert hits[0].distance <= hits[1].distance
    assert hits[0].target_version == "v1"


def test_search_metadata_filter_scopes_results(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_vectors("v1", [_row("a", "v1", [1.0, 0.0, 0.0, 0.0])])
    store.replace_vectors("v2", [_row("c", "v2", [1.0, 0.0, 0.0, 0.0])])

    # Without a filter the nearest hit could be either; the predicate scopes it.
    hits = store.search([1.0, 0.0, 0.0, 0.0], k=5, where="target_version = 'v2'")

    assert [h.passage_id for h in hits] == ["c"]
    assert all(h.target_version == "v2" for h in hits)


def test_replace_vectors_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows = [
        _row("a", "v1", [1.0, 0.0, 0.0, 0.0]),
        _row("b", "v1", [0.0, 1.0, 0.0, 0.0]),
    ]
    store.replace_vectors("v1", rows)
    store.replace_vectors("v1", rows)

    table = lancedb.connect(tmp_path / "vectors").open_table("embeddings")
    persisted = table.to_arrow().to_pylist()
    # Re-running the same head replaces wholesale — no duplicate rows.
    assert len(persisted) == 2
    assert {r["passage_id"] for r in persisted} == {"a", "b"}


def test_replace_vectors_only_touches_its_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_vectors("v1", [_row("a", "v1", [1.0, 0.0, 0.0, 0.0])])
    store.replace_vectors("v2", [_row("c", "v2", [0.0, 1.0, 0.0, 0.0])])

    # Replacing v2 must not disturb v1's rows.
    store.replace_vectors("v2", [_row("c2", "v2", [0.0, 0.0, 1.0, 0.0])])

    table = lancedb.connect(tmp_path / "vectors").open_table("embeddings")
    by_version: dict[str, set[str]] = {}
    for r in table.to_arrow().to_pylist():
        by_version.setdefault(r["target_version"], set()).add(r["passage_id"])
    assert by_version == {"v1": {"a"}, "v2": {"c2"}}


def test_replace_with_empty_rows_clears_the_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_vectors("v1", [_row("a", "v1", [1.0, 0.0, 0.0, 0.0])])
    store.replace_vectors("v1", [])

    assert store.search([1.0, 0.0, 0.0, 0.0], k=5) == []


def test_search_on_empty_store_returns_no_hits(tmp_path: Path) -> None:
    # A query against a store that has never been written must not raise.
    assert _store(tmp_path).search([1.0, 0.0, 0.0, 0.0], k=5) == []


# --- vectors_for (lode-w0h.5's materiality gate reads a target's own vectors) --


def test_vectors_for_returns_every_vector_for_the_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_vectors(
        "v1",
        [
            _row("a", "v1", [1.0, 0.0, 0.0, 0.0]),
            _row("b", "v1", [0.0, 1.0, 0.0, 0.0]),
        ],
    )

    vectors = store.vectors_for("v1")

    assert sorted(vectors) == sorted([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])


def test_vectors_for_scopes_to_its_own_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_vectors("v1", [_row("a", "v1", [1.0, 0.0, 0.0, 0.0])])
    store.replace_vectors("v2", [_row("c", "v2", [0.0, 0.0, 1.0, 0.0])])

    assert store.vectors_for("v1") == [[1.0, 0.0, 0.0, 0.0]]
    assert store.vectors_for("v2") == [[0.0, 0.0, 1.0, 0.0]]


def test_vectors_for_unknown_target_returns_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_vectors("v1", [_row("a", "v1", [1.0, 0.0, 0.0, 0.0])])

    assert store.vectors_for("nonexistent") == []


def test_vectors_for_on_never_written_store_returns_empty(tmp_path: Path) -> None:
    # Mirrors search()'s empty-store handling: opens an empty table, no raise.
    assert _store(tmp_path).vectors_for("v1") == []
