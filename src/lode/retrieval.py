"""The E4 retrieval read side: the two passage-search legs, heads only (lode-72m.1).

This is the **read side** of the hybrid retrieval pipeline (``docs/retrieval.md``,
"The v1 retrieval pipeline is hybrid, fused app-side"). It consumes the two
already-landed index legs and reimplements neither chunking, FTS, nor vector
storage:

- the **lexical leg** — :meth:`lode.lexical.LexicalIndex.search`, BM25 over
  ``passages_fts`` (synchronous, always fresh);
- the **dense leg** — :meth:`lode.vectorstore.VectorStore.search`, a cosine ANN
  query over the LanceDB passage vectors (the async cache).

Both rank the **same passage unit** (``docs/retrieval.md``, "Both retrieval legs
must rank the same unit"), so their two ranked lists fuse apples to apples under
the app-side RRF step — which is a **later ticket** (lode-72m.2), not this one.
This ticket is only the two legs each returning a ranked passage list for a query.

**Heads only.** Both indexes accumulate passages for *non-head* versions: an
update re-indexes the new head but deliberately leaves the prior head's rows in
place (soft history), and a soft-delete clears only the tombstone's own (empty)
rows, never the note's pre-delete content (``lode.lexical`` / ``lode.embedding``
both document this — the note-wide hard cascade is purge's job, E8). Retrieval
must therefore **filter to live heads** (``docs/retrieval.md``, "Index heads
only" — "a note edited 5x would return 5 near-duplicate hits and cite a stale
copy"). A *live head* is a note's current ``head_version_id`` whose version is not
a delete tombstone (:func:`live_head_versions`); scoping each leg to that set
drops both stale prior-head passages and soft-deleted notes' content in one move.

The query vector for the dense leg is the caller's (the ``emb(q)`` node in the
pipeline is the embedder's concern, distinct from the search node), so
:func:`vector_search` takes an already-embedded ``query_vector`` — mirroring the
landed :meth:`VectorStore.search` signature and keeping this read side model-free.
"""

import sqlite3
from collections.abc import Collection

from lode.lexical import LexicalHit, LexicalIndex
from lode.vectorstore import VectorHit, VectorStore


def live_head_versions(conn: sqlite3.Connection) -> list[str]:
    """Return the version ids that are a note's current, non-deleted head.

    A *live head* is a ``notes.head_version_id`` whose version's ``op`` is not a
    delete tombstone — i.e. content that retrieval should surface. Non-head
    versions (a note's prior, superseded edits) are excluded by construction
    (only head pointers are read), and soft-deleted notes are excluded by the
    ``op != 'delete'`` guard. This is the allow-list each leg's search is scoped
    to so retrieval never returns a stale or tombstoned passage.
    """
    rows = conn.execute(
        "SELECT n.head_version_id FROM notes n "
        "JOIN versions v ON v.version_id = n.head_version_id "
        "WHERE v.op != 'delete'"
    ).fetchall()
    return [row[0] for row in rows]


def lexical_search(conn: sqlite3.Connection, query: str, *, k: int) -> list[LexicalHit]:
    """Return the ``k`` best BM25 passage matches for ``query``, heads only.

    Delegates to :meth:`lode.lexical.LexicalIndex.search` scoped to the live-head
    set (:func:`live_head_versions`), so stale prior-head and soft-deleted
    passages never surface. ``query`` is an FTS5 ``MATCH`` expression. Results are
    best-first (most negative ``bm25()`` first); empty if nothing matches or the
    store has no live heads.
    """
    heads = live_head_versions(conn)
    if not heads:
        return []
    return LexicalIndex(conn).search(query, k=k, target_versions=heads)


def vector_search(
    store: VectorStore,
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    k: int,
) -> list[VectorHit]:
    """Return the ``k`` nearest passages to ``query_vector``, heads only.

    Delegates to :meth:`lode.vectorstore.VectorStore.search` with a
    ``target_version IN (...)`` metadata filter built from the live-head set
    (:func:`live_head_versions`), so the cosine ANN query is pre-scoped to live
    heads — stale prior-head and soft-deleted vectors never surface. The version
    ids are lowercase-hex content addresses (``lode.hashing``), safe to inline in
    the predicate. Results are nearest-first; empty if the store has no vectors or
    the database has no live heads.
    """
    heads = live_head_versions(conn)
    if not heads:
        return []
    where = _in_clause("target_version", heads)
    return store.search(query_vector, k=k, where=where)


def _in_clause(column: str, values: Collection[str]) -> str:
    """A ``<column> IN ('a', 'b', ...)`` predicate over content-address hex values.

    Used to scope the LanceDB query to the live-head set. ``values`` are lowercase
    hex version ids (``lode.hashing``), so inlining them needs no escaping — the
    same trusted-value assumption the landed vector store documents for its
    ``where`` predicate.
    """
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"
