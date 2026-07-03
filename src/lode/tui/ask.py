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
Textual app, exactly like :mod:`lode.tui.capture`. :func:`render_ask_result`
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

#: The honest failure mode's display line -- mirrors ``lode.cli._ABSTAIN_LINE``
#: verbatim (not imported: that name is CLI-command-private, and duplicating
#: one literal string is simpler than reaching into the CLI module for it).
ABSTAIN_LINE = "Your notes don't answer this."


@dataclass(frozen=True)
class AskResult:
    """A gated cited answer plus each citation's as-of provenance.

    ``as_of`` maps a cited ``target_id`` (a note ``version_id`` or external
    ``snapshot_id``) to its resolved as-of timestamp, or ``None`` when the
    store had nothing to resolve (practically unreachable -- the faithfulness
    gate already verified the span against the stored body -- but handled
    rather than assumed away).
    """

    answer: "CitedAnswer"
    as_of: dict[str, str | None] = field(default_factory=dict)


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
        as_of = {
            support.target_id: _resolve_as_of(conn, support)
            for claim in answer.claims
            for support in claim.support
        }
        return AskResult(answer=answer, as_of=as_of)
    finally:
        conn.close()


def _resolve_as_of(conn: sqlite3.Connection, support: "Support") -> str | None:
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


def _render_citation(support: "Support", as_of: str | None) -> str:
    """Render one support as an indented ``[<id-kind> <id>, as of <ts>] "<span>"`` line."""
    if support.version_id is not None:
        target = f"version {support.version_id}"
    else:
        target = f"snapshot {support.snapshot_id}"
    provenance = f"{target}, as of {as_of}" if as_of else f"{target}, as of unknown"
    return f'  [{provenance}]  "{support.quoted_span}"'
