"""``lode purge`` -- hard-delete a note and its derived data (see docs/externals.md)."""

from typing import Annotated

import typer

from lode.cli import _DbOption, _open_db, _report_ambiguous_prefix, app
from lode.repository import AmbiguousNoteIdError, Repository


@app.command()
def purge(
    target: Annotated[
        str,
        typer.Argument(
            help="Note id, or an unambiguous prefix of one, to hard-delete."
        ),
    ],
    db: _DbOption = None,
) -> None:
    """Hard-delete a note and its derived data (see docs/externals.md).

    The deliberate immutability break: every body in the note's version
    chain is overwritten with a "\\[purged YYYY-MM-DD]" marker, the purge is
    stamped, AI-generated annotations are dropped (your own corrections are
    kept), and derived cache entries are evicted. There is no half-delete.

    TARGET may be a full note id or an unambiguous prefix of one.
    """
    conn = _open_db(db)
    try:
        repo = Repository(conn)
        try:
            note_id = repo.resolve_note_prefix(target)
            result = repo.purge(note_id)
        except KeyError:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1) from None
        except AmbiguousNoteIdError as exc:
            _report_ambiguous_prefix(conn, target, exc)
    finally:
        conn.close()
    typer.echo(
        f"purged {result.note_id}: swept {len(result.purged_versions)} version(s); "
        f"body now {result.marker_body}"
    )
