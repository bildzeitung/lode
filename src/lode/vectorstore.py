"""The LanceDB passage-vector store — the regenerable vector cache (lode-x6r.3).

This is the one place LanceDB is touched. It is the **engine behind the cache
seam** (:class:`lode.repository.CacheBackend`, lode-s2f.5): passage vectors are
the regenerable half of the split store (``docs/stack.md`` "Why a split store"),
rebuilt from the irreplaceable SQLite rows, so this module owns only derived
state — losing it costs a re-embed, never data.

It exposes the two halves the rest of the system needs:

- the **write side** — :meth:`VectorStore.replace_vectors`, the cache-fill path
  the embed leg drives (:func:`lode.embedding.embed`, lode-x6r.2). It replaces a
  version's vectors wholesale, so re-embedding the same content-addressed head
  converges instead of accumulating (idempotent by construction).
- the **read side** — :meth:`VectorStore.search`, a **plain dense ANN query** with
  optional metadata filtering. LanceDB earns its place on columnar vectors / ANN /
  metadata filtering (``docs/stack.md``); its *own* native hybrid search is
  deliberately **unused** — the retrieval pipeline (E4) runs FTS5 and this vector
  leg separately and fuses them app-side with reciprocal-rank fusion
  (``docs/retrieval.md`` "Fusion is app-side RRF"), so this store stays a single
  pure-vector index with no lexical leg of its own.

Vectors are stored at the pinned ``embedding_vector_dim`` build constant — LanceDB
fixes the width at table creation (``docs/configuration.md``), and re-keying it
implies a full re-embed (lode-txh.6). sqlite-vec is the documented fallback-down
(``docs/stack.md``); LanceDB is the live store, so it is the only one wired here.
"""

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from lode.config import Settings

#: LanceDB table holding one vector row per passage, keyed by ``passage_id`` and
#: tagged with its ``target_version`` (the content-addressed head it belongs to)
#: and the ``model`` that produced it. Mirrors the ``embeddings`` SQLite table's
#: role (``schema.sql``) but is the *live* vector store (``docs/stack.md``).
_VECTOR_TABLE = "embeddings"

#: Distance metric for the ANN query. The pinned embedder (nomic-embed-text-v1.5,
#: lode-txh.6) is trained for cosine similarity, so cosine is the metric that puts
#: the genuinely nearest passages first; only the resulting *rank* feeds app-side
#: RRF downstream (``docs/retrieval.md``), but ranking on the model's own metric is
#: what makes that rank meaningful.
_METRIC = "cosine"


@dataclass(frozen=True, slots=True)
class VectorHit:
    """One ANN neighbour: the passage it points at and its distance to the query.

    ``passage_id`` joins back to the SQLite ``passages`` row for the passage text
    and span (the read side stores only what identifies a neighbour, not the
    passage body). ``distance`` is the cosine distance under :data:`_METRIC` —
    smaller is nearer; the retrieval pipeline consumes the rank, not the absolute
    value.
    """

    passage_id: str
    target_version: str
    distance: float


class VectorStore:
    """A LanceDB-backed store of passage vectors at the pinned dimension.

    Bound to a ``lance_dir`` (the on-disk LanceDB database) and the build
    ``Settings`` that pin the vector width and embedding model. The same instance
    serves the write side (:meth:`replace_vectors`) and the read side
    (:meth:`search`); both go through one schema, so the table is created once with
    the pinned shape and reused.
    """

    def __init__(self, lance_dir: str | Path, settings: Settings | None = None) -> None:
        self._lance_dir = lance_dir
        self._settings = settings or Settings()

    def _schema(self) -> pa.Schema:
        """The table schema: a fixed-width vector plus the passage metadata.

        The vector width is the ``embedding_vector_dim`` build constant; LanceDB
        needs it fixed at table creation (``docs/configuration.md``), and re-keying
        it implies a full re-embed (lode-txh.6).
        """
        return pa.schema(
            [
                pa.field("passage_id", pa.string()),
                pa.field("target_version", pa.string()),
                pa.field(
                    "vector",
                    pa.list_(pa.float32(), self._settings.embedding_vector_dim),
                ),
                pa.field("model", pa.string()),
            ]
        )

    def _open_or_create_table(self):
        """Open the vector table, creating it with the pinned schema on first use."""
        import lancedb

        db = lancedb.connect(self._lance_dir)
        return db.create_table(_VECTOR_TABLE, schema=self._schema(), exist_ok=True)

    def replace_vectors(
        self, target_version: str, rows: list[dict[str, object]]
    ) -> None:
        """Replace ``target_version``'s vectors with ``rows`` (idempotent re-embed).

        Opens (or, on first write, creates with the pinned schema) the table,
        deletes any rows already keyed to ``target_version``, then adds the fresh
        batch. The delete-then-add is what makes re-embedding the same head version
        converge instead of accumulate. ``target_version`` is a lowercase hex
        content-address (``lode.hashing``), so it is safe to inline in the delete
        predicate. ``rows`` must carry the table's columns (``passage_id``,
        ``target_version``, ``vector``, ``model``); an empty list just clears the
        version.
        """
        table = self._open_or_create_table()
        table.delete(f"target_version = '{target_version}'")
        if rows:
            table.add(rows)

    def search(
        self, query_vector: list[float], *, k: int, where: str | None = None
    ) -> list[VectorHit]:
        """Return the ``k`` nearest passages to ``query_vector``, nearest first.

        A **plain dense ANN query** under the cosine metric (:data:`_METRIC`) — no
        native hybrid; the lexical leg and app-side RRF live in the retrieval
        pipeline (``docs/retrieval.md``). ``where`` is an optional LanceDB filter
        predicate applied as **metadata filtering** before the search (e.g.
        ``"target_version = '<hex>'"`` to scope to one head, or a ``model``
        predicate); callers compose it from trusted/escaped values. Returns at most
        ``k`` :class:`VectorHit` results, empty if the store has no vectors yet (a
        query against a never-written store opens an empty table and finds none).
        """
        query = self._open_or_create_table().search(query_vector).metric(_METRIC)
        if where is not None:
            query = query.where(where)
        return [
            VectorHit(
                passage_id=row["passage_id"],
                target_version=row["target_version"],
                distance=row["_distance"],
            )
            for row in query.limit(k).to_list()
        ]
