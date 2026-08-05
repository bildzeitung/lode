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
import pyarrow as pa

from lode.config import load_settings
from lode.vectorstore import VectorStore

# Small vector dim so the test vectors are trivial; the real dim is the pinned
# build constant.
DIM = 4


def _settings():
    return load_settings(embedding_vector_dim=DIM)


def _row(
    passage_id: str,
    target_version: str,
    vector: list[float],
    *,
    model_revision: str | None = None,
):
    return {
        "passage_id": passage_id,
        "target_version": target_version,
        "vector": vector,
        "model": _settings().embedding_model,
        "model_revision": model_revision,
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
    # model_revision is nullable -- _row()'s default (never passed here).
    assert all(r["model_revision"] is None for r in rows)


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


# --- model_revisions (lode-g274.4's "the manifest is this per-vector data") ---
#
# docs/storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81:
# there is no separate manifest artifact -- it's the aggregate of the existing
# per-row model_revision field. More than one distinct value means the index
# is currently mixed.


def test_model_revisions_on_never_written_store_returns_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).model_revisions(_settings().embedding_model) == set()


def test_model_revisions_returns_the_single_recorded_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    model = _settings().embedding_model
    store.replace_vectors(
        "v1",
        [
            _row("a", "v1", [1.0, 0.0, 0.0, 0.0], model_revision="sha-1"),
            _row("b", "v1", [0.0, 1.0, 0.0, 0.0], model_revision="sha-1"),
        ],
    )

    assert store.model_revisions(model) == {"sha-1"}


def test_model_revisions_detects_a_mixed_index(tmp_path: Path) -> None:
    # Some passages embedded under one revision, others under a different one
    # (e.g. a mid-corpus cache eviction and re-pull) -- more than one distinct
    # value means the index is structurally mixed.
    store = _store(tmp_path)
    model = _settings().embedding_model
    store.replace_vectors(
        "v1",
        [
            _row("a", "v1", [1.0, 0.0, 0.0, 0.0], model_revision="sha-1"),
            _row("b", "v1", [0.0, 1.0, 0.0, 0.0], model_revision="sha-2"),
        ],
    )

    assert store.model_revisions(model) == {"sha-1", "sha-2"}


def test_model_revisions_includes_none_for_rows_that_predate_the_field(
    tmp_path: Path,
) -> None:
    # A row written before model_revision existed (or whose probe failed at
    # embed time) carries NULL -- surfaced here as None, a distinct member of
    # the set, not silently omitted.
    store = _store(tmp_path)
    model = _settings().embedding_model
    store.replace_vectors(
        "v1",
        [
            _row("a", "v1", [1.0, 0.0, 0.0, 0.0], model_revision="sha-1"),
            _row("b", "v1", [0.0, 1.0, 0.0, 0.0]),  # model_revision=None default
        ],
    )

    assert store.model_revisions(model) == {"sha-1", None}


# --- schema-mismatch self-heal (lode-t08v) ---------------------------------
#
# A release that adds a column to VectorStore._schema() (e.g. model_revision,
# lode-crh8.1) leaves a pre-existing on-disk table on the old shape.
# create_table(exist_ok=True) used to reject that unconditionally with a
# "Schema Error", crashing every caller (search, status, reembed alike) until
# a human ran `rm -rf` on the lancedb dir by hand (lode-2lu2's documented
# workaround). These pin the self-heal: _open_or_create_table detects the
# mismatch and drops+recreates on the current schema before returning.


def _write_stale_schema_table(tmp_path: Path) -> None:
    """Simulate an old on-disk table that predates the ``model_revision`` column.

    Writes a real table under the pinned dim but with an older, narrower
    schema than :meth:`VectorStore._schema` currently declares -- the exact
    shape a pre-lode-crh8.1 table would have. Carries one row under the
    ``"v1"`` target so the self-heal tests can assert it's gone after the
    heal (dropping the stale table is expected to lose it -- the regenerable-
    cache contract, module docstring).
    """
    old_schema = pa.schema(
        [
            pa.field("passage_id", pa.string()),
            pa.field("target_version", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), DIM)),
            pa.field("model", pa.string()),
            # no model_revision column -- the stale, pre-migration shape.
        ]
    )
    db = lancedb.connect(tmp_path / "vectors")
    db.create_table(
        "embeddings",
        schema=old_schema,
        data=[
            {
                "passage_id": "stale",
                "target_version": "v1",
                "vector": [1.0, 0.0, 0.0, 0.0],
                "model": _settings().embedding_model,
            }
        ],
    )


def test_replace_vectors_self_heals_a_schema_mismatched_table(
    tmp_path: Path,
) -> None:
    _write_stale_schema_table(tmp_path)
    store = _store(tmp_path)

    # Used to raise "Schema Error: Provided schema does not match existing
    # table schema" -- must now self-heal (drop + recreate) and write clean.
    store.replace_vectors("v2", [_row("b", "v2", [0.0, 1.0, 0.0, 0.0])])

    table = lancedb.connect(tmp_path / "vectors").open_table("embeddings")
    assert table.schema == store._schema()
    rows = table.to_arrow().to_pylist()
    assert {r["passage_id"] for r in rows} == {"b"}


def test_self_heal_drops_the_stale_rows(tmp_path: Path) -> None:
    # Dropping the mismatched table is a real, expected side effect -- the
    # pre-existing "v1" row (written under the old schema) does not survive
    # the heal. Losing it costs a re-embed, never data (module docstring):
    # the SQLite rows it was derived from are untouched.
    _write_stale_schema_table(tmp_path)
    store = _store(tmp_path)

    store.replace_vectors("v2", [_row("b", "v2", [0.0, 1.0, 0.0, 0.0])])

    assert store.vectors_for("v1") == []


def test_search_self_heals_a_schema_mismatched_table(tmp_path: Path) -> None:
    # Every caller of _open_or_create_table shares the heal -- a read path
    # must not crash on a mismatched table either; it recreates it empty,
    # same as any cold/never-written store.
    _write_stale_schema_table(tmp_path)
    store = _store(tmp_path)

    assert store.search([1.0, 0.0, 0.0, 0.0], k=5) == []


def test_matching_schema_table_is_not_dropped(tmp_path: Path) -> None:
    # The heal must not fire (and so must not discard data) when the
    # on-disk schema already matches -- only a genuine mismatch drops.
    store = _store(tmp_path)
    store.replace_vectors("v1", [_row("a", "v1", [1.0, 0.0, 0.0, 0.0])])

    # A second, unrelated write through a fresh VectorStore instance (same
    # schema) must not disturb the first version's already-written row.
    _store(tmp_path).replace_vectors("v2", [_row("c", "v2", [0.0, 1.0, 0.0, 0.0])])

    assert store.vectors_for("v1") == [[1.0, 0.0, 0.0, 0.0]]


# --- held-table staleness gate (lode-2brb) ---------------------------------
#
# VectorStore caches its opened Table across calls (lode-2brb). A held LanceDB
# Table handle is a fixed snapshot -- it does NOT see a write made through a
# different connection until `checkout_latest()` is called. These pin that the
# cache calls it on every use, so a shared instance never reads stale data.


def test_a_second_connection_writing_is_still_visible_through_the_first(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.replace_vectors("v1", [_row("a", "v1", [1.0, 0.0, 0.0, 0.0])])
    # Opens (and caches) the Table.
    assert store.vectors_for("v1") == [[1.0, 0.0, 0.0, 0.0]]

    # A second, independent VectorStore instance -- its own connection --
    # writes a version the first instance has never touched.
    _store(tmp_path).replace_vectors("v2", [_row("c", "v2", [0.0, 1.0, 0.0, 0.0])])

    # The first instance's cached Table must see it -- not a stale snapshot.
    assert store.vectors_for("v2") == [[0.0, 1.0, 0.0, 0.0]]


def test_periodic_optimize_prunes_history_without_losing_current_rows(
    tmp_path: Path,
) -> None:
    """The every-N-writes `optimize()` prunes history and keeps the live rows.

    The interval defaults to 200 (`vectorstore_optimize_interval`), so nothing
    else in the suite reaches this branch -- it shipped entirely unexercised
    despite running a destructive prune against the live store (found on
    technical review). Driven here with a small interval.
    """
    settings = load_settings(embedding_vector_dim=DIM, vectorstore_optimize_interval=2)
    store = VectorStore(tmp_path / "vectors", settings)

    for i in range(2):
        store.replace_vectors("v1", [_row(f"a{i}", "v1", [1.0, 0.0, 0.0, 0.0])])

    # The prune fired on the 2nd write and re-armed its counter.
    assert store._writes_since_optimize == 0
    # ...and the history it pruned is genuinely gone, from an independent
    # connection -- not just invisible through the held handle.
    versions = (
        lancedb.connect(tmp_path / "vectors").open_table("embeddings").list_versions()
    )
    assert len(versions) == 1, (
        f"expected history pruned to the latest version, got {versions}"
    )

    # The current rows survived the prune, through the held handle and a fresh one.
    assert store.vectors_for("v1") == [[1.0, 0.0, 0.0, 0.0]]
    assert _store(tmp_path).vectors_for("v1") == [[1.0, 0.0, 0.0, 0.0]]

    # And the store keeps working across the boundary.
    store.replace_vectors("v1", [_row("b", "v1", [0.0, 1.0, 0.0, 0.0])])
    assert store._writes_since_optimize == 1
    assert store.vectors_for("v1") == [[0.0, 1.0, 0.0, 0.0]]


def test_model_revisions_scopes_to_the_requested_model(tmp_path: Path) -> None:
    # A different model's rows must not bleed into this model's manifest read.
    store = _store(tmp_path)
    store.replace_vectors(
        "v1",
        [_row("a", "v1", [1.0, 0.0, 0.0, 0.0], model_revision="sha-1")],
    )
    table = lancedb.connect(tmp_path / "vectors").open_table("embeddings")
    table.add(
        [
            {
                "passage_id": "z",
                "target_version": "v1",
                "vector": [0.0, 0.0, 0.0, 1.0],
                "model": "some-other/model",
                "model_revision": "sha-other",
            }
        ]
    )

    assert store.model_revisions(_settings().embedding_model) == {"sha-1"}
    assert store.model_revisions("some-other/model") == {"sha-other"}
