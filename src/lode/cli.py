"""lode command-line entry point.

A Typer app wired to the ``lode`` console-script (``lode --help`` lists the
subcommand surface). ``add`` (capture + save, lode-y42.1) is real; ``ask`` /
``purge`` / ``status`` / ``eval`` remain dispatching stubs until their E10 tasks.
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

import typer

from lode import __version__, jobs, versions
from lode.logconfig import configure_logging
from lode.storage import init_db

app = typer.Typer(
    name="lode",
    help="AI-first personal knowledge base for things you learn at work.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """lode — capture and retrieve what you learn at work."""
    # Group callback: keeps lode a multi-command app so ``--help`` lists the
    # subcommands. Real behaviour for add / ask / purge / status / eval lands
    # in later E0/E10 tasks. Configure logging once, here, so every subcommand
    # (and the Anthropic SDK) logs consistently (LODE_LOG_LEVEL / ANTHROPIC_LOG).
    configure_logging()


def _stub(command: str) -> None:
    """Placeholder body for a subcommand whose real behaviour lands in E10."""
    typer.echo(f"lode {command}: not yet implemented (lands in E10).")


def _default_db_path() -> Path:
    """Resolve the database path: ``$LODE_DB`` else ``~/.local/share/lode/lode.db``.

    The ``--db`` option's ``envvar`` already reads ``LODE_DB``; this is the
    fallthrough default when neither the flag nor the env var is set.
    """
    return Path.home() / ".local" / "share" / "lode" / "lode.db"


def _write_draft(db_path: Path, note_id: str, body: str) -> Path:
    """Persist a CAS-rejected capture buffer beside the DB so it is never lost.

    Named uniquely (``mkstemp``) so a retry never clobbers an earlier draft; the
    interactive re-apply/discard surface waits for the TUI (E11). Returns the
    draft's path for the user-facing message.
    """
    fd, name = tempfile.mkstemp(
        prefix=f"{note_id}.", suffix=".draft", dir=db_path.parent
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return Path(name)


@app.command()
def add(
    text: str | None = typer.Argument(
        None, help="Note body. Omit to read the note verbatim from stdin."
    ),
    db: Path | None = typer.Option(
        None,
        "--db",
        envvar="LODE_DB",
        help="SQLite database path (default: ~/.local/share/lode/lode.db).",
    ),
) -> None:
    """Capture a note into lode and enqueue its derive jobs.

    Instant by design: this writes the version (``versions.save``) and enqueues
    the embed/enrich derive jobs, with **no AI in the capture path** (the save
    path, ``docs/design.md``). The body comes from the ``TEXT`` argument or, if
    omitted, verbatim from stdin; an empty / whitespace-only body is refused.
    """
    db_path = db or _default_db_path()
    body = text if text is not None else sys.stdin.read()
    if not body.strip():
        typer.echo("refusing to save an empty note", err=True)
        raise typer.Exit(code=1)

    # A fresh logical id per capture — `add` always creates a new note (no
    # aliasing), so `save` always takes its create path.
    note_id = str(uuid.uuid4())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    try:
        try:
            result = versions.save(conn, note_id, body)
        except versions.HeadConflictError:
            # A create against an already-present note: never clobber or
            # auto-merge — preserve the buffer as a draft and bail (the
            # interactive re-apply path lands with the TUI, E11).
            draft = _write_draft(db_path, note_id, body)
            typer.echo(f"note changed since opened; draft saved to {draft}", err=True)
            raise typer.Exit(code=1) from None
        jobs.enqueue_derive_jobs(conn, result.version_id)
    finally:
        conn.close()
    typer.echo(note_id)


@app.command()
def ask() -> None:
    """Ask a cited question over the corpus (stub; lands in E10)."""
    _stub("ask")


@app.command()
def purge() -> None:
    """Hard-delete notes and their derived data (stub; lands in E10)."""
    _stub("purge")


@app.command()
def status() -> None:
    """Show store and work-queue status (stub; lands in E10)."""
    _stub("status")


@app.command(name="eval")
def eval_() -> None:
    """Run the eval harness over the golden set (stub; lands in E10)."""
    _stub("eval")


@app.command()
def version() -> None:
    """Print the installed lode version."""
    typer.echo(__version__)


if __name__ == "__main__":  # pragma: no cover
    app()
