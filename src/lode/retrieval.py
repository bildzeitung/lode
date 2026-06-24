"""The E4 retrieval read side: the two passage-search legs + app-side RRF fusion.

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
the app-side RRF step (:func:`reciprocal_rank_fusion`, lode-72m.2) — Reciprocal
Rank Fusion app-side, which is why LanceDB's *own* native hybrid stays unused
(``docs/retrieval.md``, "Fusion is app-side RRF").

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
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class FusedHit:
    """One passage's combined standing across both legs: its RRF score, best-first.

    ``passage_id`` and ``target_version`` are carried straight from the leg hits;
    both legs rank the **same passage unit**, so a passage seen in both agrees on
    them. ``score`` is the Reciprocal-Rank-Fusion score — a sum of ``1 / (k + rank)``
    over the legs the passage appears in — so **larger is better** and the fused
    list sorts descending (the inverse of the legs' own raw metrics, where smaller
    bm25/distance is better; RRF consumes only the rank, not the absolute value).
    """

    passage_id: str
    target_version: str
    score: float


def reciprocal_rank_fusion(
    lexical: list[LexicalHit],
    vector: list[VectorHit],
    *,
    k: int = 60,
) -> list[FusedHit]:
    """Fuse the two already-ranked legs into one RRF-scored list, best-first.

    App-side Reciprocal Rank Fusion (``docs/retrieval.md``, "Fusion is app-side
    RRF"): each passage scores ``sum over legs of 1 / (k + rank)``, where ``rank``
    is its 1-based position in a leg's best-first list and ``k`` is the smoothing
    constant (``Settings.rrf_k``, default 60 — ``docs/configuration.md``). This
    reuses the landed legs' output (:func:`lexical_search`, :func:`vector_search`)
    and re-queries neither, which is why LanceDB's own native hybrid stays unused.

    A passage present in only **one** leg still scores from that leg alone and so
    still appears — e.g. a just-saved note matched lexically (FTS5 is synchronous)
    before its vector lands in the async cache. Higher score sorts first; ties keep
    first-seen order (a stable sort, lexical leg before dense).
    """
    scores: dict[str, float] = {}
    versions: dict[str, str] = {}
    for leg in (lexical, vector):
        for rank, hit in enumerate(leg, start=1):
            scores[hit.passage_id] = scores.get(hit.passage_id, 0.0) + 1.0 / (k + rank)
            versions[hit.passage_id] = hit.target_version
    fused = [FusedHit(pid, versions[pid], score) for pid, score in scores.items()]
    fused.sort(key=lambda hit: hit.score, reverse=True)
    return fused


@dataclass(frozen=True, slots=True)
class ExpandedHit:
    """A fused passage hit expanded for small-to-big retrieval.

    The **citation stays pinned to the precise passage/span** (``passage_id``,
    ``target_version``, ``char_range`` — the half-open char offsets so
    ``body[start:end]`` is the cited text — and ``passage_text``), while
    ``parent_block`` carries the **larger enclosing section** the passage was
    chunked from to give the Q&A LLM enough surrounding context to synthesize
    (``docs/retrieval.md``, "Small-to-big retrieval": match the small passage,
    expand to its parent block for context, cite the precise span). ``score`` is
    carried straight from the fused hit so the expansion preserves the upstream
    ranking.
    """

    passage_id: str
    target_version: str
    char_range: str
    passage_text: str
    parent_block: str
    score: float


def expand_parents(conn: sqlite3.Connection, hits: list[FusedHit]) -> list[ExpandedHit]:
    """Expand each fused passage hit to its parent block, best-first order kept.

    Small-to-big retrieval (``docs/retrieval.md``, ``expand_parents`` in the
    pipeline sketch): for each top passage hit, resolve its stored ``passages``
    row (``schema.sql``) to recover the precise span and the enclosing
    ``parent_block`` the chunker recorded (``lode.chunking``). The returned
    :class:`ExpandedHit` carries the larger parent block for the Q&A context
    window **while its citation stays pinned to the precise passage/span** —
    never the expanded block.

    Reuses the landed fusion output (:func:`reciprocal_rank_fusion`) and the
    chunker's ``passages`` table; it reimplements neither search nor chunking. A
    hit whose passage row is no longer present (the passages cache is
    regenerable, re-chunked per head — ``schema.sql``) is dropped, since it can
    be neither expanded nor cited. Input order (best-first) is preserved.
    """
    if not hits:
        return []
    placeholders = ", ".join("?" for _ in hits)
    rows = conn.execute(
        f"SELECT passage_id, char_range, text, parent_block FROM passages "
        f"WHERE passage_id IN ({placeholders})",
        [hit.passage_id for hit in hits],
    ).fetchall()
    by_id = {row[0]: row for row in rows}
    expanded: list[ExpandedHit] = []
    for hit in hits:
        row = by_id.get(hit.passage_id)
        if row is None:
            continue
        _, char_range, text, parent_block = row
        expanded.append(
            ExpandedHit(
                passage_id=hit.passage_id,
                target_version=hit.target_version,
                char_range=char_range,
                passage_text=text,
                parent_block=parent_block,
                score=hit.score,
            )
        )
    return expanded


def _in_clause(column: str, values: Collection[str]) -> str:
    """A ``<column> IN ('a', 'b', ...)`` predicate over content-address hex values.

    Used to scope the LanceDB query to the live-head set. ``values`` are lowercase
    hex version ids (``lode.hashing``), so inlining them needs no escaping — the
    same trusted-value assumption the landed vector store documents for its
    ``where`` predicate.
    """
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"
