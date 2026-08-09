"""``lode egress`` / ``lode no-egress`` -- the egress audit read-out + no_egress control."""

from typing import Annotated

import typer

from lode.cli import _DbOption, _open_db, app
from lode.cli.status import EgressPurpose, _format_redactions, _format_sent
from lode.jobs_read import list_egress


@app.command()
def egress(
    purpose: Annotated[
        EgressPurpose | None,
        typer.Option(
            "--purpose", help="Only list sends of this purpose (default: all)."
        ),
    ] = None,
    db: _DbOption = None,
) -> None:
    """List what content has left the box for the cloud, and when.

    A straight answer to "what of mine has gone to the cloud, and when?"
    (see docs/externals.md "Egress log"). One row per cloud send, oldest
    first: id, timestamp, purpose, model, the ids of what was sent, and
    which redactions were applied. --purpose narrows to enrich, qa, or tool
    sends. A purpose='tool' row (lode.tools.fetch_for_ask) has no model -- a
    tool call is egress but not an LLM call -- and additionally carries the
    call's destination (the URL/API base hit) and its arguments as sent,
    both post-redaction and both appended after the redactions field.
    """
    conn = _open_db(db)
    try:
        rows = list_egress(conn, purpose.value if purpose is not None else None)
    finally:
        conn.close()

    if not rows:
        typer.echo("no egress")
        return

    for row in rows:
        # model is NULL for a purpose='tool' row (lode-35nu.11.7): a tool call is
        # cloud egress but not an LLM call, so there is no model to name. Format
        # it as "-" rather than letting f-string padding raise on None.
        line = (
            f"{row.id}  {row.ts}  {row.purpose:<7} {row.model or '-':<20}  "
            f"sent: {_format_sent(row.sent_targets)}  "
            f"redactions: {_format_redactions(row.redactions)}"
        )
        if row.purpose == "tool":
            line += (
                f"  destination: {row.destination or '-'}  "
                f"arguments: {row.arguments or '-'}"
            )
        typer.echo(line)


@app.command(name="no-egress")
def no_egress_(
    external_id: Annotated[
        str,
        typer.Argument(
            help="The external source's id (its canonical URL) to mark/clear."
        ),
    ],
    clear: Annotated[
        bool,
        typer.Option(
            "--clear",
            help="Clear no_egress instead of setting it (source becomes cloud-eligible again).",
        ),
    ] = False,
    db: _DbOption = None,
) -> None:
    """Mark (or --clear) an external source no_egress (see docs/externals.md).

    A no_egress external stays captured, chunked, embedded, and locally
    retrievable (keyword + vector) -- only cloud egress changes. It is
    excluded from both the enrichment send and the Q&A context, and any
    answer that would have cited it surfaces it instead as "present,
    withheld from cloud synthesis".

    EXTERNAL_ID must already exist (e.g. drawn down via a note's pasted
    URL) -- this command does not create sources.
    """
    conn = _open_db(db)
    try:
        from lode.externals import set_no_egress

        existed = set_no_egress(conn, external_id, no_egress=not clear)
    finally:
        conn.close()
    if not existed:
        typer.echo(f"no such external source: {external_id}", err=True)
        raise typer.Exit(code=1)
    state = "cleared" if clear else "marked"
    typer.echo(f"{state} no_egress: {external_id}")
