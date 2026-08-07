"""Ask-path wiring + rendering for the TUI's ask screen (lode-mkc.2).

Wires the exact same seams ``lode ask`` drives -- the read pipeline
(:func:`lode.cli._retrieve`) to build a trust-ranked context, then the cited
Q&A loop (:func:`lode.cited_answer.ask`, which synthesizes structured claims
and runs the faithfulness gate **before display**) -- rather than
re-implementing retrieval or the gate. It reuses ``cli._retrieve`` verbatim
(the same seam ``tests/test_cli.py`` reaches into directly) instead of
duplicating the read pipeline's composition a third time (``lode.eval.harness``
already keeps its own deliberately-narrower copy for deterministic scoring).

What this module adds is what the terminal's ``lode ask`` doesn't need: each
surviving citation's **as-of** provenance, resolved from the store --
``docs/design.md`` ("Retrieval always points back to the source note, 'as of'
a known version") and ``docs/externals.md`` ("as of ``fetched_at``"). A note
citation's as-of is its version's write time (``versions.created``); an
external citation's is its snapshot's fetch time (``snapshots.fetched_at``).

:func:`run_ask` is the screen's only entry point -- pure I/O (DB + the Q&A
send), no widget/App state, so it is unit-testable without spinning up a
Textual app, exactly like :mod:`lode.tui.services.capture`. :func:`render_ask_result`
turns its output into the screen's display text: cited claims each followed
by their citation + as-of line, withheld markers, and the abstention line --
the ticket's acceptance surface.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from lode.config import Settings, lance_dir
from lode.faithfulness import locate_span, normalize_whitespace
from lode.notes_read import first_line
from lode.storage import init_db

if TYPE_CHECKING:
    from lode.answer import Support
    from lode.cited_answer import CitedAnswer

#: The honest failure mode's display line -- the abstention wording from
#: ``docs/retrieval.md`` ("the system says 'your notes don't answer this'").
#: The CLI's own abstention line (``lode.cli._ABSTAIN_LINE``) reads differently
#: and is command-private; each surface owns its phrasing rather than sharing
#: one literal.
ABSTAIN_LINE = "Your notes don't answer this."

#: Pipeline stage names, in the order :func:`run_ask` reaches them -- passed
#: verbatim to an ``on_stage`` callback (lode-35nu.5). ``gate`` covers both
#: the faithfulness gate and its immediate predecessor, synthesis
#: (:func:`lode.cited_answer.ask` runs synthesize-then-gate as one call, so
#: there is no seam between them to report a stage change at) -- it fires
#: once that call returns, i.e. "gate complete".
STAGE_RETRIEVING = "retrieving"
STAGE_SYNTHESIZING = "synthesizing"
STAGE_GATE = "gate complete"

#: A callback taking the current stage name -- ``None`` (the default) means
#: no caller wants progress reporting. Kept Textual-free, per the module
#: docstring's own constraint: the caller (the TUI screen) supplies whatever
#: it needs to marshal this onto its own thread; this module never imports
#: Textual.
OnStage = Callable[[str], None]


def _no_stage(stage: str) -> None:
    """Stand in for an omitted ``on_stage`` so :func:`run_ask` reports
    unconditionally, instead of repeating a ``None`` check per stage."""


@dataclass(frozen=True)
class CitationIdentity:
    """Resolved note/external identity for one citation target (lode-35nu.1).

    Exactly one of ``note_id``/``external_id`` is set, mirroring
    :class:`~lode.answer.Support`'s own ``version_id``/``snapshot_id`` split.
    ``title`` is :func:`lode.notes_read.first_line` of the *cited*
    version/snapshot's body -- taken from the cited version even when that
    version is superseded, so an old citation shows the title it had rather
    than the current head's. ``is_head`` is True when the cited
    version/snapshot is still the note/external's current head.
    """

    title: str
    is_head: bool
    note_id: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class AskResult:
    """A gated cited answer plus each citation's as-of provenance and identity.

    ``as_of`` maps a cited ``target_id`` (a note ``version_id`` or external
    ``snapshot_id``) to its resolved as-of timestamp, or ``None`` when the
    store had nothing to resolve (practically unreachable -- the faithfulness
    gate already verified the span against the stored body -- but handled
    rather than assumed away). ``identities`` maps the same ``target_id`` to
    its resolved :class:`CitationIdentity` (lode-35nu.1) -- absent, not
    ``None``, for a target the store had nothing to resolve, so a caller's
    ``.get(target_id)`` returning ``None`` means exactly "unresolvable",
    same convention as ``as_of``. ``bodies`` maps the same ``target_id`` to
    the cited version/snapshot's full body text, used only to render
    surrounding context around a citation's ``quoted_span`` (lode-35nu.3);
    same absent-means-unresolvable convention as ``identities``. All three
    come from one batched pass (:func:`_resolve_citations`).
    """

    answer: CitedAnswer
    as_of: dict[str, str | None] = field(default_factory=dict)
    identities: dict[str, CitationIdentity] = field(default_factory=dict)
    bodies: dict[str, str] = field(default_factory=dict)


def run_ask(
    db_path: Path,
    question: str,
    *,
    think_harder: bool = False,
    settings: Settings | None = None,
    on_stage: OnStage | None = None,
) -> AskResult:
    """Run the cited Q&A loop for ``question`` and resolve citation provenance.

    Drives ``lode ask``'s own pipeline start to finish: retrieve
    (:func:`lode.cli._retrieve`) -> synthesize + gate
    (:func:`lode.cited_answer.ask`) -> resolve as-of provenance for each
    surviving citation. Raises :class:`lode.auth.AuthError` on unresolved
    Anthropic credentials, same as the CLI -- the screen catches it and
    notifies rather than crashing.

    ``on_stage``, when given, is called synchronously (on this function's own
    calling thread -- this module does no threading of its own) once per
    stage transition, in order: :data:`STAGE_RETRIEVING` before retrieval
    starts, :data:`STAGE_SYNTHESIZING` before the synthesize+gate call, and
    :data:`STAGE_GATE` once that call returns. Optional so this stays a plain
    function call for every existing caller/test; a caller wanting
    in-flight UI feedback (the TUI's ask screen) supplies one that marshals
    onto whatever thread it needs.

    Imports the retrieval/Q&A stack here, not at module scope: ``cli._retrieve``
    pulls in the vector stack (pyarrow) and ``cited_answer`` pulls in the
    Anthropic SDK, neither of which the capture path -- or merely importing
    this module to register the ask screen in ``LodeApp.SCREENS`` -- may load.
    """
    from lode import cited_answer
    from lode.cli import _retrieve

    settings = settings or Settings()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    try:
        report = on_stage or _no_stage
        report(STAGE_RETRIEVING)
        context = _retrieve(
            conn, question, lance_dir=lance_dir(db_path), settings=settings
        )
        report(STAGE_SYNTHESIZING)
        answer = cited_answer.ask(
            conn, question, context, think_harder=think_harder, settings=settings
        )
        report(STAGE_GATE)
        supports = [support for claim in answer.claims for support in claim.support]
        as_of, identities, bodies = _resolve_citations(conn, supports)
        return AskResult(
            answer=answer, as_of=as_of, identities=identities, bodies=bodies
        )
    finally:
        conn.close()


def _resolve_citations(
    conn: sqlite3.Connection, supports: list[Support]
) -> tuple[dict[str, str | None], dict[str, CitationIdentity], dict[str, str]]:
    """Resolve as-of provenance, identity, and body for every cited target, batched (lode-35nu.1).

    Two queries total -- one ``IN (...)`` over every distinct cited
    ``version_id``, one over every distinct cited ``snapshot_id`` -- so a
    multi-claim answer costs a fixed two round-trips regardless of citation
    count (the ticket's "a single batched query" acceptance line). The as-of
    stamp and the full body both ride along on the same rows the identity
    comes from (a note version is stamped at write time, ``versions.created``;
    an external snapshot at fetch time, ``snapshots.fetched_at``), so neither
    costs an extra query. The body is kept only so the ask screen can render
    surrounding context around a citation's ``quoted_span`` (lode-35nu.3).

    Returns ``(as_of, identities, bodies)``, all keyed by
    :attr:`~lode.answer.Support.target_id`. Every cited target is a key in
    ``as_of``, mapping to ``None`` when the store had nothing to resolve; such
    a target is simply absent from ``identities`` and ``bodies``. Unresolvable
    is practically unreachable -- the faithfulness gate already verified the
    span against the stored body -- but handled rather than assumed away.
    """
    identities: dict[str, CitationIdentity] = {}
    bodies: dict[str, str] = {}
    as_of: dict[str, str | None] = {}

    version_ids = tuple({s.version_id for s in supports if s.version_id is not None})
    if version_ids:
        placeholders = ",".join("?" for _ in version_ids)
        rows = conn.execute(
            "SELECT v.version_id, v.note_id, v.body, v.created, n.head_version_id "
            "FROM versions v JOIN notes n ON n.note_id = v.note_id "
            f"WHERE v.version_id IN ({placeholders})",
            version_ids,
        ).fetchall()
        for version_id, note_id, body, created, head_version_id in rows:
            identities[version_id] = CitationIdentity(
                title=first_line(body),
                is_head=version_id == head_version_id,
                note_id=note_id,
            )
            bodies[version_id] = body
            as_of[version_id] = created

    snapshot_ids = tuple({s.snapshot_id for s in supports if s.snapshot_id is not None})
    if snapshot_ids:
        placeholders = ",".join("?" for _ in snapshot_ids)
        rows = conn.execute(
            "SELECT s.snapshot_id, s.external_id, s.body, s.fetched_at, "
            "e.head_snapshot_id "
            "FROM snapshots s JOIN externals e ON e.external_id = s.external_id "
            f"WHERE s.snapshot_id IN ({placeholders})",
            snapshot_ids,
        ).fetchall()
        for snapshot_id, external_id, body, fetched_at, head_snapshot_id in rows:
            identities[snapshot_id] = CitationIdentity(
                title=first_line(body),
                is_head=snapshot_id == head_snapshot_id,
                external_id=external_id,
            )
            bodies[snapshot_id] = body
            as_of[snapshot_id] = fetched_at

    for support in supports:
        as_of.setdefault(support.target_id, None)
    return as_of, identities, bodies


#: Wraps a citation's matched body text inside its surrounding-context
#: line so it stands out in plain text -- ``LodeStatic`` renders with
#: ``markup=False`` (the module docstring explains why), so this can't lean
#: on Rich markup for emphasis.
_HIGHLIGHT_OPEN = "»"
_HIGHLIGHT_CLOSE = "«"

#: Prefix/suffix marking that a context window was truncated against the
#: full body (i.e. the window doesn't start/end at the body's own edge).
_ELLIPSIS = "…"


def render_ask_result(result: AskResult, *, context_chars: int) -> str:
    """Render a gated ask result as the screen's display text.

    Citations whose target resolved to a note/external identity
    (lode-35nu.1) are grouped by that note/external (lode-35nu.3): each cited
    note/external prints once, its title as a header, with the claims that
    cite it nested underneath and each citation shown as surrounding body
    context with the verbatim ``quoted_span`` highlighted
    (``context_chars`` either side -- :func:`_render_context`). A citation
    whose target the store had nothing to resolve (practically unreachable --
    the faithfulness gate already verified the span -- but handled rather
    than assumed away) falls back to the old flat rendering: the claim text
    followed by an indented ``[<id-kind> <id>, as of <ts>] "<span>"`` line,
    with no grouping and no context (there is no body to pull context from).

    When the answer abstained (no claim survived the gate), the honest
    abstention line prints instead. Either way, any no_egress material that
    matched surfaces under an explicit "withheld" heading rather than being
    silently dropped -- unchanged by the grouping above (ticket acceptance:
    "withheld citations and the abstention line keep their current explicit
    treatment").
    """
    lines: list[str] = []
    answer = result.answer
    if answer.abstained:
        lines.append(ABSTAIN_LINE)
    else:
        lines.extend(_render_claims(result, context_chars))
    if answer.withheld_citations:
        lines.append("")
        lines.append("Withheld from cloud synthesis (present locally):")
        for withheld in answer.withheld_citations:
            lines.append(f"  [withheld] {withheld.target_id}: {withheld.note}")
    return "\n".join(lines)


def _render_claims(result: AskResult, context_chars: int) -> list[str]:
    """Group every surviving claim's citations by cited note/external.

    Returns the grouped section (one block per distinct note/external, in
    first-cited order) followed by the flat fallback section (unresolvable
    citations, in original claim order) -- see :func:`render_ask_result`.
    """
    # Both dicts rely on insertion order for "first-cited order" -- no separate
    # ordering list. A claim is identified by its index into
    # ``result.answer.claims``, so its text is looked up at render time rather
    # than copied into every group that cites it.
    titles: dict[tuple[str, str], str] = {}
    grouped: dict[tuple[str, str], dict[int, list[str]]] = {}
    flat: list[tuple[str, str]] = []  # (claim text, rendered citation line)

    for claim_idx, claim in enumerate(result.answer.claims):
        for support in claim.support:
            identity = result.identities.get(support.target_id)
            if identity is None or (group_key := _group_key(identity)) is None:
                flat.append(
                    (
                        claim.text,
                        _render_citation(support, result.as_of.get(support.target_id)),
                    )
                )
                continue
            titles.setdefault(group_key, identity.title)
            citations = grouped.setdefault(group_key, {}).setdefault(claim_idx, [])
            citations.append(
                _render_grouped_citation(
                    support,
                    result.as_of.get(support.target_id),
                    result.bodies.get(support.target_id),
                    context_chars,
                )
            )

    lines: list[str] = []
    for group_key, claims in grouped.items():
        lines.append(titles[group_key])
        for claim_idx, citation_lines in claims.items():
            lines.append(f"  {result.answer.claims[claim_idx].text}")
            lines.extend(f"    {line}" for line in citation_lines)
    if flat:
        if lines:
            lines.append("")
        for claim_text, citation_line in flat:
            lines.append(claim_text)
            lines.append(citation_line)
    return lines


def _group_key(identity: CitationIdentity) -> tuple[str, str] | None:
    """The note/external this citation groups under, or ``None`` if unresolvable."""
    if identity.note_id is not None:
        return ("note", identity.note_id)
    if identity.external_id is not None:
        return ("external", identity.external_id)
    return None


def citation_targets(result: AskResult) -> list[str]:
    """Distinct navigable citation ``target_id``s, in the order they render (lode-35nu.4).

    Walks the *same* grouped traversal :func:`_render_claims` renders with --
    outer loop over distinct notes/externals in first-cited order, inner loop
    over that group's citations in claim order -- so a caller offering
    "next/previous citation" navigation (the ask screen) steps through targets
    top-to-bottom exactly as they appear on screen. A flat first-cited walk
    does *not* do this: a note cited by two claims with another note's claim
    between them renders as one contiguous block but would be walked
    non-contiguously, so the status line's "Citation n/m" would disagree with
    what the reader is looking at.

    A target is excluded when it has no :class:`CitationIdentity`, or an
    identity that :func:`_group_key` cannot place (neither ``note_id`` nor
    ``external_id`` -- the "exactly one is set" invariant violated). Those are
    exactly the citations :func:`_render_claims` drops into its ungrouped flat
    fallback section, and there is no note/external to navigate to for them --
    so the navigable set is precisely the grouped, rendered set.

    Keyed by ``target_id`` (a specific ``version_id``/``snapshot_id``), not by
    note/external -- deliberately: two citations of the same note but
    different versions are two distinct, individually navigable entries, and
    opening one must show the exact version actually cited, not just the
    note's current head (the ticket's own framing).
    """
    seen: set[str] = set()
    grouped: dict[tuple[str, str], list[str]] = {}
    for claim in result.answer.claims:
        for support in claim.support:
            identity = result.identities.get(support.target_id)
            if identity is None or (group_key := _group_key(identity)) is None:
                continue
            if support.target_id in seen:
                continue
            seen.add(support.target_id)
            grouped.setdefault(group_key, []).append(support.target_id)
    return [target_id for bucket in grouped.values() for target_id in bucket]


def _render_citation(support: Support, as_of: str | None) -> str:
    """Render one support as an indented ``[<id-kind> <id>, as of <ts>] "<span>"`` line.

    The flat fallback for a citation whose target didn't resolve to an
    identity -- no body to pull surrounding context from, so this renders
    the bare span exactly as before grouping existed.
    """
    provenance = _provenance(support, as_of)
    return f'  [{provenance}]  "{support.quoted_span}"'


def _render_grouped_citation(
    support: Support, as_of: str | None, body: str | None, context_chars: int
) -> str:
    """Render one support nested under its note/external group's header.

    Same ``[<id-kind> <id>, as of <ts>]`` provenance prefix as the flat
    fallback, followed by the citation's surrounding context with its
    ``quoted_span`` highlighted rather than the bare span alone.
    """
    provenance = _provenance(support, as_of)
    context = _render_context(
        body, support.quoted_span, context_chars, hint=support.body_offset
    )
    return f"[{provenance}]  {context}"


def _provenance(support: Support, as_of: str | None) -> str:
    if support.version_id is not None:
        target = f"version {support.version_id}"
    else:
        target = f"snapshot {support.snapshot_id}"
    return f"{target}, as of {as_of}" if as_of else f"{target}, as of unknown"


def _render_context(
    body: str | None, span: str, context_chars: int, *, hint: int | None = None
) -> str:
    """Surrounding text around ``span`` inside ``body``, with ``span`` highlighted.

    ``context_chars`` characters of ``body`` on either side of the span,
    all collapsed to single-line whitespace (a note body is often multi-line;
    the citation is one display line). The highlighted text is the body's own
    matched region -- not the span string as the model wrote it -- wrapped in
    :data:`_HIGHLIGHT_OPEN`/:data:`_HIGHLIGHT_CLOSE`, so the highlight is
    always contiguous with the context around it. An ellipsis marks a side
    truncated against the body's own edge.

    The span is located with :func:`~lode.faithfulness.locate_span` -- the same
    primitive the faithfulness gate's own ``span_occurs`` is derived from, so
    this renderer can never be stricter than the gate that let the citation
    through. (An exact-substring-only search here would be: the gate accepts a
    span matching only after whitespace normalization, and a quote reflowed off
    a multi-line body is the common case, so context would silently vanish for
    a whole class of gate-passing citation.) When ``span`` occurs more than once
    in ``body``, ``hint`` -- ``Support.body_offset``, stamped app-side against
    the retrieved passage the span actually came from (lode-hruz) -- picks the
    occurrence nearest it; ``hint=None`` (unresolved) falls back to the first
    occurrence, same as before that offset existed. Falls back to the bare
    quoted span (no context) only when there's no body to draw from or the span
    doesn't locate at all.
    """
    if body is None:
        return f'"{span}"'
    located = locate_span(span, body, hint=hint)
    if located is None:
        return f'"{span}"'
    start, end = located
    before = normalize_whitespace(body[max(0, start - context_chars) : start])
    after = normalize_whitespace(body[end : end + context_chars])
    highlighted = normalize_whitespace(body[start:end])
    prefix = _ELLIPSIS if start - context_chars > 0 else ""
    suffix = _ELLIPSIS if end + context_chars < len(body) else ""
    parts = [
        f"{prefix}{before}" if before else "",
        f"{_HIGHLIGHT_OPEN}{highlighted}{_HIGHLIGHT_CLOSE}",
        f"{after}{suffix}" if after else "",
    ]
    return " ".join(p for p in parts if p)
