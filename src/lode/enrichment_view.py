"""Enrichment view-model reader -- the shared TUI+CLI seam (lode-ay5.1).

A pure read function (no Textual, no Typer) that assembles a note's full
enrichment view for display: :func:`enrichment_view` returns an
:class:`EnrichmentView` carrying the note's summary/tags/entities, its
inferred edges (each with ``to_id``/``reason``/``confidence``/``stale``),
embed status, and a three-valued :attr:`EnrichmentView.enrichment_state`.
This is the ONE seam the TUI inspector modal (lode-ay5.2) and the CLI parity
guard (lode-ay5.3) both consume, so the two surfaces cannot drift apart --
neither may re-derive the stale-display policy or the state predicate below.

**Content** is built ENTIRELY on :mod:`lode.display` --
:func:`~lode.display.display_annotations` / :func:`~lode.display.
display_edges`, the shared stale-display policy (lode-npx.4) -- so
tombstones and hidden-assertive items are dropped and stale items are
flagged exactly the way every other consumer already sees them. Content is
note_id-scoped (spans every version in the chain) and is **never** suppressed
by :attr:`EnrichmentView.enrichment_state`; a re-enriching note legitimately
shows ``pending`` state alongside its stale last-known content.

**State** (``enrichment_state``) is keyed on the note's HEAD version instead,
per the pinned predicate (bd lode-ay5.1 notes, decided 2026-07-08):

- ``"pending"`` -- the head's ``target_version`` has a live (``pending`` or
  ``running``) ``type='enrich'`` job.
- ``"failed"`` -- the head has a dead/failed ``enrich`` job (``status`` in
  ``failed``/``dead``) and NO live one, AND zero ``source='ai'`` annotation
  or edge rows exist for the head's ``source_version`` -- a dead-letter
  surfaced honestly rather than misread as enriched-empty.
- ``"ready"`` -- otherwise (the head has ``source='ai'`` rows, or there was
  never an enrich job for it at all).

No writes; this module only reads.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lode.display import display_annotations, display_edges
from lode.storage import init_db

#: The three enrichment states an :class:`EnrichmentView` can report --
#: see the module docstring for the exact predicate.
EnrichmentState = Literal["pending", "ready", "failed"]

#: Live (in-flight) job statuses -- schema.sql's ``pending -> running -> done``
#: happy path, still short of a terminal outcome.
_LIVE_JOB_STATUSES = ("pending", "running")

#: Terminal-bad job statuses -- ``failed`` (transient, retried) and ``dead``
#: (the poison terminal, schema.sql "The UI surfaces 'dead' rows as
#: dead-letters"). Either one, with no live job and no AI output, means the
#: last enrich attempt never produced content.
_DEAD_JOB_STATUSES = ("failed", "dead")


@dataclass(frozen=True, slots=True)
class EnrichmentEdge:
    """One inferred (or user-curated) edge, as the view-model carries it."""

    to_id: str
    reason: str | None
    confidence: float | None
    stale: bool


@dataclass(frozen=True, slots=True)
class EnrichmentView:
    """A note's full enrichment view, assembled for display (lode-ay5.1).

    ``summary`` is the first visible ``kind='summary'`` annotation payload (or
    ``None`` if un-enriched), matching ``cli.show_``'s existing pick of
    ``summaries[0]``. ``tags``/``entities`` are the payload strings of every
    visible annotation of that kind, each carrying a ``" [stale]"`` suffix
    when the stale-display policy flagged it -- the same rendering
    ``cli.show_`` already does, reused here so ay5.3's refactor changes
    nothing about wording that already matches.
    """

    note_id: str
    enrichment_state: EnrichmentState
    summary: str | None
    tags: list[str]
    entities: list[str]
    edges: list[EnrichmentEdge]
    embedded: bool
    passage_count: int


def _annotation_values(annotations: list[dict], kind: str) -> list[str]:
    """Extract every visible annotation payload of ``kind``, stale-flagged inline.

    ``payload`` is a bare string for ``tag``/``entity``/``summary`` rows (see
    :func:`lode.enrich._write_enrichment`); a ``" [stale]"`` suffix is
    appended per-item rather than hiding it, per the stale-display policy
    (show, but flag -- ``docs/storage.md`` "Stale-display policy"). This is a
    formatting convenience, not a second copy of the display policy itself --
    the ``visible``/``stale`` decision was already made by
    :func:`~lode.display.display_annotations`.
    """
    return [
        f"{a['payload']} [stale]" if a["stale"] else str(a["payload"])
        for a in annotations
        if a["kind"] == kind
    ]


def enrichment_view(db_path: Path, note_id: str) -> EnrichmentView | None:
    """Return ``note_id``'s full enrichment view, or ``None`` if it doesn't exist.

    Opens its own short-lived connection (:func:`lode.storage.init_db`), same
    convention as :func:`lode.notes_read.list_notes` / :func:`~lode.
    notes_read.note_body` -- a plain top-level read, not tied to a connection
    a caller might hold.
    """
    conn = init_db(db_path)
    try:
        return _enrichment_view(conn, note_id)
    finally:
        conn.close()


def _enrichment_view(conn: sqlite3.Connection, note_id: str) -> EnrichmentView | None:
    row = conn.execute(
        "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    head_version_id: str = row[0]

    annotations = display_annotations(conn, note_id)
    edges = display_edges(conn, note_id)

    summaries = _annotation_values(annotations, "summary")
    tags = _annotation_values(annotations, "tag")
    entities = _annotation_values(annotations, "entity")
    view_edges = [
        EnrichmentEdge(
            to_id=edge["to_id"],
            reason=edge["reason"],
            confidence=edge["confidence"],
            stale=edge["stale"],
        )
        for edge in edges
    ]

    (passage_count,) = conn.execute(
        "SELECT COUNT(*) FROM passages WHERE target_version = ?",
        (head_version_id,),
    ).fetchone()

    return EnrichmentView(
        note_id=note_id,
        enrichment_state=_enrichment_state(conn, note_id, head_version_id),
        summary=summaries[0] if summaries else None,
        tags=tags,
        entities=entities,
        edges=view_edges,
        embedded=passage_count > 0,
        passage_count=passage_count,
    )


def _enrichment_state(
    conn: sqlite3.Connection, note_id: str, head_version_id: str
) -> EnrichmentState:
    """Apply the pinned three-state predicate (see module docstring) to the head."""
    (has_live_job,) = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM jobs WHERE type = 'enrich' "
        "AND target_version = ? AND status IN (?, ?))",
        (head_version_id, *_LIVE_JOB_STATUSES),
    ).fetchone()
    if has_live_job:
        return "pending"

    (has_dead_job,) = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM jobs WHERE type = 'enrich' "
        "AND target_version = ? AND status IN (?, ?))",
        (head_version_id, *_DEAD_JOB_STATUSES),
    ).fetchone()
    if has_dead_job:
        (has_ai_output,) = conn.execute(
            "SELECT EXISTS("
            "SELECT 1 FROM annotations "
            "WHERE target = ? AND source = 'ai' AND source_version = ? "
            "UNION "
            "SELECT 1 FROM edges "
            "WHERE from_id = ? AND source = 'ai' AND source_version = ?"
            ")",
            (note_id, head_version_id, note_id, head_version_id),
        ).fetchone()
        if not has_ai_output:
            return "failed"

    return "ready"
