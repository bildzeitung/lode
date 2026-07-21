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
a delete tombstone, **or** an external's current ``head_snapshot_id`` whose
snapshot is not a tombstone (:func:`live_head_versions`); scoping each leg to
that set drops both stale prior-head passages and soft-deleted notes' content in
one move, and admits a mirrored external's current snapshot as a direct
candidate on its own content rather than only reachable via graph-expansion
from a citing note (``docs/externals.md`` "externals are directly retrievable").
A *stale* (non-head) snapshot stays excluded from both direct legs by
construction — only head pointers are read — the same way a superseded note
version is. :func:`graph_expand` (a note→external edge, lode-c4cd) also only
ever reaches an external's *current* head — edges resolve to ``external_id``,
not a specific ``snapshot_id`` — so a stale snapshot is unreachable by either
path; :func:`trust_rank` tiers it :data:`TrustTier.STALE_EXTERNAL` only in the
(currently untriggered) case of a direct hit whose target isn't the head.

The query vector for the dense leg is the caller's (the ``emb(q)`` node in the
pipeline is the embedder's concern, distinct from the search node), so
:func:`vector_search` takes an already-embedded ``query_vector`` — mirroring the
landed :meth:`VectorStore.search` signature and keeping this read side model-free.
"""

import re
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

import networkx as nx

from lode.config import Settings, model_cache_dir
from lode.lexical import LexicalHit, LexicalIndex
from lode.vectorstore import VectorHit, VectorStore

#: Word-token pattern for turning a natural-language question into FTS5 terms.
#: ``\w+`` runs (letters/digits/underscore) are the terms; everything else
#: (whitespace, the trailing ``?``, punctuation) is a separator. A token can never
#: contain a double-quote, so quoting each term in :func:`build_match_query` is safe.
_QUERY_TOKEN = re.compile(r"\w+")

#: The tombstone guard behind this module's live-head queries: a version is live
#: when its ``op`` is not ``'delete'``. ``versions.op`` is ``NOT NULL CHECK (op IN
#: ('create', 'update', 'delete'))``, so the test is total — no third state, no
#: ``NULL`` to fall through it. Both :func:`live_head_versions` and
#: :func:`graph_expand` interpolate this, so widening what counts as a delete (a
#: second tombstone op, say) is one edit here (module docstring, "Heads only").
#:
#: It is a raw SQL fragment, so it carries two preconditions on the enclosing
#: query: ``versions`` must be joined **aliased ``v``**, and joined **on
#: ``notes.head_version_id``**. This fragment excludes tombstones and nothing
#: else — the *head* half of "live head" is that join condition, not this string.
#: Retrieval-local by design, and deliberately not shared further: ``reconcile``
#: additionally guards on ``purged_at``, and ``notes_read`` / ``repository`` /
#: ``tui.edit`` scope their own queries with their own copies.
_LIVE_HEAD_PREDICATE = "v.op != 'delete'"

#: The external-side analogue of :data:`_LIVE_HEAD_PREDICATE`: a snapshot is
#: live when its ``status`` is not ``'tombstone'``. ``snapshots.status`` is
#: ``NOT NULL CHECK (status IN ('ok', 'tombstone'))``, so this test is total
#: too. A failed fetch must never become a directly-retrievable hit — the same
#: fail-closed rule :mod:`lode.externals` applies to the embed enqueue applies
#: here to the read side (a tombstone's body is a synthetic placeholder, not
#: real content, ``docs/design.md`` §2). It is a raw SQL fragment carrying the
#: same preconditions: ``snapshots`` must be joined **aliased ``s``**, joined
#: **on ``externals.head_snapshot_id``** — that join condition is the *head*
#: half of "live", this string only excludes tombstones.
_LIVE_SNAPSHOT_PREDICATE = "s.status != 'tombstone'"


def build_match_query(question: str) -> str:
    """Build an FTS5 ``MATCH`` expression from a natural-language ``question``.

    The lexical leg's ``query`` is an FTS5 ``MATCH`` expression, and building it
    from the user's question is the read side's job (``lode.lexical`` — "the
    retrieval pipeline owns building it from a user question"). The question's word
    tokens are extracted and **OR-ed** rather than left to FTS5's default AND: a
    real question's every word rarely co-occurs in one passage, so AND would almost
    always match nothing, while OR widens recall and BM25 still ranks a passage
    matching more terms higher. Each token is double-quoted so a stop-word that
    collides with an FTS5 operator (``and`` / ``or`` / ``not`` / ``near``) or a
    stray punctuation char is matched as a literal term, not parsed as syntax.

    Returns ``""`` when the question has no word tokens — the caller skips the
    lexical leg, since an empty ``MATCH`` is an FTS5 syntax error, not a match-none.
    """
    tokens = _QUERY_TOKEN.findall(question)
    return " OR ".join(f'"{token}"' for token in tokens)


def live_head_versions(conn: sqlite3.Connection) -> list[str]:
    """Return the ids that are a note's current head, or an external's current head.

    A *live head* is either a ``notes.head_version_id`` whose version's ``op`` is
    not a delete tombstone, or an ``externals.head_snapshot_id`` whose snapshot's
    ``status`` is not a tombstone — i.e. content that retrieval should surface.
    Non-head versions/snapshots (a note's prior superseded edits, an external's
    superseded snapshots) are excluded by construction (only head pointers are
    read); soft-deleted notes and tombstoned externals are excluded by their
    respective guards. This is the allow-list each leg's search is scoped to, so
    retrieval never returns a stale or tombstoned passage — and, since it now
    includes external heads, a mirrored snapshot is a direct lexical/vector
    candidate on its own content, not only reachable via graph-expansion from a
    citing note (``docs/externals.md`` "externals are directly retrievable").
    """
    rows = conn.execute(
        "SELECT n.head_version_id FROM notes n "
        "JOIN versions v ON v.version_id = n.head_version_id "
        f"WHERE {_LIVE_HEAD_PREDICATE} "
        "UNION "
        "SELECT e.head_snapshot_id FROM externals e "
        "JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id "
        f"WHERE {_LIVE_SNAPSHOT_PREDICATE}"
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


class CrossEncoder(Protocol):
    """Scores a batch of (query, passage) pairs by relevance, higher is better.

    The one seam between :func:`rerank` and the reranker model — the build-side
    twin of :class:`lode.embedding.Embedder`. Production uses
    :class:`FastEmbedCrossEncoder` (the pinned ``rerank_model`` on the shared
    ONNX runtime); tests pass a stub so the gate never downloads a model.
    Implementations return one score per document, in input order.
    """

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Return one relevance score per document for ``query``, in input order."""
        ...


class FastEmbedCrossEncoder:
    """Default :class:`CrossEncoder`: the pinned local ONNX reranker via ``fastembed``.

    Mirrors :class:`lode.embedding.FastEmbedEmbedder`: it constructs
    ``fastembed``'s ``TextCrossEncoder`` for ``settings.rerank_model``
    (``BAAI/bge-reranker-base`` — the loadable bge-family pick, lode-txh.6) lazily
    on first :meth:`rerank` call, so the model download/load (hundreds of MB) is
    deferred out of import and out of any path that never reranks. It runs on the
    **same ONNX runtime** as the embedder — no new stack, content stays on-box
    (``docs/stack.md`` "Reranker"). Weights are cached under
    :func:`lode.config.model_cache_dir` (``$LODE_HOME/models/``), same as the
    embedder, so the download survives a reboot (lode-gmo). The model +
    threshold ship untuned, revisited against the eval harness
    (``docs/decisions.md``).
    """

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.rerank_model
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(
                model_name=self._model_name, cache_dir=str(model_cache_dir())
            )
        return self._model

    def warm(self) -> None:
        """Force the weights download/load now, ahead of any rerank call.

        The public seam ``lode models pull`` (lode-6qh) warms the cache through,
        so the CLI does not depend on the private :meth:`_load`.
        """
        self._load()

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        model = self._load()
        # fastembed yields one (numpy) score per document, in input order; coerce
        # to plain float so the score carried on FusedHit stays a Python float
        # (mirrors FastEmbedEmbedder's .tolist()).
        return [float(score) for score in model.rerank(query, documents)]


def rerank(
    conn: sqlite3.Connection,
    query: str,
    hits: list[FusedHit],
    *,
    scorer: CrossEncoder | None = None,
    settings: Settings | None = None,
) -> list[FusedHit]:
    """Re-score the fused top-N with a local cross-encoder — a toggleable stage.

    The ``rerank(q, fused)`` step of the read pipeline (``docs/retrieval.md`` "The
    v1 retrieval pipeline"), slotted between fusion (:func:`reciprocal_rank_fusion`)
    and parent-expansion (:func:`expand_parents`). The **seam is permanent** — the
    pipeline always calls through this insertion point — while the **stage is
    toggleable** via ``Settings.rerank_enabled`` (default on, ``runtime`` knob,
    ``docs/configuration.md``). When the stage is off the call is **fully bypassed**:
    ``hits`` is returned unchanged, with no model loaded.

    When on, a local cross-encoder re-scores the fused top-N: the
    ``retrieval_top_k`` best-fused passages are paired with ``query`` and scored by
    ``scorer`` (default :class:`FastEmbedCrossEncoder`, the pinned reranker on the
    **same ONNX runtime** as the embedder — on-box, no egress). The returned
    :class:`FusedHit` list is sorted by that relevance score, best-first, and
    trimmed to ``rerank_keep_n`` (``docs/configuration.md``); each hit's ``score``
    **becomes the cross-encoder relevance** (replacing the upstream RRF score), so
    order and score agree for the downstream stages that carry it through.

    The cross-encoder needs each passage's text, so the ``passages`` rows are read
    here (the same regenerable cache :func:`expand_parents` reads). A hit whose
    passage row is gone cannot be scored (nor later cited) and is dropped — the
    same drop :func:`expand_parents` makes. Model/threshold tuning is deferred to
    the eval harness (``docs/decisions.md``); nothing is tuned here.
    """
    settings = settings or Settings()
    if not settings.rerank_enabled or not hits:
        return hits

    top = hits[: settings.retrieval_top_k]
    texts = _passage_texts(conn, [hit.passage_id for hit in top])
    scorable = [hit for hit in top if hit.passage_id in texts]
    if not scorable:
        return []

    scorer = scorer or FastEmbedCrossEncoder(settings)
    scores = scorer.rerank(query, [texts[hit.passage_id] for hit in scorable])
    ranked = sorted(
        zip(scorable, scores, strict=True), key=lambda pair: pair[1], reverse=True
    )
    return [
        FusedHit(hit.passage_id, hit.target_version, score)
        for hit, score in ranked[: settings.rerank_keep_n]
    ]


def _passage_texts(conn: sqlite3.Connection, passage_ids: list[str]) -> dict[str, str]:
    """Map each present ``passage_id`` to its passage text, for cross-encoder scoring.

    Reads the regenerable ``passages`` cache (``schema.sql``) the same way
    :func:`expand_parents` does. Ids with no row are simply absent from the map —
    the caller drops them, since a passage with no text can be neither scored nor
    cited.
    """
    if not passage_ids:
        return {}
    placeholders = ", ".join("?" for _ in passage_ids)
    rows = conn.execute(
        f"SELECT passage_id, text FROM passages WHERE passage_id IN ({placeholders})",
        passage_ids,
    ).fetchall()
    return {row[0]: row[1] for row in rows}


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

    ``edge_source`` is ``None`` for direct retrieval hits (the passage units the
    lexical/dense/rerank pipeline produces). :func:`graph_expand` sets it to
    ``'user'`` or ``'ai'`` for hits added via graph traversal; :func:`trust_rank`
    uses this to assign :data:`TrustTier.USER_ANNOTATION` or
    :data:`TrustTier.AI_EDGE` to those hits, bypassing the version-table lookup
    used for direct hits. Defaults to ``None`` so existing callers that never set
    it are unaffected.
    """

    passage_id: str
    target_version: str
    char_range: str
    passage_text: str
    parent_block: str
    score: float
    edge_source: str | None = None


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


def graph_expand(
    conn: sqlite3.Connection,
    hits: list[ExpandedHit],
    *,
    settings: Settings | None = None,
) -> list[ExpandedHit]:
    """Traverse edges from seed notes in-memory via networkx (GraphRAG stage).

    The ``graph_expand`` step of the read pipeline (``docs/retrieval.md``): for each
    seed note whose passages appear in ``hits``, traverse the in-memory knowledge
    graph — a networkx :class:`~networkx.DiGraph` built from the ``edges`` table
    (``schema.sql``) — up to ``Settings.drawdown_hop_limit`` hops
    (``docs/configuration.md``, "Draw-down hop limit", default 1). For each reached
    node that resolves to a live **note** or a live **external** (``docs/externals.md``
    "Retrieval uses an explicit trust gradient" — a note→external edge is exactly
    the case that gradient depends on), its current head passages (the note's
    ``head_version_id``, or the external's ``head_snapshot_id``) are appended as new
    :class:`ExpandedHit` entries with ``edge_source`` set to the edge type that led
    there (``'user'`` or ``'ai'`` — both are followed, no source filtering, same as
    note-to-note expansion). A reached id that resolves to neither (a true concept
    label, e.g. an AI-inferred edge to ``"python"``) is silently skipped — there is
    no content to expand to.

    **Externals always resolve to their current head.** Edges point at
    ``external_id``, not at a specific ``snapshot_id``, so a graph-reached external
    is always its *current* head snapshot — never a stale one (that only happens on
    the direct-hit path, ``docs/externals.md``). :func:`trust_rank` tiers a
    graph-reached external as :data:`TrustTier.CURRENT_EXTERNAL`, bypassing the
    edge-type-based tiering that still applies to a graph-reached **note**.

    **No-op when no edges exist.** If the ``edges`` table has no ``fresh`` rows,
    the input is returned unchanged — the expected state before enrichment infers
    note-to-note edges (lode-npx.1). A passage already present in ``hits`` as a
    direct retrieval hit is never duplicated; the higher-trust direct hit is kept and
    the graph-sourced copy is dropped.

    ``edge_source`` on the new hits feeds :func:`trust_rank`:

    - reached node is a **note**: ``'user'`` → :data:`TrustTier.USER_ANNOTATION`
      (tier 2); ``'ai'`` → :data:`TrustTier.AI_EDGE` (tier 5).
    - reached node is an **external**: always :data:`TrustTier.CURRENT_EXTERNAL`
      (tier 3), regardless of ``edge_source`` (see above).

    When multiple paths reach the same node, the most-trusted edge type wins
    (``'user'`` beats ``'ai'``). Seeds (notes already providing direct hits) are
    excluded from graph-expanded results; their passages are already in ``hits``.
    """
    if not hits:
        return hits

    settings = settings or Settings()
    max_hops = settings.drawdown_hop_limit
    if max_hops == 0:
        return hits

    # Load all fresh edges and build the in-memory DiGraph. When two edges share
    # the same (from_id, to_id), the more-trusted source ('user' beats 'ai') wins.
    edge_rows = conn.execute(
        "SELECT from_id, to_id, source FROM edges WHERE status = 'fresh'"
    ).fetchall()
    if not edge_rows:
        return hits  # no-op: no edges in the knowledge graph yet

    G: nx.DiGraph = nx.DiGraph()
    for from_id, to_id, source in edge_rows:
        if G.has_edge(from_id, to_id):
            if source == "user":
                G[from_id][to_id]["source"] = "user"
        else:
            G.add_edge(from_id, to_id, source=source)

    # Resolve seed passage target_versions to note_ids (direct hits only).
    seed_versions = [h.target_version for h in hits if h.edge_source is None]
    if not seed_versions:
        return hits

    placeholders = ", ".join("?" for _ in seed_versions)
    seed_note_ids: set[str] = {
        row[0]
        for row in conn.execute(
            f"SELECT note_id FROM versions WHERE version_id IN ({placeholders})",
            seed_versions,
        )
    }
    if not seed_note_ids:
        return hits

    # BFS from each seed note up to max_hops hops.
    # reached[node] = best edge_source ('user' beats 'ai').
    reached: dict[str, str] = {}
    visited: set[str] = set(seed_note_ids)
    frontier: set[str] = set(seed_note_ids)

    for _ in range(max_hops):
        next_frontier: set[str] = set()
        for node in frontier:
            if node not in G:
                continue
            for _, neighbor, data in G.out_edges(node, data=True):
                edge_src: str = data.get("source", "ai")
                existing = reached.get(neighbor)
                # Track most-trusted path to this neighbor.
                if existing is None or (existing == "ai" and edge_src == "user"):
                    reached[neighbor] = edge_src
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.add(neighbor)
        frontier = next_frontier
        if not frontier:
            break

    # Remove seeds — their passages are already in hits.
    for seed in seed_note_ids:
        reached.pop(seed, None)

    if not reached:
        return hits

    # Keep only reached node IDs that are actual live notes OR live externals in
    # the DB. A reached id matching neither (a true concept label) has no content
    # to expand to and is silently dropped.
    reached_ids = list(reached)
    placeholders = ", ".join("?" for _ in reached_ids)
    reached_notes: dict[str, str] = {
        row[0]: row[1]  # note_id -> head_version_id
        for row in conn.execute(
            "SELECT n.note_id, n.head_version_id "
            "FROM notes n "
            "JOIN versions v ON v.version_id = n.head_version_id "
            f"WHERE n.note_id IN ({placeholders}) "
            f"AND {_LIVE_HEAD_PREDICATE}",
            reached_ids,
        )
    }
    reached_externals: dict[str, str] = {
        row[0]: row[1]  # external_id -> head_snapshot_id
        for row in conn.execute(
            "SELECT e.external_id, e.head_snapshot_id "
            "FROM externals e "
            "JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id "
            f"WHERE e.external_id IN ({placeholders}) "
            f"AND {_LIVE_SNAPSHOT_PREDICATE}",
            reached_ids,
        )
    }
    if not reached_notes and not reached_externals:
        return hits

    # Fetch passages for the head versions of reached notes and the head
    # snapshots of reached externals in one query.
    target_ids = list(reached_notes.values()) + list(reached_externals.values())
    placeholders = ", ".join("?" for _ in target_ids)
    passage_rows = conn.execute(
        f"SELECT passage_id, target_version, char_range, text, parent_block "
        f"FROM passages WHERE target_version IN ({placeholders})",
        target_ids,
    ).fetchall()
    if not passage_rows:
        return hits

    # Reverse maps: head_version_id -> note_id, head_snapshot_id -> external_id
    # (for edge_source lookup — reached[] is keyed by the original graph node id,
    # which is a note_id or an external_id, not the resolved head target).
    version_to_note_id = {v: k for k, v in reached_notes.items()}
    snapshot_to_external_id = {v: k for k, v in reached_externals.items()}

    # Passage ids already in hits — never duplicated (direct hit wins).
    existing_passage_ids = {h.passage_id for h in hits}

    new_hits: list[ExpandedHit] = []
    for passage_id, target_version, char_range, text, parent_block in passage_rows:
        if passage_id in existing_passage_ids:
            continue  # already a direct retrieval hit; keep its higher-trust tier
        node_id = version_to_note_id.get(target_version) or snapshot_to_external_id.get(
            target_version
        )
        if node_id is None:
            continue
        new_hits.append(
            ExpandedHit(
                passage_id=passage_id,
                target_version=target_version,
                char_range=char_range,
                passage_text=text,
                parent_block=parent_block,
                score=0.0,
                edge_source=reached[node_id],
            )
        )

    return hits + new_hits


class TrustTier(IntEnum):
    """The documented trust gradient that orders the final Q&A context.

    ``docs/externals.md`` ("Retrieval uses an explicit trust gradient") and
    ``docs/retrieval.md`` (the ``trust_rank`` step): **your note > your annotation
    > current external snapshot > stale external snapshot > AI-inferred edge.** The
    user's own words are highest-trust; externals corroborate, they do not
    override. The integer value *is* that rank, so **lower sorts earlier** (higher
    trust) in the context handed to the Q&A LLM.

    :data:`OWNED_NOTE` and :data:`STALE_EXTERNAL` are direct-hit-only (lexical/dense/
    rerank pipeline). :data:`CURRENT_EXTERNAL` comes from either a direct hit on an
    external's current head snapshot, **or** a graph-expanded hit that reached an
    external (:func:`graph_expand`, lode-c4cd) — edges resolve to an external's
    *current* head by construction, so a graph-reached external is never stale.
    :data:`USER_ANNOTATION` and :data:`AI_EDGE` are graph-expanded-only and apply
    when the reached node is a **note**: a user-curated edge yields
    ``USER_ANNOTATION`` (tier 2), an AI-inferred edge yields ``AI_EDGE`` (tier 5).
    The integer values are stable — no renumbering was needed when graph_expand
    landed, nor when it grew to reach externals.
    """

    OWNED_NOTE = 1
    USER_ANNOTATION = 2
    CURRENT_EXTERNAL = 3
    STALE_EXTERNAL = 4
    AI_EDGE = 5


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One trust-ranked unit of Q&A context, with its citation carried through.

    ``tier`` is the unit's place on the trust gradient (:class:`TrustTier`) and
    orders the context. ``target_version`` is the polymorphic citation target
    (``passages.target_version`` — a note ``version_id`` for :data:`OWNED_NOTE`, an
    external ``snapshot_id`` otherwise; the tier discriminates which, so the
    downstream answer fills the matching field of ``answer.Support``), with
    ``char_range`` (the half-open span) and ``passage_text`` pinning the precise
    citation while ``parent_block`` gives the LLM the surrounding context
    (``docs/retrieval.md`` small-to-big). ``score`` is the upstream RRF ranking,
    carried so within a tier the better-ranked unit still leads.
    """

    tier: TrustTier
    passage_id: str
    target_version: str
    char_range: str
    passage_text: str
    parent_block: str
    score: float


@dataclass(frozen=True, slots=True)
class WithheldHit:
    """An E4 hit the trust ranker could not place on the gradient — surfaced, not dropped.

    The acceptance criterion is that the context builder **withholds nothing
    silently**: a hit whose ``target_version`` resolves to neither a note version
    nor a known external snapshot cannot be assigned a trust tier (nor reliably
    cited), so rather than silently omit it the ranker returns it here with the
    ``reason``, for the caller to log or report.
    """

    passage_id: str
    target_version: str
    reason: str


@dataclass(frozen=True, slots=True)
class TrustRankedContext:
    """The ``trust_rank`` output: ordered Q&A context plus everything withheld.

    ``context`` is the citation-carrying units ordered by the trust gradient
    (``docs/retrieval.md`` ``trust_rank``), best-trust first. ``withheld`` is every
    input hit that could not be placed on the gradient — empty in the normal case,
    non-empty exactly when something was dropped, so nothing leaves silently.
    """

    context: list[ContextItem]
    withheld: list[WithheldHit]


def trust_rank(conn: sqlite3.Connection, hits: list[ExpandedHit]) -> TrustRankedContext:
    """Order the expanded hits into Q&A context by the trust gradient.

    The final ``trust_rank`` step of the read pipeline (``docs/retrieval.md``): the
    expanded hits are reordered by the **trust gradient** (``docs/externals.md`` —
    your note > your annotation > current external snapshot > stale external snapshot
    > AI-inferred edge), carrying each hit's citation straight through to
    :class:`ContextItem`.

    **Direct hits** (``edge_source is None``) are classified by a lookup against
    ``versions`` and ``snapshots``:

    - present in ``versions`` → :data:`TrustTier.OWNED_NOTE` (tier 1);
    - present in ``snapshots``, current head → :data:`TrustTier.CURRENT_EXTERNAL` (tier 3);
    - present in ``snapshots``, not current head → :data:`TrustTier.STALE_EXTERNAL` (tier 4).

    **Graph-expanded hits** (``edge_source in {'user', 'ai'}`` — produced by
    :func:`graph_expand`) are classified by the **type of node reached**, not
    ``edge_source`` alone:

    - reached node is an **external** (``target_version`` is a snapshot id) →
      :data:`TrustTier.CURRENT_EXTERNAL` (tier 3), regardless of ``edge_source`` —
      a graph-reached external always resolves to its current head (edges point at
      ``external_id``, not a specific snapshot, ``docs/externals.md``), so
      :data:`TrustTier.STALE_EXTERNAL` is unreachable via graph expansion by
      construction; that tier stays direct-hit-only.
    - reached node is a **note** (``target_version`` is a version id) →
      ``edge_source == 'user'`` → :data:`TrustTier.USER_ANNOTATION` (tier 2);
      ``edge_source == 'ai'``  → :data:`TrustTier.AI_EDGE` (tier 5).

    The sort is stable, so within a tier the upstream best-first (RRF) order is
    preserved. A direct hit whose ``target_version`` matches neither ``versions``
    nor ``snapshots`` cannot be placed on the gradient (nor cited); rather than
    drop it silently it is returned in ``withheld`` (acceptance: **withholds
    nothing silently**). Graph-expanded hits always have a tier and are never
    withheld — :func:`graph_expand` only ever produces one for an id that
    resolved to a live note or a live external.
    """
    if not hits:
        return TrustRankedContext(context=[], withheld=[])

    # Look up every hit's target_version in the DB — both direct hits (to
    # classify owned-vs-current-vs-stale) and graph-expanded hits (to tell a
    # reached external apart from a reached note, since only the former forces
    # CURRENT_EXTERNAL regardless of edge_source).
    all_targets = {h.target_version for h in hits}

    target_list = list(all_targets)
    placeholders = ", ".join("?" for _ in target_list)

    owned: set[str] = {
        row[0]
        for row in conn.execute(
            f"SELECT version_id FROM versions WHERE version_id IN ({placeholders})",
            target_list,
        )
    }
    # snapshot_id -> is it its external's current head? (current vs stale)
    snapshots: dict[str, bool] = {
        row[0]: row[0] == row[1]
        for row in conn.execute(
            f"SELECT s.snapshot_id, e.head_snapshot_id "
            f"FROM snapshots s JOIN externals e ON e.external_id = s.external_id "
            f"WHERE s.snapshot_id IN ({placeholders})",
            target_list,
        )
    }

    context: list[ContextItem] = []
    withheld: list[WithheldHit] = []
    for hit in hits:
        if hit.edge_source is not None:
            # Graph-expanded hit: tier depends on the TYPE of node reached, not
            # edge_source alone. A reached external is always current (edges
            # resolve to the current head snapshot by construction) — tier it
            # CURRENT_EXTERNAL regardless of which edge type led there. Only a
            # reached note falls back to the edge-type-based tier.
            tier: TrustTier | None = (
                TrustTier.CURRENT_EXTERNAL
                if hit.target_version in snapshots
                else TrustTier.USER_ANNOTATION
                if hit.edge_source == "user"
                else TrustTier.AI_EDGE
            )
        else:
            tier = _classify(hit.target_version, owned, snapshots)

        if tier is None:
            withheld.append(
                WithheldHit(
                    passage_id=hit.passage_id,
                    target_version=hit.target_version,
                    reason="target_version is neither a note version nor a known "
                    "external snapshot; cannot place on the trust gradient",
                )
            )
            continue
        context.append(
            ContextItem(
                tier=tier,
                passage_id=hit.passage_id,
                target_version=hit.target_version,
                char_range=hit.char_range,
                passage_text=hit.passage_text,
                parent_block=hit.parent_block,
                score=hit.score,
            )
        )
    # Stable sort by tier: preserves the upstream best-first order within a tier.
    context.sort(key=lambda item: item.tier)
    return TrustRankedContext(context=context, withheld=withheld)


def _classify(
    target_version: str, owned: set[str], snapshots: dict[str, bool]
) -> TrustTier | None:
    """Place one ``target_version`` on the trust gradient, or ``None`` if it can't.

    Owned notes outrank externals (the documented gradient), so ``versions`` is
    checked first; an external is current or stale by whether it is its external's
    head snapshot. ``None`` means the target matched neither table.
    """
    if target_version in owned:
        return TrustTier.OWNED_NOTE
    if target_version in snapshots:
        return (
            TrustTier.CURRENT_EXTERNAL
            if snapshots[target_version]
            else TrustTier.STALE_EXTERNAL
        )
    return None


def _in_clause(column: str, values: Collection[str]) -> str:
    """A ``<column> IN ('a', 'b', ...)`` predicate over content-address hex values.

    Used to scope the LanceDB query to the live-head set. ``values`` are lowercase
    hex version ids (``lode.hashing``), so inlining them needs no escaping — the
    same trusted-value assumption the landed vector store documents for its
    ``where`` predicate.
    """
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"
