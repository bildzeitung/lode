"""Passive connection surfacing while writing (lode-mkc.3, E11).

``docs/design.md`` §2 supporting feature 2: "When writing a new note, passively
show related past notes ('you wrote about this 3 weeks ago')." This module is
the pure, thread-safe read side the capture screen calls off the UI thread
(:mod:`lode.tui.screens.capture`); it owns no widget/App state and does not
itself decide *when* to run — that debounce lives in the screen.

**Reuses the landed E4 read pipeline, does not reimplement it**
(``docs/retrieval.md`` "The v1 retrieval pipeline"): the lexical (FTS5) and
dense (LanceDB) legs, app-side RRF fusion, small-to-big parent expansion, one
graph hop (:func:`lode.retrieval.graph_expand` — GraphRAG,
``docs/externals.md``), and the trust gradient
(:func:`lode.retrieval.trust_rank`) — the exact same building blocks
``lode.cli._retrieve`` assembles for ``lode ask``. The cross-encoder rerank
stage is deliberately skipped here: it loads a second on-box model and adds
latency this passive, every-few-keystrokes pass does not need to pay, and
skipping it does not touch the seam (``lode.retrieval.rerank`` stays wired for
the Q&A path).

**No AI call.** The dense leg's query embedding is a local ONNX model
(``fastembed``, on-box, ``docs/stack.md``) — no cloud round-trip, distinct from
the "no AI in the capture path" prohibition on autocomplete / enrichment /
chat-to-add (``docs/design.md`` "Explicitly NOT doing"), which this feature is
not: it is supporting feature 2, not capture-path AI.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lode.config import Settings, lance_dir
from lode.embedding import Embedder, FastEmbedEmbedder
from lode.retrieval import (
    ContextItem,
    TrustTier,
    build_match_query,
    expand_parents,
    graph_expand,
    lexical_search,
    reciprocal_rank_fusion,
    trust_rank,
    vector_search,
)
from lode.storage import init_db
from lode.vectorstore import VectorStore

#: Trust tiers that resolve back to an owned note (``versions`` table) rather
#: than an external snapshot (``lode.retrieval.TrustTier`` / ``trust_rank``) —
#: "related past notes" means notes the user wrote, not mirrored externals.
_NOTE_TIERS = frozenset(
    {TrustTier.OWNED_NOTE, TrustTier.USER_ANNOTATION, TrustTier.AI_EDGE}
)

#: Passage-text snippet length shown per related note (chars, incl. ellipsis).
_SNIPPET_CHARS = 80


@dataclass(frozen=True, slots=True)
class RelatedNote:
    """One past note surfaced while writing — "you wrote about this 3 weeks ago"."""

    note_id: str
    snippet: str
    age: str


def find_related_notes(
    db_path: Path,
    draft: str,
    *,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
) -> list[RelatedNote]:
    """Surface past notes related to the in-progress ``draft``, best-first.

    Opens its own short-lived connection (:func:`lode.storage.init_db`), same
    convention as :func:`lode.tui.capture.save_capture` — this runs off the
    UI thread (see the capture screen's worker), so it cannot share the app's
    connection across threads.

    Returns ``[]`` fast, opening no DB connection and running no embedder/
    LanceDB work at all, when ``settings.related_notes_enabled`` is ``False``
    (a plain user preference, not a lag fix — lode-0wj.2 confirmed the pass
    already runs off the UI thread) or when ``draft.strip()`` is shorter than
    ``settings.related_notes_min_chars`` — an empty or just-started buffer has
    no useful signal to search on. Otherwise runs the read pipeline described
    in the module docstring and reduces the trust-ranked context to at most
    ``settings.related_notes_limit``
    **distinct notes** (deduped, keeping each note's best-ranked passage as its
    snippet), each carrying a human "N weeks ago"-style age
    (:func:`humanize_age`).

    Raises nothing pipeline-specific to the caller beyond what the underlying
    DB/model calls raise — the capture screen is responsible for keeping this
    off the UI thread so a slow or failing pass never stalls capture; it is
    not swallowed here so a real bug is not silently hidden.
    """
    settings = settings or Settings()
    if not settings.related_notes_enabled:
        return []
    if len(draft.strip()) < settings.related_notes_min_chars:
        return []

    conn = init_db(db_path)
    try:
        match = build_match_query(draft)
        lexical = (
            lexical_search(conn, match, k=settings.retrieval_top_k) if match else []
        )

        embedder = embedder or FastEmbedEmbedder(settings)
        query_vector = embedder.embed_query(draft)
        store = VectorStore(lance_dir(db_path), settings)
        vector = vector_search(store, conn, query_vector, k=settings.retrieval_top_k)

        fused = reciprocal_rank_fusion(lexical, vector, k=settings.rrf_k)
        expanded = expand_parents(conn, fused[: settings.retrieval_top_k])
        graphed = graph_expand(conn, expanded, settings=settings)
        context = trust_rank(conn, graphed).context

        return _to_related_notes(conn, context, limit=settings.related_notes_limit)
    finally:
        conn.close()


def _to_related_notes(
    conn: sqlite3.Connection, context: list[ContextItem], *, limit: int
) -> list[RelatedNote]:
    """Reduce trust-ranked context to distinct owned notes, best-first, capped."""
    note_items = [item for item in context if item.tier in _NOTE_TIERS]
    if not note_items:
        return []

    target_versions = list({item.target_version for item in note_items})
    placeholders = ", ".join("?" for _ in target_versions)
    rows = conn.execute(
        f"SELECT version_id, note_id, created FROM versions "
        f"WHERE version_id IN ({placeholders})",
        target_versions,
    ).fetchall()
    by_version: dict[str, tuple[str, str]] = {row[0]: (row[1], row[2]) for row in rows}

    now = datetime.now(UTC)
    seen_notes: set[str] = set()
    related: list[RelatedNote] = []
    for item in note_items:
        resolved = by_version.get(item.target_version)
        if resolved is None:
            continue  # graph-expanded/withheld edge case: nothing to cite
        note_id, created = resolved
        if note_id in seen_notes:
            continue  # keep only the best-ranked passage per distinct note
        seen_notes.add(note_id)
        related.append(
            RelatedNote(
                note_id=note_id,
                snippet=_snippet(item.passage_text),
                age=humanize_age(created, now=now),
            )
        )
        if len(related) >= limit:
            break
    return related


def _snippet(text: str) -> str:
    """Collapse whitespace and truncate to :data:`_SNIPPET_CHARS`, ellipsized."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SNIPPET_CHARS:
        return collapsed
    return collapsed[: _SNIPPET_CHARS - 1].rstrip() + "…"


#: Bucket thresholds (seconds) -> (unit seconds, singular label), checked
#: smallest-to-largest; ``seconds < 60`` short-circuits to "just now" before
#: this table is consulted. Deliberately coarse ("3 weeks ago", not "21 days
#: ago") — this is a passive hint, not a precise timestamp.
_AGE_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (3600, 60, "minute"),
    (86400, 3600, "hour"),
    (7 * 86400, 86400, "day"),
    (30 * 86400, 7 * 86400, "week"),
    (365 * 86400, 30 * 86400, "month"),
)


def humanize_age(created: str, *, now: datetime | None = None) -> str:
    """Render an ISO-8601 UTC ``created`` timestamp as a coarse relative age.

    ``created`` is a ``versions.created`` / ``notes.created`` value
    (``schema.sql``, ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')``); Python's
    ``datetime.fromisoformat`` parses that ``Z``-suffixed form directly.
    Buckets from "just now" through weeks/months to years — precise enough for
    "you wrote about this 3 weeks ago", not a precision timestamp.
    """
    now = now or datetime.now(UTC)
    then = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    seconds = max((now - then).total_seconds(), 0.0)

    if seconds < 60:
        return "just now"
    for threshold, unit, label in _AGE_BUCKETS:
        if seconds < threshold:
            count = max(int(seconds // unit), 1)
            return f"{count} {label}{'s' if count != 1 else ''} ago"

    years = max(int(seconds // (365 * 86400)), 1)
    return f"{years} year{'s' if years != 1 else ''} ago"
