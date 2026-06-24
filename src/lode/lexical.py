"""SQLite FTS5 lexical index over passages — the synchronous lexical leg (lode-x6r.4).

This is the **lexical leg** of E3 local indexing, and the one leg that runs
**synchronously on the capture path**: it is *model-free* (no embedder, no
network), so unlike the vector leg it can index a just-saved note inline without
stalling capture, leaving the note keyword-findable the instant the save returns
— before any async embedding lands (``docs/design.md`` save path,
``docs/retrieval.md`` "FTS5 is the synchronous index").

It ranks the **same passage unit** as the vector leg (``docs/retrieval.md`` "FTS5
indexes passages too"), so the two legs fuse apples to apples under app-side RRF.
Chunking is the landed :func:`lode.chunking.chunk` (imported, never reimplemented);
passage ids are deterministic (derived from the content-addressed head version),
so re-indexing the same head converges instead of duplicating.

The index lives in the **SQLite container** next to ``versions`` (``docs/stack.md``
"FTS5 sits next to versions"), not in LanceDB — LanceDB carries vectors only and
its native hybrid stays unused; the lexical leg is here and fusion is app-side
(``docs/retrieval.md`` "Fusion is app-side RRF"). The ``passages_fts`` virtual
table is part of the data shape (``schema.sql``), created at :func:`lode.storage.init_db`.

Two pieces:

- :class:`LexicalIndex` — the **store**, the one place the FTS5 table is touched:
  a write side (:meth:`replace_passages`, idempotent per head version) and a read
  side (:meth:`search`, BM25-ranked).
- :class:`LexicalCacheBackend` — the :class:`lode.repository.CacheBackend` adapter
  that plugs the store into :class:`~lode.repository.CompositeCache` on the same
  two-method seam as :class:`~lode.embedding.EmbeddingCacheBackend`.
"""

import sqlite3
from dataclasses import dataclass

from lode.chunking import Passage, chunk
from lode.config import Settings

#: The FTS5 virtual table holding one row per passage (``schema.sql``). Keyed for
#: replacement by the UNINDEXED ``target_version`` column; ``text`` is indexed.
_FTS_TABLE = "passages_fts"


@dataclass(frozen=True, slots=True)
class LexicalHit:
    """One BM25 match: the passage it points at and its relevance score.

    ``passage_id`` joins back to the SQLite ``passages`` row for the passage text
    and span (the read side stores only what identifies a match). ``target_version``
    is the head the passage belongs to. ``score`` is SQLite's ``bm25()`` — **more
    negative is a better match**, so results come back ascending; the retrieval
    pipeline consumes the resulting rank, not the absolute value.
    """

    passage_id: str
    target_version: str
    score: float


class LexicalIndex:
    """The FTS5 passage store — the one place ``passages_fts`` is touched.

    Bound to an open SQLite connection (the same container that holds the
    irreplaceable rows). The same instance serves the write side
    (:meth:`replace_passages`) and the read side (:meth:`search`); the virtual
    table is created by :func:`lode.storage.init_db` from ``schema.sql``, so this
    store only reads and writes it.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def replace_passages(self, target_version: str, passages: list[Passage]) -> None:
        """Replace ``target_version``'s FTS rows with ``passages`` (idempotent re-index).

        Deletes any rows already keyed to ``target_version``, then inserts one row
        per passage. The delete-then-insert is what makes re-indexing the same
        content-addressed head converge instead of accumulate; an empty
        ``passages`` list just clears the version (the evict path). Runs in one
        transaction so a head's rows are never half-written.
        """
        with self._conn:
            self._conn.execute(
                f"DELETE FROM {_FTS_TABLE} WHERE target_version = ?",
                (target_version,),
            )
            if passages:
                self._conn.executemany(
                    f"INSERT INTO {_FTS_TABLE} (passage_id, target_version, text) "
                    "VALUES (?, ?, ?)",
                    [(p.passage_id, p.target_version, p.text) for p in passages],
                )

    def search(self, query: str, *, k: int) -> list[LexicalHit]:
        """Return the ``k`` best BM25 matches for ``query``, best match first.

        ``query`` is an FTS5 ``MATCH`` expression (bare keywords are ANDed) — the
        retrieval pipeline (E4) owns building it from a user question. Results are
        ordered by ``bm25()`` ascending (most negative = best). Returns at most
        ``k`` :class:`LexicalHit`, empty if nothing matches (a query against an
        empty index simply finds none).
        """
        rows = self._conn.execute(
            f"SELECT passage_id, target_version, bm25({_FTS_TABLE}) AS score "
            f"FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH ? "
            "ORDER BY score LIMIT ?",
            (query, k),
        ).fetchall()
        return [
            LexicalHit(passage_id=row[0], target_version=row[1], score=row[2])
            for row in rows
        ]


class LexicalCacheBackend:
    """The FTS5 lexical leg behind the Repository cache seam (lode-x6r.4).

    Wraps :class:`LexicalIndex` as a :class:`lode.repository.CacheBackend` so the
    lexical index is reached *through the Repository*, on the same two-method seam
    as the vector leg — it plugs into :class:`~lode.repository.CompositeCache`
    alongside :class:`~lode.embedding.EmbeddingCacheBackend` (adding this leg is
    appending one engine at the wiring point, never a new seam — ``docs/storage.md``
    "The cache slot holds one engine that may be many"). It is **synchronous and
    model-free**, so the seam's ``index`` runs inline on save with no async stage.

    - :meth:`index` → chunk the head's body (deterministic, no model) and replace
      this version's per-passage FTS rows, so the note is keyword-findable the
      moment the save returns.
    - :meth:`evict` → drop *this version's* FTS rows (replace with none). A
      soft-delete tombstone has no passages of its own, so this is the symmetric
      "clear this version" of :meth:`index`; like the vector leg it deliberately
      does **not** sweep the note's prior content rows — soft-delete is reversible
      (``recover`` re-indexes the repointed head) and retrieval filters to the live
      head, so the note-wide hard cascade is purge's job (E8, lode-fk8.4).
    """

    def __init__(
        self, conn: sqlite3.Connection, *, settings: Settings | None = None
    ) -> None:
        self._index = LexicalIndex(conn)
        self._settings = settings or Settings()

    def index(self, note_id: str, version_id: str, body: str) -> None:
        passages = chunk(body, version_id, settings=self._settings)
        self._index.replace_passages(version_id, passages)

    def evict(self, note_id: str, version_id: str) -> None:
        self._index.replace_passages(version_id, [])
