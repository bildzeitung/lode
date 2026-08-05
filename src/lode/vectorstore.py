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
from datetime import timedelta
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
    the pinned shape and reused. A caller that shares one instance across many
    calls gets the opened-table caching described in
    :meth:`_open_or_create_table`.
    """

    def __init__(self, lance_dir: str | Path, settings: Settings | None = None) -> None:
        self._lance_dir = lance_dir
        self._settings = settings or Settings()
        # The opened Table (None until first use) and the writes since it was
        # last pruned -- see _open_or_create_table and replace_vectors.
        self._table = None
        self._writes_since_optimize = 0

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
                # The resolved HuggingFace revision (commit SHA) the embedder
                # actually produced this vector under -- one field on the same
                # per-row write, not a new table
                # (docs/storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81,
                # lode-crh8.1's per-vector mismatch-behavior decision). Nullable:
                # a row written before this field existed simply carries NULL,
                # and a resolution failure at embed time (offline, rate-limited)
                # is recorded as NULL rather than failing the embed (WARN, never
                # REFUSE). NULL is itself a distinct value :meth:`model_revisions`
                # can surface as part of a mixed index.
                pa.field("model_revision", pa.string()),
            ]
        )

    def _open_or_create_table(self):
        """Open the vector table, creating it with the pinned schema on first use.

        **Self-heals a schema-mismatched table (lode-t08v).** A release that adds
        a column to :meth:`_schema` (e.g. ``model``, ``model_revision``,
        lode-crh8.1) leaves any pre-existing on-disk table on the *old* shape;
        ``create_table(exist_ok=True)`` then unconditionally rejects it with
        "Provided schema does not match existing table schema", and every caller
        of this method -- read or write -- goes through here, so that used to mean
        the whole store was stuck until a human ran ``rm -rf`` on the LanceDB
        directory by hand (the interim workaround lode-2lu2 documents). Instead:
        if a table already exists and its schema doesn't match the pinned one,
        drop it and recreate on the current schema before returning. This is
        safe under the module's own regenerable-cache contract (top-of-file
        docstring) -- dropping loses only derived vectors, never the irreplaceable
        SQLite rows, and ``lode reembed`` + ``lode work`` (or the next live
        ``embed()``/``search()`` call, same as any cold cache) repopulate it.
        No flag, no separate rebuild command: this is the single place LanceDB
        is touched, so healing it here covers every call path uniformly.

        **Caches the opened Table across calls on this instance (lode-2brb),**
        so a shared ``VectorStore`` pays ``list_tables`` + ``open_table`` +
        the schema compare on its *first* call only instead of on every embed
        job. LanceDB tables are snapshot-versioned, so a held handle would
        otherwise read a stale version; ``checkout_latest()`` on every reuse
        is what makes holding it safe, and ``tests/test_vectorstore.py``'s
        held-table staleness gate pins that. Note the consequence for the
        schema self-heal above: it too is first-call-only per instance, which
        is sound because ``Settings`` (and so the pinned schema) is fixed for
        an instance's lifetime.

        Why the Table and not the ``lancedb.connect()`` handle, and the
        measurements behind that: ``docs/decisions.md`` (lode-2brb).
        """
        import lancedb

        if self._table is not None:
            self._table.checkout_latest()
            return self._table

        db = lancedb.connect(self._lance_dir)
        schema = self._schema()
        if _VECTOR_TABLE in db.list_tables().tables:
            table = db.open_table(_VECTOR_TABLE)
            if table.schema != schema:
                db.drop_table(_VECTOR_TABLE)
                table = db.create_table(_VECTOR_TABLE, schema=schema)
        else:
            table = db.create_table(_VECTOR_TABLE, schema=schema, exist_ok=True)
        self._table = table
        return table

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
        ``target_version``, ``vector``, ``model``); ``model_revision`` is optional
        per row (a missing key converts to ``NULL``, same as an explicit ``None``)
        — an empty list just clears the version.

        **Periodically prunes the held Table's version history (lode-2brb).**
        Each call is a delete + add = 2 new LanceDB versions, and a Table held
        across many calls accumulates that history in memory. Every
        ``settings.vectorstore_optimize_interval`` calls this prunes all but
        the latest version -- safe because this store's vectors are a
        regenerable cache (module docstring), so nothing here needs history.

        Deliberately does **not** pass ``delete_unverified=True``: it would
        lift LanceDB's protection for young files that may belong to *another
        process's* in-progress transaction (its own docs warn that "the
        dataset could be put into a corrupted state"), and it was measured to
        buy nothing here. Measurements: ``docs/decisions.md`` (lode-2brb).
        """
        table = self._open_or_create_table()
        table.delete(f"target_version = '{target_version}'")
        if rows:
            table.add(rows)
        self._writes_since_optimize += 1
        if self._writes_since_optimize >= self._settings.vectorstore_optimize_interval:
            table.optimize(cleanup_older_than=timedelta(0))
            table.checkout_latest()
            self._writes_since_optimize = 0

    def vectors_for(self, target_version: str) -> list[list[float]]:
        """Return every passage vector persisted for ``target_version``, unordered.

        A plain metadata-filtered scan — no ANN query, no ``query_vector`` — for
        a caller that wants ``target_version``'s *own* vectors rather than its
        nearest neighbours (lode-w0h.5's post-embed materiality gate: it
        mean-pools a snapshot's passage vectors into one document-level vector
        to compare against its predecessor's). Empty if the store has no
        vectors for this target (never embedded, or embedded with a body that
        chunked to zero passages) — mirrors :meth:`search`'s empty-store
        handling, including on a never-written store (opens an empty table and
        finds none).
        """
        rows = (
            self._open_or_create_table()
            .search()
            .where(f"target_version = '{target_version}'")
            .to_list()
        )
        return [row["vector"] for row in rows]

    def model_revisions(self, model: str) -> set[str | None]:
        """Distinct ``model_revision`` values recorded for ``model``'s live vectors.

        **This is "the manifest"** — per the ``lode-crh8.1`` decision
        (``docs/storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81``)
        there is no separate manifest file/table; the manifest is this aggregate
        read over the ``embeddings`` rows already written. A plain metadata-filtered
        scan, no ANN query — mirrors :meth:`vectors_for`'s shape.

        More than one distinct value means the index is currently **mixed**: some
        passages under ``model`` were embedded under a different resolved revision
        than others (e.g. a mid-corpus cache eviction and re-pull), detectable
        structurally with no separate bookkeeping. ``None`` is itself a member the
        set can carry — a row written before the ``model_revision`` field existed,
        or one where the revision probe failed at embed time, is recorded as
        ``NULL`` and shows up here as ``None``, not omitted. Empty if the store has
        never written a vector under ``model`` (including a never-written store,
        which opens empty and finds none — mirrors :meth:`search`/:meth:`vectors_for`).

        Projects the ``model_revision`` column only (unlike :meth:`vectors_for`,
        which needs the vectors themselves) so the scan never deserializes a
        single fixed-width ``vector`` blob just to read one string per row.
        """
        rows = (
            self._open_or_create_table()
            .search()
            .where(f"model = '{model}'")
            .select(["model_revision"])
            .to_list()
        )
        return {row["model_revision"] for row in rows}

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
