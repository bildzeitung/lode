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

:func:`embed` is the function the ``embed`` derive job calls (dispatched by
:func:`lode.worker._embed_handler`, lode-x6r.5). Capture enqueues the job via
:mod:`lode.jobs`; the async worker drains it — capture never embeds inline. It
is **idempotent** by construction: passage ids are deterministic (``chunk``
derives them from the content-addressed ``target_version``), passages are
upserted on their primary key, and the vector rows for a ``target_version`` are
replaced wholesale on each run — so re-embedding the same head version converges
to the same state instead of duplicating.

The embedder is injected (:class:`Embedder`) so the model is constructed lazily
and only in production: the default loads ``fastembed`` on first use, while tests
pass a stub and stay fast + offline (the real model download is exercised by the
opt-in smoke test ``tests/test_models_smoke.py``, lode-txh.6).

**Lazy load is thread-safe (lode-0wj.4).** :meth:`FastEmbedEmbedder._load` is
reached from a worker thread (``asyncio.to_thread``) by
:mod:`lode.tui.screens.capture`'s debounced related-notes pass, which now
shares *one* instance across the capture screen's lifetime (see that module's
docstring for why: a fresh instance per pass used to reload the ONNX model from
scratch every time, a measured ~1.5s event-loop stall). Reuse makes concurrent
access to a single instance real: ``@work(exclusive=True)`` cancels the
*asyncio* worker when a newer pass supersedes it, but the ``to_thread`` pool
thread it already launched keeps running to completion (a running thread can't
be force-killed), so while the first pass is still ~1.3s into the cold model
load, every further debounce fire starts another thread that also calls
``_load`` on the shared instance. Unguarded that is the classic lazy-singleton
race (each thread sees ``self._model is None`` and constructs its own model),
so the load is guarded by a lock.
"""

import sqlite3
import threading
from pathlib import Path
from typing import Protocol

from lode.chunking import Passage, chunk
from lode.config import Settings, model_cache_dir
from lode.redact import redact_before_index
from lode.vectorstore import VectorStore

#: Task prefixes nomic-embed-text-v1.5 expects: ``search_document:`` on the
#: documents being indexed, ``search_query:`` on the queries searched against them.
#: The model is **asymmetric** — it was trained with this query/document pair, so a
#: query embedded with the *document* prefix lands in a subtly wrong region and
#: softens dense recall; the query side must use its own prefix (``docs/stack.md``
#: local embedding, ``docs/retrieval.md`` the ``emb(q)`` node). Both are fixed
#: properties of the pinned ``embedding_model`` build constant — re-keying the
#: model updates them with it — so they ride the default embedder rather than a
#: separate config knob. Match the embedder usage proven in
#: ``tests/test_models_smoke.py`` (lode-txh.6).
_DOCUMENT_PREFIX = "search_document: "
_QUERY_PREFIX = "search_query: "


class Embedder(Protocol):
    """Embeds passage texts (indexing) or a query (retrieval) into fixed-dim vectors.

    The one seam between the embed leg / read side and the model: production uses
    :class:`FastEmbedEmbedder`; tests pass a stub so the gate never downloads a
    model. The two methods cover the **asymmetric** pair the pinned model expects —
    :meth:`embed_passages` for the documents being indexed, :meth:`embed_query` for
    the question searched against them (:data:`_DOCUMENT_PREFIX` /
    :data:`_QUERY_PREFIX`) — and both produce vectors of length
    ``Settings.embedding_vector_dim`` in the same space, so a query vector and a
    passage vector are directly comparable under the cosine ANN.
    """

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per text in ``texts``, in input order."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Return the query-side embedding of ``text`` (asymmetric query prefix)."""
        ...


class FastEmbedEmbedder:
    """Default :class:`Embedder`: the pinned local ONNX model via ``fastembed``.

    Constructs ``fastembed.TextEmbedding`` for ``settings.embedding_model`` lazily
    on first embed call (the model download/load is hundreds of MB, so it is
    deferred out of import and out of any code path that never embeds). Weights
    are cached under :func:`lode.config.model_cache_dir` (``$LODE_HOME/models/``)
    rather than ``fastembed``'s own ``tempfile.gettempdir()`` default, so the
    download survives a reboot (lode-gmo). Documents are prefixed with
    :data:`_DOCUMENT_PREFIX` and queries with :data:`_QUERY_PREFIX` as the
    asymmetric model requires.
    """

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.embedding_model
        self._model: object | None = None
        self._load_lock = threading.Lock()

    def _load(self) -> object:
        # Double-checked locking: the fast (already-loaded) path never takes
        # the lock, and only the very first caller pays construction cost --
        # this instance may be shared and called from concurrent threads
        # (lode-0wj.4), which a bare ``if self._model is None`` check would
        # race under.
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from fastembed import TextEmbedding

                    self._model = TextEmbedding(
                        model_name=self._model_name,
                        cache_dir=str(model_cache_dir()),
                    )
        return self._model

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        prefixed = [f"{_DOCUMENT_PREFIX}{text}" for text in texts]
        # fastembed yields one numpy array per input, in order.
        return [vector.tolist() for vector in model.embed(prefixed)]

    def embed_query(self, text: str) -> list[float]:
        # The asymmetric query side: same model, the ``search_query:`` prefix
        # instead of ``search_document:``. fastembed's ``embed`` is batch-only and
        # yields a generator, so embed the one query and take the single vector.
        model = self._load()
        (vector,) = model.embed([f"{_QUERY_PREFIX}{text}"])
        return vector.tolist()


def _version_body(conn: sqlite3.Connection, target_version: str) -> str:
    """Return the body of ``target_version``; raise ``KeyError`` if absent.

    ``target_version`` is polymorphic — a note ``version_id`` or an external
    ``snapshot_id`` (mirrors :func:`lode.cited_answer._resolve_target`'s
    versions-then-snapshots resolution, adapted here to take just the id: an
    embed job is enqueued on ``target_version`` alone, with no ``is_external``
    flag riding along, so this tries ``versions`` first and falls back to
    ``snapshots`` rather than being told which). Resolving a snapshot here is
    what lets an ``embed`` job enqueued for a mirrored external's snapshot_id
    (:func:`lode.externals.ingest_snapshot`) run to completion instead of
    raising.
    """
    row = conn.execute(
        "SELECT body FROM versions WHERE version_id = ?", (target_version,)
    ).fetchone()
    if row is not None:
        return row[0]
    row = conn.execute(
        "SELECT body FROM snapshots WHERE snapshot_id = ?", (target_version,)
    ).fetchone()
    if row is not None:
        return row[0]
    raise KeyError(target_version)


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
    that chunks to nothing). ``target_version`` is polymorphic — a note
    ``version_id`` or an external ``snapshot_id`` (:func:`_version_body`
    resolves either). Raises ``KeyError`` if ``target_version`` is unknown to
    both.

    **redact-before-index (lode-n60):** the body read from ``versions`` or
    ``snapshots`` is redacted (:func:`lode.redact.redact_before_index`) before
    it is chunked — the vector leg is driven by this async job off
    ``target_version`` alone (no body travels through the enqueue), so it
    independently strips secrets from what it reads rather than relying on a
    caller to have redacted first. ``versions.body``/``snapshots.body``
    themselves are left untouched (only ``purge`` clears the durable copy of a
    version; nothing currently purges a snapshot); only the text handed to
    :func:`chunk` here is redacted, so LanceDB and the ``passages`` rows this
    writes never carry the secret.
    """
    settings = settings or Settings()
    body = redact_before_index(_version_body(conn, target_version), settings)
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
    VectorStore(lance_dir, settings).replace_vectors(target_version, rows)
    return len(rows)


class EmbeddingCacheBackend:
    """The passage-vector engine behind the Repository cache seam (lode-1f9).

    Wraps the embed leg as a :class:`lode.repository.CacheBackend` so passage
    vectors are reached *through the Repository*, never by calling
    :func:`embed` / :class:`~lode.vectorstore.VectorStore` directly — the
    "access is through the repository interface" criterion that lode-x6r.3
    deferred. It plugs into :class:`~lode.repository.CompositeCache` alongside the
    FTS5 leg (lode-x6r.4) and the future graph, all on the same two-method seam.

    - :meth:`index` → :func:`embed`: chunk the head's body, embed each passage,
      and replace this version's vectors in LanceDB (idempotent re-embed).
    - :meth:`evict` → drop *this version's* vectors from LanceDB. A soft-delete
      tombstone has no passages of its own, so this is the symmetric "clear this
      version" of :meth:`index`; it deliberately does **not** sweep the note's
      prior content vectors — soft-delete is reversible (``recover`` repoints the
      head with no re-embed) and retrieval filters to the live head, so the
      note-wide hard cascade is purge's job (E8, lode-fk8.4), not the seam's.

    The connection, ``lance_dir`` and (optional) ``embedder`` the embed leg needs
    are held on the backend; the body the seam hands :meth:`index` is the version
    just committed, which :func:`embed` re-reads from ``conn`` to also persist the
    ``passages`` rows the vectors reference.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lance_dir: str | Path,
        embedder: Embedder | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._conn = conn
        self._lance_dir = lance_dir
        self._embedder = embedder
        self._settings = settings or Settings()

    def index(self, note_id: str, version_id: str, body: str) -> None:
        embed(
            self._conn,
            version_id,
            lance_dir=self._lance_dir,
            embedder=self._embedder,
            settings=self._settings,
        )

    def evict(self, note_id: str, version_id: str) -> None:
        VectorStore(self._lance_dir, self._settings).replace_vectors(version_id, [])
