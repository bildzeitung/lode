"""``lode add`` -- capture a note, enqueue its derive jobs, fast-track enrichment."""

import sys
import uuid
from typing import Annotated

import typer

from lode import cli, versions
from lode.cli import _DbOption, _enrich_immediately, _write_draft, app
from lode.config import default_db_path
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db


@app.command(
    help=(
        "Capture a note, enqueue its derive jobs, and fast-track enrichment.\n\n"
        "The body comes from TEXT, or -- if omitted -- is read verbatim from "
        "stdin. Saving makes the note keyword-findable immediately; tags, "
        "entities, and inferred edges usually appear right away too, and "
        'embedding always finishes asynchronously via "lode work".'
    )
)
def add(
    text: Annotated[
        str | None,
        typer.Argument(help="Note body. Omit to read the note verbatim from stdin."),
    ] = None,
    db: _DbOption = None,
) -> None:
    """Capture a note, enqueue its derive jobs, and fast-track enrichment.

    The save path (see docs/design.md):

    1. The note is saved and both its embed and enrich jobs are enqueued
       atomically -- it is keyword-findable the moment the save commits.
    2. The enrich job is opportunistically run immediately, so the fresh
       note's tags, entities, and inferred edges usually appear right away.
       If that immediate attempt loses to a concurrent "lode work" or
       fails outright, the job's normal retry/backoff accounting picks it
       up later -- no separate re-enqueue path needed.
    3. Embedding always runs asynchronously via "lode work", so this
       command returns quickly regardless of enrichment latency.

    The body comes from the TEXT argument, or -- if omitted -- is read
    verbatim from stdin. An empty or whitespace-only body is refused.
    """
    db_path = db or default_db_path()
    body = text if text is not None else sys.stdin.read()
    if not body.strip():
        typer.echo("refusing to save an empty note", err=True)
        raise typer.Exit(code=1)

    settings = cli._resolve_settings()

    # A fresh logical id per capture — `add` always creates a new note (no
    # aliasing), so `save` always takes its create path.
    note_id = str(uuid.uuid4())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    try:
        # Inject the synchronous model-free cache: LexicalCacheBackend chunks the
        # body and writes passages + passages_fts right after the version commits,
        # so the note is keyword-findable BEFORE any async embedding runs
        # (lode-xyb; embedding stays async via the worker).
        repo = Repository(conn, cache=CompositeCache([LexicalCacheBackend(conn)]))
        try:
            # settings resolved once for the whole command and threaded into BOTH
            # legs (lode-40g): save() runs redact_before_index() + the drawdown
            # scan off it, so without this the user's own
            # redact_before_index_patterns / url_tracking_param_blocklist would
            # be ignored and a secret they configured would reach the index
            # unredacted.
            result = repo.save(note_id, body, settings=settings)
        except versions.HeadConflictError:
            # A create against an already-present note: never clobber or
            # auto-merge — preserve the buffer as a draft and bail (the
            # interactive re-apply path lands with the TUI, E11).
            draft = _write_draft(db_path, note_id, body)
            typer.echo(f"note changed since opened; draft saved to {draft}", err=True)
            raise typer.Exit(code=1) from None

        # Opportunistic immediate enrichment — claim + run the enrich job
        # save() just enqueued so tags/entities/edges appear right away
        # (lode-npx.2 "interactive now" path). A lost claim race or a run
        # failure is handled by the job's own retry/backoff/dead-letter
        # accounting, not here.
        if not result.deduped:
            _enrich_immediately(conn, db_path, result.version_id, settings)
    finally:
        conn.close()
    typer.echo(note_id)
