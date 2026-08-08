"""``lode recover`` -- undo a soft-delete: repoint a tombstoned note's head."""

from typing import Annotated

import typer

from lode import cli
from lode.cli import _DbOption, _open_db, _report_ambiguous_prefix, app
from lode.lexical import LexicalCacheBackend
from lode.notes_read import note_head_op_and_parent
from lode.repository import AmbiguousNoteIdError, CompositeCache, Repository


@app.command()
def recover(
    target: Annotated[
        str,
        typer.Argument(help="Note id, or an unambiguous prefix of one, to recover."),
    ],
    db: _DbOption = None,
) -> None:
    """Undo a soft-delete: repoint a tombstoned note's head past the tombstone.

    A soft-deleted note is otherwise a one-way trip to "lode purge".

    TARGET may be a full id or an unambiguous prefix of one. Unlike
    "purge"/"show", which only ever resolve a prefix to a live note, a
    prefix here may also resolve to a tombstoned note, since that is the
    only valid input. A prefix matching more than one note (live or
    deleted) is still ambiguous, and unknown ids error the same way
    "purge"/"show" do.

    A resolved note that is not currently tombstoned errors clearly rather
    than silently doing nothing.
    """
    conn = _open_db(db)
    try:
        repo = Repository(conn, cache=CompositeCache([LexicalCacheBackend(conn)]))
        try:
            note_id = repo.resolve_note_prefix(target, include_deleted=True)
        except KeyError:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1) from None
        except AmbiguousNoteIdError as exc:
            _report_ambiguous_prefix(conn, target, exc)

        head = note_head_op_and_parent(conn, note_id)
        if head is None:
            # resolve_note_prefix returns a full id unchanged without checking
            # it exists (purge's/show's own contract) -- an unknown full id
            # lands here.
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1)
        op, parent_version_id = head
        if op != "delete":
            typer.echo(f"note is not deleted: {note_id}", err=True)
            raise typer.Exit(code=1)

        # Threaded for the same reason as `add`'s save (lode-40g): recover()
        # re-indexes the restored body through redact_before_index(), so a bare
        # Settings() here would silently ignore the user's own redaction patterns.
        result = repo.recover(
            note_id, target_version=parent_version_id, settings=cli._resolve_settings()
        )
    finally:
        conn.close()
    typer.echo(f"recovered {result.note_id}: head now {result.version_id}")
