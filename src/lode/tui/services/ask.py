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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from lode.config import Settings, lance_dir
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


@dataclass(frozen=True)
class CitationIdentity:
    """Resolved note/external identity for one citation target (lode-35nu.1).

    Exactly one of ``note_id``/``external_id`` is set, mirroring
    :class:`~lode.answer.Support`'s own ``version_id``/``snapshot_id`` split.
    ``title`` is the first non-blank line of the *cited* version/snapshot's
    own body -- the only "title" concept this codebase has (mirrors
    ``notes_read._first_line``'s convention for the browse table's summary
    column); it comes from the cited version even when that version is
    superseded, so an old citation still shows a title rather than the
    current head's. ``is_head`` is True when the cited version/snapshot is
    still the note/external's current head, False when superseded.
    """

    note_id: str | None = None
    external_id: str | None = None
    title: str = ""
    is_head: bool = False


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
    same convention as ``as_of``.
    """

    answer: CitedAnswer
    as_of: dict[str, str | None] = field(default_factory=dict)
    identities: dict[str, CitationIdentity] = field(default_factory=dict)


def run_ask(
    db_path: Path,
    question: str,
    *,
    think_harder: bool = False,
    settings: Settings | None = None,
) -> AskResult:
    """Run the cited Q&A loop for ``question`` and resolve citation provenance.

    Drives ``lode ask``'s own pipeline start to finish: retrieve
    (:func:`lode.cli._retrieve`) -> synthesize + gate
    (:func:`lode.cited_answer.ask`) -> resolve as-of provenance for each
    surviving citation. Raises :class:`lode.auth.AuthError` on unresolved
    Anthropic credentials, same as the CLI -- the screen catches it and
    notifies rather than crashing.

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
        context = _retrieve(
            conn, question, lance_dir=lance_dir(db_path), settings=settings
        )
        answer = cited_answer.ask(
            conn, question, context, think_harder=think_harder, settings=settings
        )
        supports = [support for claim in answer.claims for support in claim.support]
        as_of = {
            support.target_id: _resolve_as_of(conn, support) for support in supports
        }
        identities = _resolve_identities(conn, supports)
        return AskResult(answer=answer, as_of=as_of, identities=identities)
    finally:
        conn.close()


def _resolve_as_of(conn: sqlite3.Connection, support: Support) -> str | None:
    """Resolve one citation's as-of timestamp from the store.

    A note ``version_id`` is stamped at write time (``versions.created``); an
    external ``snapshot_id`` at fetch time (``snapshots.fetched_at``). Returns
    ``None`` for a target absent from the store.
    """
    if support.version_id is not None:
        row = conn.execute(
            "SELECT created FROM versions WHERE version_id = ?",
            (support.version_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT fetched_at FROM snapshots WHERE snapshot_id = ?",
            (support.snapshot_id,),
        ).fetchone()
    return row[0] if row is not None else None


def _resolve_identities(
    conn: sqlite3.Connection, supports: list[Support]
) -> dict[str, CitationIdentity]:
    """Resolve note/external identity for every ``supports`` target, batched (lode-35nu.1).

    Two queries total -- one ``IN (...)`` for every cited ``version_id``, one
    for every cited ``snapshot_id`` -- not one query per support, so a
    multi-claim answer costs a fixed two round-trips regardless of citation
    count (the ticket's "a single batched query" acceptance line). A
    target absent from the store (practically unreachable, same as
    :func:`_resolve_as_of`) is simply missing from the returned dict.
    """
    identities: dict[str, CitationIdentity] = {}
    version_ids = {s.version_id for s in supports if s.version_id is not None}
    if version_ids:
        placeholders = ",".join("?" for _ in version_ids)
        rows = conn.execute(
            "SELECT v.version_id, v.note_id, v.body, n.head_version_id "
            "FROM versions v JOIN notes n ON n.note_id = v.note_id "
            f"WHERE v.version_id IN ({placeholders})",
            tuple(version_ids),
        ).fetchall()
        for version_id, note_id, body, head_version_id in rows:
            identities[version_id] = CitationIdentity(
                note_id=note_id,
                title=_first_line(body),
                is_head=version_id == head_version_id,
            )
    snapshot_ids = {s.snapshot_id for s in supports if s.snapshot_id is not None}
    if snapshot_ids:
        placeholders = ",".join("?" for _ in snapshot_ids)
        rows = conn.execute(
            "SELECT s.snapshot_id, s.external_id, s.body, e.head_snapshot_id "
            "FROM snapshots s JOIN externals e ON e.external_id = s.external_id "
            f"WHERE s.snapshot_id IN ({placeholders})",
            tuple(snapshot_ids),
        ).fetchall()
        for snapshot_id, external_id, body, head_snapshot_id in rows:
            identities[snapshot_id] = CitationIdentity(
                external_id=external_id,
                title=_first_line(body),
                is_head=snapshot_id == head_snapshot_id,
            )
    return identities


def _first_line(body: str) -> str:
    """The first non-blank line of ``body``, or ``""`` for an all-blank body.

    Mirrors ``lode.notes_read._first_line`` (the browse table's summary
    fallback) -- duplicated rather than imported since that one is private to
    its module, the same "each surface owns its own copy of a tiny private
    helper" convention this file already follows for ``_resolve_as_of``
    (``lode.cli`` keeps its own copy too).
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def render_ask_result(result: AskResult) -> str:
    """Render a gated ask result as the screen's display text.

    Each surviving claim prints its text followed by one indented citation
    line per support -- its version/snapshot id, its resolved as-of
    provenance, and the verbatim span. When the answer abstained (no claim
    survived the gate), the honest abstention line prints instead. Either
    way, any no_egress material that matched surfaces under an explicit
    "withheld" heading rather than being silently dropped.
    """
    lines: list[str] = []
    answer = result.answer
    if answer.abstained:
        lines.append(ABSTAIN_LINE)
    else:
        for claim in answer.claims:
            lines.append(claim.text)
            for support in claim.support:
                lines.append(
                    _render_citation(support, result.as_of.get(support.target_id))
                )
    if answer.withheld_citations:
        lines.append("")
        lines.append("Withheld from cloud synthesis (present locally):")
        for withheld in answer.withheld_citations:
            lines.append(f"  [withheld] {withheld.target_id}: {withheld.note}")
    return "\n".join(lines)


def _render_citation(support: Support, as_of: str | None) -> str:
    """Render one support as an indented ``[<id-kind> <id>, as of <ts>] "<span>"`` line."""
    if support.version_id is not None:
        target = f"version {support.version_id}"
    else:
        target = f"snapshot {support.snapshot_id}"
    provenance = f"{target}, as of {as_of}" if as_of else f"{target}, as of unknown"
    return f'  [{provenance}]  "{support.quoted_span}"'
