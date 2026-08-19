"""``lode ask`` -- retrieve, synthesize, gate, then cite an answer from your notes."""

from typing import TYPE_CHECKING, Annotated

import typer

from lode import cli
from lode.citations_read import resolve_citations
from lode.cli import _abort_on_provider_error, _DbOption, _open_db, app
from lode.config import default_db_path, lance_dir
from lode.llm_provider import LLMProviderError

if TYPE_CHECKING:
    # Type-only; the runtime imports live inside ``ask`` so the capture-path
    # commands (``add`` is "instant by design") never pay the cost of loading
    # the Q&A SDK (anthropic) or the vector stack (pyarrow), which the cited
    # Q&A loop pulls in but the rest of the CLI never touches.
    from lode.answer import Support
    from lode.cited_answer import CitedAnswer

#: How an abstention reads at the terminal — the honest "no grounded answer"
#: failure mode (``docs/retrieval.md`` the faithfulness gate's abstention path),
#: printed when no claim survives the gate.
_ABSTAIN_LINE = (
    "No grounded answer: your notes don't support a cited claim for this question."
)


@app.command()
def ask(
    question: Annotated[
        str,
        typer.Argument(
            help="Your question, answered from your own notes with citations."
        ),
    ],
    think_harder: Annotated[
        bool,
        typer.Option(
            "--think-harder",
            help="Use the higher-quality 'think harder' Q&A model (Claude Opus).",
        ),
    ] = False,
    db: _DbOption = None,
) -> None:
    """Answer a question from your notes -- retrieve, synthesize, gate, then cite.

    Runs the read pipeline (lexical + dense search, fused and re-ranked) to
    build a cited context, synthesizes structured claims from it, and checks
    each one against your notes before showing anything. Prints either the
    surviving cited claims -- each with its source and the verbatim span it
    rests on -- or an honest abstention when nothing is grounded. Any
    no_egress material that matched is surfaced as "present, withheld from
    cloud synthesis" rather than silently dropped.
    """
    # Imported here, not at module scope: cited_answer / auth pull in the Anthropic
    # SDK, and lode.retrieval pulls in the vector stack (pyarrow via
    # lode.vectorstore), neither of which the instant capture path (``add``)
    # must ever load.
    from lode import cited_answer
    from lode.auth import AuthError
    from lode.retrieval import _retrieve

    db_path = db or default_db_path()
    # Resolve settings once so gate-tuning knobs (entailment_threshold, etc.) come
    # from a single configured object, not from per-call Settings() defaults buried
    # inside _retrieve and cited_answer.ask. _resolve_settings() (not bare
    # Settings()) so a config-file override actually reaches the pipeline.
    settings = cli._resolve_settings()
    conn = _open_db(db_path)
    try:
        context = _retrieve(
            conn, question, lance_dir=lance_dir(db_path), settings=settings
        )
        answer = cited_answer.ask(
            conn, question, context, think_harder=think_harder, settings=settings
        )
        # Resolve each surviving citation's as-of provenance while conn is still
        # open (docs/externals.md "Every AI claim from an external must cite
        # 'as of fetched_at'") -- a note's is its version's write time, an
        # external's its snapshot's fetch time
        # (:func:`lode.citations_read.resolve_citations`, shared with the TUI's
        # ask screen, lode-kuc7). Only the as_of half is used here; identities
        # is discarded -- the terminal `ask` output doesn't render titles.
        supports = [support for claim in answer.claims for support in claim.support]
        as_of, _identities, _bodies = resolve_citations(conn, supports)
    except (AuthError, LLMProviderError) as err:
        # `ask` is one-shot with no retry machinery, so every provider failure
        # ends the command -- both the credential case and any other
        # LLMProviderError. AuthError must be named alongside it: they are
        # sibling RuntimeError subclasses, neither an ancestor of the other, so
        # naming only one silently misses the other (lode-yx1c).
        _abort_on_provider_error("ask", err)
    finally:
        conn.close()
    for line in _format_cited_answer(answer, as_of):
        typer.echo(line)


def _format_cited_answer(
    answer: CitedAnswer, as_of: dict[str, str | None]
) -> list[str]:
    """Render a gated answer for the terminal: cited claims, or an abstention.

    Each surviving claim prints its text followed by one indented citation line per
    support — its ``version_id`` / ``snapshot_id``, its resolved as-of provenance
    (``as_of``, keyed by :attr:`Support.target_id` —
    :func:`lode.citations_read.resolve_citations`), and the
    verbatim span. ``docs/externals.md`` ("Every AI claim from an external must cite
    'as of fetched_at'") is why this line is never omitted, note citation or
    external. When the answer abstained (no claim survived the gate) the honest
    abstention line is printed instead. Either way, any no_egress material is
    surfaced as "present, withheld from cloud synthesis" so the user knows relevant
    local content exists.
    """
    lines: list[str] = []
    if answer.abstained:
        lines.append(_ABSTAIN_LINE)
    else:
        for claim in answer.claims:
            lines.append(claim.text)
            lines.extend(
                _format_citation(support, as_of.get(support.target_id))
                for support in claim.support
            )
    for withheld in answer.withheld_citations:
        lines.append(f"  withheld {withheld.target_id}: {withheld.note}")
    return lines


def _format_citation(support: Support, as_of: str | None) -> str:
    """Render one support as an indented ``<id-kind> <id>, as of <ts>  "<span>"`` line.

    ``as_of`` is ``None`` only for a target the store could not resolve
    (practically unreachable — the faithfulness gate already verified the span
    against the stored body — but rendered as ``"as of unknown"`` rather than
    assumed away).
    """
    if support.version_id is not None:
        target = f"version_id {support.version_id}"
    else:
        target = f"snapshot_id {support.snapshot_id}"
    provenance = f"{target}, as of {as_of}" if as_of else f"{target}, as of unknown"
    return f'  - {provenance}  "{support.quoted_span}"'
