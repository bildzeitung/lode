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

import re
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

from lode.config import Settings
from lode.lexical import LexicalHit, LexicalIndex
from lode.vectorstore import VectorHit, VectorStore

#: Word-token pattern for turning a natural-language question into FTS5 terms.
#: ``\w+`` runs (letters/digits/underscore) are the terms; everything else
#: (whitespace, the trailing ``?``, punctuation) is a separator. A token can never
#: contain a double-quote, so quoting each term in :func:`build_match_query` is safe.
_QUERY_TOKEN = re.compile(r"\w+")


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
    (``docs/stack.md`` "Reranker"). The model + threshold ship untuned, revisited
    against the eval harness (``docs/decisions.md``).
    """

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.rerank_model
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(model_name=self._model_name)
        return self._model

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


class TrustTier(IntEnum):
    """The documented trust gradient that orders the final Q&A context.

    ``docs/externals.md`` ("Retrieval uses an explicit trust gradient") and
    ``docs/retrieval.md`` (the ``trust_rank`` step): **your note > your annotation
    > current external snapshot > stale external snapshot > AI-inferred edge.** The
    user's own words are highest-trust; externals corroborate, they do not
    override. The integer value *is* that rank, so **lower sorts earlier** (higher
    trust) in the context handed to the Q&A LLM.

    Only :data:`OWNED_NOTE`, :data:`CURRENT_EXTERNAL`, and :data:`STALE_EXTERNAL`
    are reachable from an :class:`ExpandedHit` today — those are the passage units
    the read side produces. :data:`USER_ANNOTATION` and :data:`AI_EDGE` are the
    graph-expansion tiers (annotations / inferred edges, ``docs/externals.md``);
    they slot in at their documented rank once ``graph_expand`` lands and feeds
    this step, without renumbering the gradient.
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
    your note > current external snapshot > stale external snapshot), carrying each
    hit's citation (version/snapshot id + span) straight through to the
    :class:`ContextItem`. Each hit's polymorphic ``target_version`` is classified
    by a single lookup against ``versions`` and ``snapshots``:

    - present in ``versions`` → an owned note (:data:`TrustTier.OWNED_NOTE`);
    - present in ``snapshots`` → an external, **current** if it is its external's
      ``head_snapshot_id`` else **stale** (:data:`TrustTier.CURRENT_EXTERNAL` /
      :data:`TrustTier.STALE_EXTERNAL`).

    The sort is stable, so within a tier the upstream best-first (RRF) order is
    preserved. A hit whose ``target_version`` matches neither table cannot be
    placed on the gradient (nor cited); rather than drop it silently it is returned
    in ``withheld`` (acceptance: **withholds nothing silently**). The annotation /
    inferred-edge tiers attach when ``graph_expand`` feeds this step.
    """
    if not hits:
        return TrustRankedContext(context=[], withheld=[])

    targets = {hit.target_version for hit in hits}
    placeholders = ", ".join("?" for _ in targets)
    target_list = list(targets)

    owned = {
        row[0]
        for row in conn.execute(
            f"SELECT version_id FROM versions WHERE version_id IN ({placeholders})",
            target_list,
        )
    }
    # snapshot_id -> is it its external's current head? (current vs stale external)
    snapshots = {
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
