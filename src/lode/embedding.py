"""In-process embedding of a version's passages (lode-x6r.2).

This is the **embed leg** of E3 local indexing: it takes a head ``version_id``,
chunks its body into passages (:func:`lode.chunking.chunk` — landed, imported,
never reimplemented here), embeds each passage with a **local CPU model**, and
persists the passage vectors to **LanceDB** — the decided home for the
regenerable vector cache (``docs/stack.md`` "Why a split store": vectors live in
LanceDB; the ``embeddings`` SQLite table is the data-shape row / sqlite-vec
fallback, not written here while LanceDB is the live store).

**Local, in-process, no network for inference.** The model runs on the bundled
ONNX runtime via ``fastembed`` (``docs/stack.md`` / ``docs/configuration.md``) —
CPU, no torch, **not** Ollama. The model id and its output dimension are **build
constants** pinned in :mod:`lode.config` (``embedding_model`` /
``embedding_vector_dim``): re-keying the model re-keys the whole vector space and
implies a full re-embed, so they are read from config, never taken as a runtime
knob (lode-txh.6).

:func:`embed` is the handler the ``embed`` derive job (enqueued on capture by
:mod:`lode.jobs`, lode-y42.1) calls. For the phase-a walking skeleton it runs
**synchronously inline on save** (no worker yet); moving it onto the E2 async
work queue is the follow-up (lode-x6r.5). It is **idempotent** by construction:
passage ids are deterministic (``chunk`` derives them from the content-addressed
``target_version``), passages are upserted on their primary key, and the vector
rows for a ``target_version`` are replaced wholesale on each run — so re-embedding
the same head version converges to the same state instead of duplicating.

The embedder is injected (:class:`Embedder`) so the model is constructed lazily
and only in production: the default loads ``fastembed`` on first use, while tests
pass a stub and stay fast + offline (the real model download is exercised by the
opt-in smoke test ``tests/test_models_smoke.py``, lode-txh.6).
"""

import sqlite3
from pathlib import Path
from typing import Protocol

import pyarrow as pa

from lode.chunking import Passage, chunk
from lode.config import Settings

#: LanceDB table holding one vector row per passage. Mirrors the ``embeddings``
#: SQLite table's role (``schema.sql``) — passage_id keyed — but is the *live*
#: vector store (``docs/stack.md``).
_VECTOR_TABLE = "embeddings"

#: Task prefix nomic-embed-text-v1.5 expects on documents being indexed (the
#: query side uses ``search_query:``). It is a fixed property of the pinned
#: ``embedding_model`` build constant — re-keying the model updates this with it —
#: so it rides the default embedder rather than a separate config knob. Matches
#: the embedder usage proven in ``tests/test_models_smoke.py`` (lode-txh.6).
_DOCUMENT_PREFIX = "search_document: "


class Embedder(Protocol):
    """Embeds a batch of passage texts into fixed-dimension vectors.

    The one seam between :func:`embed` and the model: production uses
    :class:`FastEmbedEmbedder`; tests pass a stub so the gate never downloads a
    model. Implementations return one vector per input text, in order, each of
    length ``Settings.embedding_vector_dim``.
    """

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per text in ``texts``, in input order."""
        ...


class FastEmbedEmbedder:
    """Default :class:`Embedder`: the pinned local ONNX model via ``fastembed``.

    Constructs ``fastembed.TextEmbedding`` for ``settings.embedding_model`` lazily
    on first :meth:`embed_passages` call (the model download/load is hundreds of
    MB, so it is deferred out of import and out of any code path that never
    embeds). Passages are prefixed with :data:`_DOCUMENT_PREFIX` as the model
    requires for indexed documents.
    """

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.embedding_model
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        prefixed = [f"{_DOCUMENT_PREFIX}{text}" for text in texts]
        # fastembed yields one numpy array per input, in order.
        return [vector.tolist() for vector in model.embed(prefixed)]


def _version_body(conn: sqlite3.Connection, target_version: str) -> str:
    """Return the body of ``target_version``; raise ``KeyError`` if absent."""
    row = conn.execute(
        "SELECT body FROM versions WHERE version_id = ?", (target_version,)
    ).fetchone()
    if row is None:
        raise KeyError(target_version)
    return row[0]


def _persist_passages(conn: sqlite3.Connection, passages: list[Passage]) -> None:
    """Upsert ``passages`` into the SQLite ``passages`` table (idempotent on re-run).

    Passage ids are deterministic, so ``INSERT OR REPLACE`` on the primary key
    makes a re-embed of the same head version a no-op rewrite rather than a
    duplicate. The ``embeddings`` FK references these rows, so they must exist
    before the vectors are written.
    """
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO passages "
            "(passage_id, target_version, ord, char_range, text, parent_block) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    p.passage_id,
                    p.target_version,
                    p.ord,
                    p.char_range,
                    p.text,
                    p.parent_block,
                )
                for p in passages
            ],
        )


def _vector_schema(settings: Settings) -> pa.Schema:
    """The LanceDB table schema: a fixed-width vector keyed by passage_id.

    The vector width is the ``embedding_vector_dim`` build constant; LanceDB needs
    it fixed at table creation (``docs/configuration.md``), and re-keying it
    implies a full re-embed.
    """
    return pa.schema(
        [
            pa.field("passage_id", pa.string()),
            pa.field("target_version", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), settings.embedding_vector_dim)),
            pa.field("model", pa.string()),
        ]
    )


def _write_vectors(
    lance_dir: str | Path,
    target_version: str,
    rows: list[dict[str, object]],
    settings: Settings,
) -> None:
    """Replace ``target_version``'s vectors in LanceDB with ``rows`` (idempotent).

    Opens (or, on first write, creates with the pinned schema) the vector table
    via ``exist_ok``, deletes any rows already keyed to ``target_version``, then
    adds the fresh batch. The delete-then-add is what makes re-embedding the same
    head version converge instead of accumulate. ``target_version`` is a lowercase
    hex content-address (``lode.hashing``), so it is safe to inline in the delete
    predicate.
    """
    import lancedb

    db = lancedb.connect(lance_dir)
    table = db.create_table(
        _VECTOR_TABLE, schema=_vector_schema(settings), exist_ok=True
    )
    table.delete(f"target_version = '{target_version}'")
    if rows:
        table.add(rows)


def embed(
    conn: sqlite3.Connection,
    target_version: str,
    *,
    lance_dir: str | Path,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> int:
    """Chunk, embed, and persist the passages of ``target_version``.

    Reads the version body from ``conn``, chunks it into passages
    (:func:`lode.chunking.chunk`), upserts those passages into the SQLite
    ``passages`` table, embeds each passage with ``embedder`` (default: the pinned
    local ONNX model via :class:`FastEmbedEmbedder`), and replaces the version's
    passage vectors in the LanceDB store under ``lance_dir``.

    Idempotent: running twice on the same head version converges to the same
    passages and vectors. Returns the number of passages embedded (0 for a body
    that chunks to nothing). Raises ``KeyError`` if ``target_version`` is unknown.
    """
    settings = settings or Settings()
    body = _version_body(conn, target_version)
    passages = chunk(body, target_version, settings=settings)
    _persist_passages(conn, passages)

    embedder = embedder or FastEmbedEmbedder(settings)
    vectors = embedder.embed_passages([p.text for p in passages]) if passages else []
    rows: list[dict[str, object]] = [
        {
            "passage_id": p.passage_id,
            "target_version": p.target_version,
            "vector": vector,
            "model": settings.embedding_model,
        }
        for p, vector in zip(passages, vectors, strict=True)
    ]
    _write_vectors(lance_dir, target_version, rows, settings)
    return len(rows)
