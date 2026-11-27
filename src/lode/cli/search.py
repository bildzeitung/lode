"""``lode search`` -- model-free BM25 note search, the CLI twin of Browse's ``/``."""

from typing import Annotated

import typer

from lode.cli import _DbOption, app
from lode.cli.notes import print_note_rows
from lode.config import default_db_path
from lode.notes_read import search_notes


@app.command(
    name="search",
    help=(
        "Find live notes by the words they contain -- no AI, no cost.\n\n"
        "Run this for a quick lookup by the literal words, or word beginnings, "
        'a note contains. Unlike "lode ask", it never calls a model or the '
        "network and never synthesizes an answer: it lists matching notes "
        'best-match-first, in the same rows as "lode notes", and matches the '
        "TUI Browse search for the same query."
    ),
)
def search_(
    text: Annotated[
        str,
        typer.Argument(
            help=(
                "Words to look for (quote several). Each matches as a prefix; "
                "a note matching any is listed."
            )
        ),
    ],
    db: _DbOption = None,
) -> None:
    """Search live notes' current content, offline (lode-oeb3b).

    A thin wrapper over :func:`lode.notes_read.search_notes` -- the very
    function the TUI Browse quick search calls -- so the two cannot drift. It
    builds no embedder and no provider client.
    """
    db_path = db or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = search_notes(db_path, text)
    if not rows:
        typer.echo("no matching notes")
        return
    print_note_rows(rows)
