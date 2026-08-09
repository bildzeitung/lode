"""``lode notes`` -- list notes, live by default, tombstoned with --deleted."""

from typing import Annotated

import typer
from rich.markup import escape

from lode.cli import _DbOption, _short_date, app, console
from lode.config import default_db_path
from lode.notes_read import list_deleted_notes, list_notes


@app.command(
    name="notes",
    help=(
        "List notes -- live by default, tombstoned with --deleted.\n\n"
        "One row per note, newest first: its full id (copy-pasteable "
        'straight into "lode purge"), a short date, and its summary. '
        "Under --deleted each row carries a trailing marker, so it still "
        'reads as a tombstone once copied out for "lode recover".'
    ),
)
def notes_(
    deleted: Annotated[
        bool,
        typer.Option(
            "--deleted",
            help="List only tombstoned (soft-deleted) notes, instead of live ones.",
        ),
    ] = False,
    db: _DbOption = None,
) -> None:
    """List notes -- live by default, tombstoned with --deleted.

    One row per note, newest first: its full id (copy-pasteable straight
    into "lode purge"), a short date, and its summary (the AI-generated one
    once available, otherwise the note's first line). The full id is never
    shortened here -- this is the copy-pasteable, greppable listing that
    Browse and "lode show" deliberately aren't. A blank line separates each
    note from the next.

    --deleted flips that: it lists only tombstoned notes instead of live
    ones. A deleted note vanishes from a plain listing, so this is the only
    route back to an id a later "lode show" or "lode recover" can act on.
    Each row in this mode also carries a trailing " [deleted]" marker -- the
    same convention "lode show" uses for a tombstoned head -- so a line still
    reads as a tombstone once copied out of this listing. The live listing is
    unaffected: no marker, byte-identical to before.
    """
    db_path = db or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list_deleted_notes(db_path) if deleted else list_notes(db_path)
    if not rows:
        # Scope the empty message to what was actually asked for: a bare
        # "no notes" under --deleted reads as "you have no notes at all",
        # which is false whenever live notes exist (lode-d32.2).
        typer.echo("no deleted notes" if deleted else "no notes")
        return
    # lode-bau6/lode-l38d.12: every row in ``--deleted`` mode is a tombstone
    # by construction of the flag, so this is computed once, not per-row --
    # empty in live mode, which is what keeps that path byte-identical to
    # before this ticket.
    marker = " [deleted]" if deleted else ""
    for i, row in enumerate(rows):
        if i:
            console.print()
        # ``row.summary`` is unescaped user/AI text and may itself contain
        # "[...]" (markdown links, code, etc.) -- escape it so it can never
        # be mistaken for markup and corrupt the row or the styles around it.
        # The marker is escaped TOGETHER WITH the summary, not appended after
        # -- escaping only the summary and then concatenating the raw
        # ``" [deleted]"`` literal would hand rich's markup parser a bare
        # "[deleted]" tag. "deleted" is not a real style name, and
        # ``Console.print`` does not raise on an unknown tag -- it resolves
        # to a null style and eats it, so the marker would render as nothing
        # at all (verified against rich 15.0.0; the exact failure mode
        # lode-l810 found in a sibling call site). Escaping the concatenation
        # instead turns the whole marker into a literal, visible string.
        # ``soft_wrap=True`` -- rich's Console otherwise word-wraps to its
        # detected width (80 columns when not a terminal), which would
        # silently break a long summary across lines; the prior
        # ``typer.echo`` never did that ("no truncation, no width clamp" is
        # this ticket's own description of the behaviour being preserved).
        # This is genuinely per-renderer (unlike ``highlight``, hoisted onto
        # the shared ``console`` itself, lode-re0s) -- ``lode config``'s
        # Table wants width-aware wrapping instead.
        #
        # The shared ``console`` is constructed with ``highlight=False``
        # (see its docstring above) precisely so this row never needs the
        # flag here: rich's ReprHighlighter would otherwise shred the date
        # and recolour numbers/IPs/etc. inside the user's own summary text.
        # The theme styles are the ONLY colour this row should carry.
        console.print(
            f"[note_id]{row.note_id}[/note_id]  "
            f"[date]{_short_date(row.created)}[/date]  "
            f"{escape(row.summary + marker)}",
            soft_wrap=True,
        )
