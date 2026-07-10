"""Enrichment view-model reader -- the shared TUI+CLI seam (lode-ay5.1, lode-0qc).

A pure read function (no Textual, no Typer) that assembles a note's full
enrichment view for display: :func:`enrichment_view` returns an
:class:`EnrichmentView` carrying the note's summary/tags/entities as
structured :class:`EnrichmentItem` values (each pairing a bare string with a
``stale`` flag), its inferred edges (each with
``to_id``/``reason``/``confidence``/``stale``), embed status, and a
three-valued :attr:`EnrichmentView.enrichment_state`. This is the ONE seam the
TUI inspector modal (lode-ay5.2) and the CLI parity guard (lode-ay5.3) both
consume, so the two surfaces cannot drift apart -- neither may re-derive the
stale-display policy or the state predicate below. Rendering, by contrast, is
deliberately NOT shared: this module hands back the ``stale`` bit as data, and
each consumer decides how to show it (the TUI styles it, the CLI prints
``" [stale]"``).

**Content** is built ENTIRELY on :mod:`lode.display` --
:func:`~lode.display.display_annotations` / :func:`~lode.display.
display_edges`, the shared stale-display policy (lode-npx.4) -- so
tombstones and hidden-assertive items are dropped and stale items are
flagged exactly the way every other consumer already sees them. Content is
note_id-scoped (spans every version in the chain) and is **never** suppressed
by :attr:`EnrichmentView.enrichment_state`; a re-enriching note legitimately
shows ``pending`` state alongside its stale last-known content.

**State** (``enrichment_state``) is keyed on the note's HEAD version instead,
per the pinned predicate (bd lode-ay5.1 notes, decided 2026-07-08; the
``"failed"`` bucket corrected 2026-07-08, bd lode-bvg):

- ``"pending"`` -- the head's ``target_version`` has a live (``pending``,
  ``running``, or ``failed``) ``type='enrich'`` job. ``worker.py`` writes
  ``status='failed'`` only in the else-branch of its max-attempts gate, so a
  ``'failed'`` job always has a retry coming -- it is pending work, not a
  terminal outcome.
- ``"failed"`` -- the head has a dead-lettered (``status='dead'``) ``enrich``
  job and NO live one, AND zero ``source='ai'`` annotation or edge rows exist
  for the head's ``source_version`` -- a dead-letter surfaced honestly rather
  than misread as enriched-empty.
- ``"ready"`` -- otherwise (the head has ``source='ai'`` rows, or there was
  never an enrich job for it at all).

**External-snapshot introspection** (:class:`ExternalView`, lode-8d2) is the
browse-time analogue of the above for a *mirrored* node: when one of a note's
edges points at a real ``externals`` row (a drawn-down web link, ``lode-
w0h.2``/``lode-w0h.3``), :attr:`EnrichmentEdge.external` carries that
external's current snapshot -- source URL (the edge's own ``to_id``),
``source_type``, the head ``snapshot_id``, ``fetched_at``, and a three-valued
``state``. This ticket's only dependencies are ``lode-ay5.1`` and ``lode-
w0h.2``, so ``state`` is scoped to what those two land: it does **not**
attempt the refresh-cadence or re-enrich-materiality signals the epic
description imagines (``lode-w0h.5``/``lode-w0h.6`` -- neither has landed,
and this module fabricates no field for data that does not exist yet):

- ``"withheld"`` -- ``externals.no_egress`` is set (``docs/externals.md``
  "No-egress tier": captured and locally retrievable, never sent to Claude).
  Checked first -- privacy trumps fetch outcome.
- ``"stale"`` -- the head snapshot's ``status`` is ``"tombstone"`` (the last
  fetch failed; there is no fresh mirrored content to trust,
  :func:`lode.externals.tombstone_body`).
- ``"un-refreshed"`` -- otherwise: an ``"ok"`` snapshot, not withheld. Named
  for what is actually true today rather than implying liveness that
  ``lode-w0h.6``'s refresh policy (TTL / on-access revalidation) alone would
  earn -- nothing currently re-fetches a source on a schedule, so every
  un-tombstoned, non-withheld external is exactly as fresh as its one
  ``fetched_at``, and no more.

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

#: The three states an :class:`ExternalView` can report -- see the module
#: docstring's "External-snapshot introspection" section for the exact
#: predicate (lode-8d2).
ExternalState = Literal["un-refreshed", "stale", "withheld"]

#: Live (in-flight or about-to-retry) job statuses -- schema.sql's
#: ``pending -> running -> done`` happy path, plus ``failed`` (DECIDED
#: 2026-07-08, bd lode-bvg): worker.py writes ``status='failed'`` ONLY in the
#: else-branch of the max-attempts gate, so a ``'failed'`` job always has a
#: retry coming -- it is pending work, not a terminal outcome.
_LIVE_JOB_STATUSES = ("pending", "running", "failed")

#: Terminal-bad job status -- ``dead``, the poison terminal (schema.sql "The
#: UI surfaces 'dead' rows as dead-letters"). With no live job and no AI
#: output, it means the last enrich attempt never produced content and no
#: retry remains.
_DEAD_JOB_STATUSES = ("dead",)


@dataclass(frozen=True, slots=True)
class ExternalView:
    """A mirrored external's current snapshot, as browse-time introspection shows it.

    The external analogue of a note's own enrichment (lode-8d2) -- assembled
    the same pure-read way (no Textual, no Typer), attached to whichever
    :class:`EnrichmentEdge` points at this external's ``external_id`` (its
    ``to_id``) rather than surfaced as a standalone top-level read, since
    browse only ever selects a *note* today (an external only becomes
    directly selectable per ``lode-w0h.8``, out of this ticket's scope). See
    the module docstring's "External-snapshot introspection" section for the
    exact ``state`` predicate.
    """

    external_id: str
    source_type: str
    snapshot_id: str
    fetched_at: str
    status: Literal["ok", "tombstone"]
    no_egress: bool
    state: ExternalState


@dataclass(frozen=True, slots=True)
class EnrichmentEdge:
    """One inferred (or user-curated) edge, as the view-model carries it.

    ``external`` is ``None`` for an edge to another note (or to a not-yet-
    drawn-down id); it carries the target's :class:`ExternalView` when
    ``to_id`` resolves to a real ``externals`` row (lode-8d2) -- e.g. the
    ``source='user'`` edge a pasted URL creates (``lode.drawdown``).
    """

    to_id: str
    reason: str | None
    confidence: float | None
    stale: bool
    external: ExternalView | None = None


@dataclass(frozen=True, slots=True)
class EnrichmentItem:
    """One visible tag/entity/summary payload, paired with its stale bit.

    Structured rather than a pre-rendered string (lode-0qc) so a consumer that
    needs the boolean -- e.g. the TUI modal styling a stale tag dim rather
    than printing a suffix -- doesn't have to string-sniff a baked-in
    ``" [stale]"`` marker. ``value`` is the bare annotation payload; rendering
    (including whether/how to mark staleness) is entirely the consumer's call.
    """

    value: str
    stale: bool


@dataclass(frozen=True, slots=True)
class EnrichmentView:
    """A note's full enrichment view, assembled for display (lode-ay5.1).

    ``summary`` is the note's one summary line -- the fresh row when one
    exists, else the last-known stale one (see :func:`_summary`), or ``None``
    when the note has no summary annotation at all. ``tags``/``entities`` are
    the :class:`EnrichmentItem` values of every visible annotation of that
    kind. Staleness is carried as data (``EnrichmentItem.stale``), not baked
    into the string, matching how ``edges`` already carries a structured
    ``stale: bool`` -- every field on this view-model exposes staleness the
    same way.
    """

    note_id: str
    enrichment_state: EnrichmentState
    summary: EnrichmentItem | None
    tags: list[EnrichmentItem]
    entities: list[EnrichmentItem]
    edges: list[EnrichmentEdge]
    embedded: bool
    passage_count: int


def _annotation_items(annotations: list[dict], kind: str) -> list[EnrichmentItem]:
    """Every visible annotation of ``kind``, as an :class:`EnrichmentItem`.

    ``payload`` is a bare string for ``tag``/``entity``/``summary`` rows (see
    :func:`lode.enrich._write_enrichment`); the ``stale`` flag was already
    decided by :func:`~lode.display.display_annotations` -- this only shapes
    it into the view-model's structured item, no re-deriving the policy.

    ``cli._annotation_values`` was a near-copy of this helper: it filtered by
    ``kind`` the same way but returned pre-rendered strings carrying a baked
    ``" [stale]"`` suffix (the seam's old shape, before lode-0qc). lode-ay5.3
    deleted that copy when it routed ``cli.show_`` through this view-model.
    """
    return [
        EnrichmentItem(value=str(a["payload"]), stale=a["stale"])
        for a in annotations
        if a["kind"] == kind
    ]


def _summary(annotations: list[dict]) -> EnrichmentItem | None:
    """The note's one summary line -- the fresh row when one exists.

    :func:`~lode.display.display_annotations` is ``note_id``-scoped and spans
    every version, and an AI summary orphaned by an edit stays *visible* (only
    ``source='user'`` orphans are curation tombstones). So an edited-then-
    re-enriched note carries TWO visible ``kind='summary'`` rows: the pre-edit
    one (orphaned, hence stale) and the head's fresh one. Taking the first
    would surface the pre-edit summary, because the rows arrive in rowid
    (insertion) order -- oldest first.

    Prefer a non-stale row; fall back to the last-known stale one so a
    re-enriching note still shows a summary, flagged, rather than nothing
    (the "show-flagged, never hide" stale-display policy). ``min`` on the
    ``stale`` flag is stable, so ties keep insertion order.

    ``cli.show_`` used to carry this defect verbatim -- it picked its summary
    with ``summaries[0]`` over the now-deleted ``cli._annotation_values(...)``,
    so an edited-then-re-enriched note printed the PRE-EDIT summary. lode-ay5.3
    fixed that by routing ``cli.show_`` through this seam; see
    ``tests/test_enrichment_view.py`` for the end-to-end reproduction.
    """
    return min(
        _annotation_items(annotations, "summary"),
        key=lambda item: item.stale,
        default=None,
    )


def enrichment_view(db_path: Path, note_id: str) -> EnrichmentView | None:
    """Return ``note_id``'s full enrichment view, or ``None`` if it doesn't exist.

    Opens its own short-lived connection (:func:`lode.storage.init_db`), same
    convention as :func:`lode.notes_read.list_notes` / :func:`~lode.
    notes_read.note_body` -- a plain top-level read, not tied to a connection
    a caller might hold. Prefer :func:`enrichment_view_conn` when you already
    hold an open connection (e.g. ``cli.show_``, lode-ay5.3) -- it avoids
    opening a second one.
    """
    conn = init_db(db_path)
    try:
        return enrichment_view_conn(conn, note_id)
    finally:
        conn.close()


def enrichment_view_conn(
    conn: sqlite3.Connection, note_id: str
) -> EnrichmentView | None:
    """Same as :func:`enrichment_view`, but reuses a connection you already hold.

    Promoted from a private helper (lode-ay5.1's technical review left it
    private -- "a public API with no caller and no test is speculative");
    ``cli.show_`` (lode-ay5.3) already holds an open ``conn`` and a resolved
    ``note_id`` when it needs a note's enrichment view, so re-opening a second
    connection here would be silent waste. This is that caller.
    """
    row = conn.execute(
        "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    head_version_id: str = row[0]

    annotations = display_annotations(conn, note_id)
    edges = display_edges(conn, note_id)

    tags = _annotation_items(annotations, "tag")
    entities = _annotation_items(annotations, "entity")
    view_edges = [
        EnrichmentEdge(
            to_id=edge["to_id"],
            reason=edge["reason"],
            confidence=edge["confidence"],
            stale=edge["stale"],
            external=_external_view(conn, edge["to_id"]),
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
        summary=_summary(annotations),
        tags=tags,
        entities=entities,
        edges=view_edges,
        embedded=passage_count > 0,
        passage_count=passage_count,
    )


def _external_view(conn: sqlite3.Connection, to_id: str) -> ExternalView | None:
    """``to_id``'s :class:`ExternalView`, or ``None`` if it isn't a real external.

    An edge's ``to_id`` is polymorphic (another note, an inferred-edge target
    string, or a drawn-down external's id) -- the only reliable way to tell
    them apart is to ask the ``externals`` table itself, so this queries by
    existence rather than guessing from ``to_id``'s shape (a web
    ``external_id`` *is* its canonical URL, ``lode.drawdown``, but a future
    connector's id need not look like one). Kept private, like
    ``enrichment_view``'s own former ``_enrichment_view`` (ay5.1's review:
    "a public API with no caller and no test is speculative") -- the one
    caller is :func:`enrichment_view_conn`, attaching this to every edge that
    resolves to an external so neither consumer needs a second read.
    """
    row = conn.execute(
        "SELECT e.source_type, e.no_egress, s.snapshot_id, s.fetched_at, s.status "
        "FROM externals e JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id "
        "WHERE e.external_id = ?",
        (to_id,),
    ).fetchone()
    if row is None:
        return None
    source_type, no_egress_raw, snapshot_id, fetched_at, status = row
    no_egress = bool(no_egress_raw)

    state: ExternalState
    if no_egress:
        state = "withheld"
    elif status == "tombstone":
        state = "stale"
    else:
        state = "un-refreshed"

    return ExternalView(
        external_id=to_id,
        source_type=source_type,
        snapshot_id=snapshot_id,
        fetched_at=fetched_at,
        status=status,
        no_egress=no_egress,
        state=state,
    )


def _has_enrich_job(
    conn: sqlite3.Connection, head_version_id: str, statuses: tuple[str, ...]
) -> bool:
    """Does the head version carry a ``type='enrich'`` job in any of ``statuses``?"""
    placeholders = ", ".join("?" * len(statuses))
    (found,) = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM jobs WHERE type = 'enrich' "
        f"AND target_version = ? AND status IN ({placeholders}))",
        (head_version_id, *statuses),
    ).fetchone()
    return bool(found)


def _has_ai_output(
    conn: sqlite3.Connection, note_id: str, head_version_id: str
) -> bool:
    """Did enrichment write any ``source='ai'`` row for the head's ``source_version``?

    Either table counts: a run can legitimately produce only inferred edges and
    no annotations (or vice versa), and either way it produced output.
    """
    (found,) = conn.execute(
        "SELECT EXISTS("
        "SELECT 1 FROM annotations "
        "WHERE target = ? AND source = 'ai' AND source_version = ? "
        "UNION ALL "
        "SELECT 1 FROM edges "
        "WHERE from_id = ? AND source = 'ai' AND source_version = ?"
        ")",
        (note_id, head_version_id, note_id, head_version_id),
    ).fetchone()
    return bool(found)


def _enrichment_state(
    conn: sqlite3.Connection, note_id: str, head_version_id: str
) -> EnrichmentState:
    """Apply the pinned three-state predicate (see module docstring) to the head."""
    if _has_enrich_job(conn, head_version_id, _LIVE_JOB_STATUSES):
        return "pending"
    if _has_enrich_job(
        conn, head_version_id, _DEAD_JOB_STATUSES
    ) and not _has_ai_output(conn, note_id, head_version_id):
        return "failed"
    return "ready"
