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

import re
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass

from lode.chunking import Passage, chunk
from lode.config import Settings

#: The FTS5 virtual table holding one row per passage (``schema.sql``). Keyed for
#: replacement by the UNINDEXED ``target_version`` column; ``text`` is indexed.
_FTS_TABLE = "passages_fts"

#: Word tokens only -- everything else a user can type is dropped before it
#: reaches the FTS5 ``MATCH`` parser. See :func:`build_match_query`.
#: ``\w`` is unicode-aware under Python's default (non-``re.ASCII``) ``re``, so
#: this matches letters/digits/underscore in any script -- not just ASCII
#: (lode-8irr). SQLite FTS5 treats alphanumerics, ``_``, and any codepoint
#: >= 0x80 as bareword characters -- exactly the set ``\w`` admits -- so a
#: unicode token is still a safe bareword and cannot introduce an FTS5
#: metacharacter into the MATCH expression.
#:
#: NOT the same token class as :data:`lode.faithfulness._WORD` (``[^\W_]+``,
#: which excludes ``_``); that one splits claims/spans for the coupling check
#: and the ``docs/retrieval.md`` SPIKE note about "``_WORD``" is about *it*,
#: not this.
_WORD = re.compile(r"\w+")


def build_match_query(text: str, *, prefix: bool = False) -> str | None:
    """Build a safe FTS5 ``MATCH`` expression from free-typed ``text``.

    THE query builder for this repo's two free-text callers -- the eval
    scorer's submitted question (``lode.eval.harness``) and the browse
    screen's as-you-type quick-search box (lode-35nu.6, via
    :func:`lode.notes_read.search_notes`). One function *for these two*,
    because the
    *sanitization* is the security-relevant part and must not fork: stripping
    to ``\\w+`` tokens is what keeps a typed ``-``, ``"``, ``:``, ``^``
    or ``(`` from ever reaching the ``MATCH`` parser (which would otherwise
    raise mid-typing), and lowercasing is what stops a typed ``OR``/``AND``/
    ``NEAR`` from being read as an FTS5 operator -- those keywords are
    recognised only in uppercase. Tokens are ``OR``-ed rather than FTS5's
    default ``AND`` so recall stays honest: a passage sharing any salient
    keyword is a candidate and BM25 ranks them. The Q&A path has its own
    builder (:func:`lode.retrieval.build_match_query`) with a different
    contract -- quoted tokens, ``""`` rather than ``None`` on empty -- so
    "one function" is scoped to the two callers named above.

    ``prefix=True`` suffixes each token ``*``, so a still-incomplete word
    matches any passage containing a word that *starts* with it -- what an
    as-you-type box needs, since a bare FTS5 term matches whole words only and
    would show nothing until the user finishes typing one. The eval scorer
    keeps the default (whole-word) form: its questions are submitted complete.

    Returns ``None`` when ``text`` has no usable token (empty, whitespace, or
    all punctuation), so the caller skips the query rather than issue a
    ``MATCH`` against nothing.

    **Residual edge (lode-8irr):** Python's ``.lower()`` and SQLite's
    ``unicode61`` tokenizer fold case slightly differently. The handful of
    characters whose lowercase *expands* to a base letter plus a combining
    mark -- ``İ`` (U+0130) -> ``i`` + U+0307 being the practical one -- split
    into two tokens here (``i`` + ``stanbul``) where the index holds one
    (``istanbul``). ``prefix=True`` still matches; whole-word mode misses.
    Over-splitting is the fail-*broad* direction (tokens are OR-ed), so this
    never returns a wrong note, only sometimes none.
    """
    tokens = _WORD.findall(text.lower())
    if not tokens:
        return None
    suffix = "*" if prefix else ""
    return " OR ".join(f"{token}{suffix}" for token in tokens)


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

    def search(
        self,
        query: str,
        *,
        k: int,
        target_versions: Collection[str] | None = None,
    ) -> list[LexicalHit]:
        """Return the ``k`` best BM25 matches for ``query``, best match first.

        ``query`` is an FTS5 ``MATCH`` expression (bare keywords are ANDed) — the
        retrieval pipeline (E4) owns building it from a user question. Results are
        ordered by ``bm25()`` ascending (most negative = best). Returns at most
        ``k`` :class:`LexicalHit`, empty if nothing matches (a query against an
        empty index simply finds none).

        ``target_versions`` optionally scopes the match to a set of versions — the
        retrieval read side (lode-72m.1) passes the **live-head** set so stale
        prior-head and soft-deleted passages never surface (the index keeps them
        as soft history). ``None`` matches every version (the unfiltered index
        read); an empty collection matches none.
        """
        sql = (
            f"SELECT passage_id, target_version, bm25({_FTS_TABLE}) AS score "
            f"FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH ?"
        )
        params: list[object] = [query]
        if target_versions is not None:
            versions = list(target_versions)
            if not versions:
                return []
            placeholders = ", ".join("?" for _ in versions)
            sql += f" AND target_version IN ({placeholders})"
            params.extend(versions)
        sql += " ORDER BY score LIMIT ?"
        params.append(k)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            LexicalHit(passage_id=row[0], target_version=row[1], score=row[2])
            for row in rows
        ]


class LexicalCacheBackend:
    """The FTS5 lexical leg behind the Repository cache seam (lode-x6r.4, lode-xyb).

    Wraps :class:`LexicalIndex` as a :class:`lode.repository.CacheBackend` so the
    lexical index is reached *through the Repository*, on the same two-method seam
    as the vector leg — it plugs into :class:`~lode.repository.CompositeCache`
    alongside :class:`~lode.embedding.EmbeddingCacheBackend` (adding this leg is
    appending one engine at the wiring point, never a new seam — ``docs/storage.md``
    "The cache slot holds one engine that may be many"). It is **synchronous and
    model-free**, so the seam's ``index`` runs inline on save with no async stage.

    - :meth:`index` → chunk the head's body (deterministic, no model), persist
      the passage structure to the ``passages`` table (needed by
      :func:`lode.retrieval.expand_parents` to build Q&A context and for the
      embed-gap reconcile signal), and replace this version's per-passage FTS rows
      — so the note is keyword-findable and context-expandable the moment the save
      returns, before any async embedding runs (lode-xyb).
    - :meth:`evict` → clear *this version's* ``passages`` rows and FTS rows.  A
      soft-delete tombstone has no passages of its own, so this is the symmetric
      "clear this version" of :meth:`index`; like the vector leg it deliberately
      does **not** sweep the note's prior content rows — soft-delete is reversible
      (``recover`` re-indexes the repointed head) and retrieval filters to the live
      head, so the note-wide hard cascade is purge's job (E8, lode-fk8.4).
    """

    def __init__(
        self, conn: sqlite3.Connection, *, settings: Settings | None = None
    ) -> None:
        self._conn = conn
        self._index = LexicalIndex(conn)
        self._settings = settings or Settings()

    def index(self, note_id: str, version_id: str, body: str) -> None:
        passages = chunk(body, version_id, settings=self._settings)
        # Persist the passage structure to the regenerable ``passages`` table.
        # Chunking is model-free so this is safe on the synchronous capture path.
        # INSERT OR REPLACE is idempotent: the embed worker re-writes the same rows
        # later (same deterministic passage_ids) — no conflict, no duplication.
        if passages:
            with self._conn:
                self._conn.executemany(
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
        # Populate passages_fts for BM25 keyword search (delete-then-insert,
        # idempotent per target_version).
        self._index.replace_passages(version_id, passages)

    def evict(self, note_id: str, version_id: str) -> None:
        # Clear the passages rows for this version (tombstone has none; no-op in
        # practice, but symmetric with index and needed for cache correctness).
        with self._conn:
            self._conn.execute(
                "DELETE FROM passages WHERE target_version = ?", (version_id,)
            )
        self._index.replace_passages(version_id, [])
