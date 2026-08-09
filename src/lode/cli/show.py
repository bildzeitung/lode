"""``lode show`` -- a note's head body plus its derived enrichment."""

from typing import Annotated

import typer

from lode.cli import _DbOption, _open_db, _report_ambiguous_prefix, _short_date, app
from lode.enrichment_view import EnrichmentItem, ExternalView, enrichment_view_conn
from lode.ids import short_version_id
from lode.notes_read import note_head_created_body_op
from lode.repository import AmbiguousNoteIdError, Repository


def _render_item(item: EnrichmentItem) -> str:
    """Render one :class:`~lode.enrichment_view.EnrichmentItem` for the CLI.

    The view-model carries ``stale``/``inherited`` as bare booleans
    (lode-0qc, lode-f0m1); this is where the CLI's own bracket-suffix
    convention gets applied -- the TUI modal (lode-ay5.2) is free to style
    the same bits differently. ``inherited`` prints before ``stale`` when
    both apply -- an arbitrary but fixed order, so the suffix is stable
    enough to grep for.
    """
    suffixes = []
    if item.inherited:
        suffixes.append("inherited")
    if item.stale:
        suffixes.append("stale")
    if not suffixes:
        return item.value
    return f"{item.value} [{', '.join(suffixes)}]"


def _render_items(items: list[EnrichmentItem]) -> str:
    """Render a list of items as a comma-joined line, or ``(none)`` when empty."""
    return ", ".join(_render_item(item) for item in items) if items else "(none)"


def _render_edge_detail(reason: str | None, confidence: float | None) -> str:
    """Render an edge's optional ``(reason, confidence)`` parenthetical.

    Both fields are nullable (``schema.sql``'s ``edges`` table) -- a
    user-curated (``source='user'``) edge may carry neither. Render whichever
    is present; an empty string when both are missing, so the line degrades to
    today's bare ``-> to_id`` rather than printing an empty ``()``.
    """
    parts = [reason] if reason else []
    if confidence is not None:
        parts.append(f"{confidence:.2f}")
    return f" ({', '.join(parts)})" if parts else ""


def _render_external(external: ExternalView) -> str:
    """Render one edge's :class:`~lode.enrichment_view.ExternalView`, indented (lode-8d2).

    Browse-time introspection for a drawn-down web link, printed directly
    beneath its edge's own ``-> to_id`` line -- the same view-model fields
    the TUI inspector modal (lode-ay5.2) renders, through the ONE seam
    (:mod:`lode.enrichment_view`) so this command holds no second copy of
    what an external's fields mean. ``state`` is always shown explicitly
    (``un-refreshed``/``stale``/``withheld``) rather than suppressed for the
    default case, so all three are equally visible/greppable in the output.
    """
    return (
        f"       {external.source_type} · snapshot "
        f"{short_version_id(external.snapshot_id)} · as of {external.fetched_at} "
        f"[{external.state}]"
    )


@app.command(
    name="show",
    help=(
        "Show a note's head body plus its derived enrichment.\n\n"
        "Prints the body, then summary, tags, entities, inferred edges "
        "(with reason/confidence), embedding status, and an overall "
        "enrichment status. A deleted note still shows, marked as such."
    ),
)
def show_(
    target: Annotated[
        str, typer.Argument(help="Note id, or an unambiguous prefix of one, to show.")
    ],
    db: _DbOption = None,
) -> None:
    """Show a note's head body plus its derived enrichment (on-demand introspection).

    Prints the head body, then every enrichment field: summary, tags, and
    entities (flagged if stale), inferred edges with their reason and
    confidence (e.g. "-> to_id (reason, 0.82) \\[stale]"), whether the note
    is embedded, and an overall enrichment status of pending, failed, or
    ready. A field that is genuinely empty still shows "(none)" -- that is
    independent of the overall status line.

    TARGET may be a full id or an unambiguous prefix of one, resolved the
    same way "purge" resolves one, so an unknown or ambiguous id errors
    identically.

    A tombstoned note is not filtered out here -- unlike a prefix, which
    never resolves to one, a full id still reaches it. Rather than render it
    as if live, the header carries a visible "\\[deleted]" marker while
    still printing the carried-forward body -- useful context for deciding
    whether to "lode recover" it.
    """
    conn = _open_db(db)
    try:
        repo = Repository(conn)
        try:
            note_id = repo.resolve_note_prefix(target)
        except KeyError:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1) from None
        except AmbiguousNoteIdError as exc:
            _report_ambiguous_prefix(conn, target, exc)

        head = note_head_created_body_op(conn, note_id)
        if head is None:
            # resolve_note_prefix returns a full id unchanged without checking
            # it exists (purge's own contract) -- an unknown full id lands here.
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1)
        created, body, op = head

        # The shared TUI+CLI seam (lode-ay5.1): this command no longer builds
        # its own display.py assembly. `conn` is already open and `note_id`
        # already resolved, so the conn-taking variant avoids a second
        # connection (lode-ay5.1's review note; enrichment_view_conn was
        # promoted public for exactly this caller).
        view = enrichment_view_conn(conn, note_id)
    finally:
        conn.close()
    assert view is not None  # the row fetch above already proved note_id exists

    deleted_marker = " [deleted]" if op == "delete" else ""
    typer.echo(f"note_id: {note_id}{deleted_marker}")
    typer.echo(f"created: {_short_date(created)}")
    typer.echo("")
    typer.echo(body)
    typer.echo("")

    typer.echo(f"enrichment: {view.enrichment_state}")

    summary = _render_item(view.summary) if view.summary else "(none)"
    typer.echo(f"summary: {summary}")

    typer.echo(f"tags: {_render_items(view.tags)}")
    typer.echo(f"entities: {_render_items(view.entities)}")

    if view.edges:
        typer.echo("edges:")
        for edge in view.edges:
            detail = _render_edge_detail(edge.reason, edge.confidence)
            flag = " [stale]" if edge.stale else ""
            typer.echo(f"  -> {edge.to_id}{detail}{flag}")
            if edge.external is not None:
                typer.echo(_render_external(edge.external))
    else:
        typer.echo("edges: (none)")

    embedded = "yes" if view.passage_count else "no"
    typer.echo(f"embedded: {embedded} ({view.passage_count} passage(s))")
