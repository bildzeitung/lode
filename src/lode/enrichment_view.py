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

**Externally-inherited tags** (lode-f0m1) are surfaced too, distinguishable
from a note's own directly-scoped tags via :attr:`EnrichmentItem.inherited`.
``lode-35nu.7`` made a tag scoped to an *external* resolve to every note that
links that external via a fresh edge for the Tags-screen filter's purposes,
but left this view-model's ``tags`` strictly note_id-scoped (built from
:func:`~lode.display.display_annotations`, which only ever reads rows whose
``target`` is the note's own id) -- so a note could match an external-only
tag filter yet show no trace of that tag once opened. :func:`_inherited_tag_items`
closes that gap with the same resolution :func:`lode.notes_read.
_list_notes_with_all_tags` uses (a fresh note->external edge), appended to
``tags`` and flagged ``inherited=True`` rather than merged in
indistinguishably -- the whole point being to make which-is-which legible,
exactly the way ``stale`` is already carried as data and rendered
per-consumer, not baked into the value string.

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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from lode.display import display_annotations, display_edges
from lode.sql_ids import placeholders
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

    ``inherited`` (lode-f0m1) is ``True`` only for a *tag* item resolved
    through an external the note links via a fresh edge, rather than scoped
    directly to the note's own ``note_id`` -- see
    :func:`_inherited_tag_items`. It is always ``False`` for entities and
    summaries, which have no external-inheritance concept. Carried as a bare
    bool the same way ``stale`` is: this module hands back the bit as data,
    each consumer decides how to render it.
    """

    value: str
    stale: bool
    inherited: bool = False


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


def _inherited_tag_items(
    conn: sqlite3.Connection, note_id: str, direct_values: set[str]
) -> list[EnrichmentItem]:
    """Tags ``note_id`` inherits from an external it links via a fresh edge (lode-f0m1).

    Mirrors :func:`lode.notes_read._list_notes_with_all_tags`'s resolution --
    the same model this ticket's dispatch names as the reference: a tag
    annotation whose ``target`` is an *external's* id (``enrich.py`` writes
    tag annotations at ``target = owner_id``, an external_id for an
    external) resolves to every note that links that external via a
    ``status = 'fresh'`` edge, the identical filter :func:`lode.retrieval`
    builds its graph on. The join onto ``externals`` is load-bearing, not
    decoration -- ``edges.to_id`` is polymorphic (another note, an
    inferred-edge target string, or a drawn-down external's id,
    :func:`_external_view`) -- so only edges that actually resolve to a real
    external row qualify; a note->note edge must never make one note inherit
    the other's tags (``notes_read``'s own regression test for that case).

    SQL resolves only *which externals* this note inherits from; the tag rows
    themselves then go through :func:`~lode.display.display_annotations` +
    :func:`_annotation_items` -- the exact pair a note's own directly-scoped
    tags already use, just pointed at an external's id instead of the note's
    (``annotations.target`` is polymorphic, so the same read serves both).
    That keeps this module's "**Content** is built ENTIRELY on
    :mod:`lode.display`" contract intact: the stale/tombstone policy and the
    payload decoding stay in exactly one place, so a tombstoned
    (``source='user', status='orphaned'``) inherited tag is dropped and a
    non-fresh one is flagged ``stale`` because that is what the shared seam
    does, not because this function re-derives it.

    Deliberately NOT reusing ``enrichment_view_conn``'s already-built
    ``view_edges``, which looks like the same set: :func:`_external_view`
    additionally joins ``snapshots`` on ``head_snapshot_id`` and returns
    ``None`` when that snapshot is missing, so filtering on it would make the
    inspector STRICTER than the Tags-screen filter -- reintroducing this
    ticket's own asymmetry (a note matched on a tag it then doesn't show) for
    an external drawn down but not yet fetched. The predicate here is
    character-for-character ``notes_read``'s, and must stay that way.

    ``direct_values`` is the set of tag values already surfaced directly on
    the note (:func:`_annotation_items`); a value already shown directly is
    not repeated here as an inherited duplicate -- the note's own tag wins
    and stays a single, unambiguous entry. Where several *externals* carry
    the same tag value, the non-stale one wins (the ``sorted`` on ``stale``
    is stable, so equally-stale ties keep edge/insertion order).
    """
    external_ids = [
        row[0]
        for row in conn.execute(
            "SELECT e.to_id FROM edges e "
            "JOIN externals x ON x.external_id = e.to_id "
            "WHERE e.from_id = ? AND e.status = 'fresh'",
            (note_id,),
        )
    ]
    candidates = [
        item
        for external_id in external_ids
        for item in _annotation_items(display_annotations(conn, external_id), "tag")
    ]
    items: list[EnrichmentItem] = []
    seen = set(direct_values)
    for item in sorted(candidates, key=lambda candidate: candidate.stale):
        if item.value in seen:
            continue
        seen.add(item.value)
        items.append(replace(item, inherited=True))
    return items


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
    convention as :func:`lode.notes_read.list_notes` -- a plain top-level
    read, not tied to a connection a caller might hold. Prefer
    :func:`enrichment_view_conn` when you already hold an open connection
    (e.g. ``cli.show_``, lode-ay5.3) -- it avoids opening a second one.
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
    tags = tags + _inherited_tag_items(conn, note_id, {item.value for item in tags})
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
    (found,) = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM jobs WHERE type = 'enrich' "
        f"AND target_version = ? AND status IN ({placeholders(len(statuses))}))",
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


def raw_snapshot_payload(conn: sqlite3.Connection, snapshot_id: str) -> str | None:
    """Fetch one snapshot's raw HTML payload, or ``None`` if absent (nullable, schema.sql).

    Relocated from ``lode.cli`` (lode-35nu.9, "no bare SQL in the cli
    package"): ``lode dump-html``'s read.
    """
    row = conn.execute(
        "SELECT raw_payload FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    return row[0] if row else None


#: The live-head (notes UNION externals) scan `lode reenrich` force-enqueues
#: from -- shared with `stale_enrichment_heads` below so "status says clean"
#: and "reenrich has work" read the identical query, never a
#: separately-maintained approximation. Four positional `?` placeholders, two
#: per UNION branch: (model, provider) each -- see `stale_enrichment_heads`.
#: The provider comparison uses `IS NOT` rather than `!=` because it must stay
#: NULL-safe both ways: a stored `NULL` means "anthropic" by convention
#: (lode-568v.4's `provider_identity`), and the current provider is itself
#: `NULL` while the active provider is anthropic -- plain `!=` against a NULL
#: operand is never true in SQL, which would silently exempt every
#: anthropic-vs-anthropic row (the common case today) from ever comparing
#: equal, and non-equal, correctly (lode-568v.6).
#:
#: Relocated from ``lode.cli`` (lode-35nu.9, "no bare SQL in the cli
#: package") -- unchanged, still shared by ``lode status``'s hint
#: (:func:`lode.cli.status._enrichment_model_stale`) and ``lode reenrich``.
STALE_ENRICHMENT_LIVE_HEADS_SQL = """
    SELECT DISTINCT n.head_version_id
    FROM notes n
    JOIN versions v ON v.version_id = n.head_version_id
    WHERE n.head_version_id IS NOT NULL
      AND v.op != 'delete'
      AND v.purged_at IS NULL
      AND n.no_egress = 0
      AND EXISTS (
          SELECT 1 FROM annotations a
          WHERE a.source = 'ai'
            AND a.source_version = n.head_version_id
            AND a.model IS NOT NULL
            AND (a.model != ? OR a.provider IS NOT ?)
      )
    UNION
    SELECT DISTINCT e.head_snapshot_id
    FROM externals e
    JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id
    WHERE e.head_snapshot_id IS NOT NULL
      AND s.status != 'tombstone'
      AND e.no_egress = 0
      AND EXISTS (
          SELECT 1 FROM annotations a
          WHERE a.source = 'ai'
            AND a.source_version = e.head_snapshot_id
            AND a.model IS NOT NULL
            AND (a.model != ? OR a.provider IS NOT ?)
      )
"""


def stale_enrichment_heads(
    conn: sqlite3.Connection, enrichment_llm: str, current_provider: str | None
) -> list[str]:
    """Live head ids (notes UNION externals) whose recorded AI annotations disagree with `enrichment_llm` or `current_provider`.

    Relocated from ``lode.cli`` (lode-35nu.9, "no bare SQL in the cli
    package") -- unchanged. The exact scan ``lode reenrich`` force-enqueues
    from (docs/storage.md#re-enriching-the-corpus-deliberately-targeted-lode-14jr):
    a live head -- not soft-deleted/tombstoned, not purged, ``no_egress = 0``
    -- carrying at least one ``'ai'`` annotation whose ``model`` differs from
    `enrichment_llm`, OR whose ``provider`` differs from `current_provider`,
    right now. A head with no ``'ai'`` annotation at all is unenriched, not
    stale -- reconcile's ``enrich_gap`` step owns that case, not this one.

    `current_provider` follows the same ``None`` == "anthropic" convention
    :func:`lode.llm_provider.provider_identity` writes at enrichment time
    (lode-568v.4/lode-568v.6): pass its return value, not
    ``settings.llm_provider`` directly, so a stored ``NULL`` (an
    anthropic-produced row, pre-seam or post-) compares equal to a currently-
    anthropic config, and a provider switch -- same model/deployment string,
    different vendor -- is legible as stale even though ``model`` alone
    wouldn't catch it.

    Shared by ``lode status``'s hint
    (:func:`lode.cli.status._enrichment_model_stale`) and ``lode reenrich``
    itself so "status says clean" and "reenrich has work" cannot disagree --
    they are, structurally, the same read.
    """
    return [
        row[0]
        for row in conn.execute(
            STALE_ENRICHMENT_LIVE_HEADS_SQL,
            (
                enrichment_llm,
                current_provider,
                enrichment_llm,
                current_provider,
            ),
        ).fetchall()
    ]
