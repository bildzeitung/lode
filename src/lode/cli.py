"""lode command-line entry point.

A Typer app wired to the ``lode`` console-script (``lode --help`` lists the
subcommand surface). ``add`` (capture + save, lode-y42.1), the operational
``status`` / ``jobs`` read-outs (lode-y42.3), and the ``egress`` audit read-out
(E8, lode-fk8.3) are real; ``ask`` / ``eval`` remain dispatching stubs until
their E10 tasks. ``purge`` is a deliberate refusing stub until its hard-delete
mechanism (E8, lode-fk8.4) exists — it never half-deletes.
"""

import json
import os
import sqlite3
import sys
import tempfile
import uuid
from enum import Enum
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


def _open_db(db: Path | None) -> sqlite3.Connection:
    """Open the lode database (creating it if absent) with the schema applied.

    Resolves the path like ``add`` (flag/``LODE_DB``/default), ensures the parent
    directory exists, and returns an :func:`init_db` connection — so the read-out
    commands always see the ``jobs`` / ``egress_log`` tables even on a first run.
    """
    db_path = db or _default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return init_db(db_path)


#: Shared ``--db`` option (path / ``LODE_DB`` / default) for the db-backed commands.
_DB_OPTION = typer.Option(
    None,
    "--db",
    envvar="LODE_DB",
    help="SQLite database path (default: ~/.local/share/lode/lode.db).",
)


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
    db: Path | None = _DB_OPTION,
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
    """Hard-delete notes and their derived data (refuses: needs E8/lode-fk8.4).

    purge is an E8 hard delete — overwrite the body with ``[purged YYYY-MM-DD]``,
    set ``purged_at``, sweep the whole version chain, and cascade-drop the derived
    cache (vectors, FTS, ``source='ai'`` annotations) (``docs/externals.md`` "Hard
    delete"). That mechanism lands in **lode-fk8.4** and does not exist yet, so this
    command refuses rather than ship a partial, unsafe delete.
    """
    typer.echo(
        "lode purge: hard-delete is not yet available — it needs the chain-sweep + "
        "cache-cascade mechanism (E8, lode-fk8.4), which is not built. Refusing "
        "rather than partially delete.",
        err=True,
    )
    raise typer.Exit(code=1)


class JobStatus(str, Enum):
    """The ``jobs.status`` enum from ``schema.sql`` — accepted by ``--status``."""

    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class EgressPurpose(str, Enum):
    """The ``egress_log.purpose`` enum from ``schema.sql`` — accepted by ``--purpose``."""

    enrich = "enrich"
    qa = "qa"


def _short(target_version: str) -> str:
    """Abbreviate a version-id digest for a one-line listing (full id is a hash)."""
    return target_version if len(target_version) <= 12 else f"{target_version[:12]}…"


def _format_sent(sent_targets: str) -> str:
    """Render the JSON ``sent_targets`` array as shortened, comma-joined ids."""
    ids = json.loads(sent_targets)
    return ", ".join(_short(i) for i in ids) if ids else "(none)"


def _format_redactions(redactions: str | None) -> str:
    """Render the JSON ``redactions`` summary as ``id×count`` pairs (or ``none``).

    ``redactions`` is the per-target span count written by ``gate_qa_egress``
    (``{target_id: n}``), or ``NULL`` when nothing was stripped.
    """
    by_target = json.loads(redactions) if redactions else {}
    if not by_target:
        return "none"
    return ", ".join(f"{_short(t)}×{n}" for t, n in by_target.items())


@app.command()
def status(
    db: Path | None = _DB_OPTION,
) -> None:
    """Show work-queue health: job counts, dead-letters, and an egress summary.

    Reads the ``jobs`` and ``egress_log`` tables (``docs/storage.md`` §8): the
    pending/running/done/failed job counts, the dead-letter (failed) jobs with
    their last error, and how much content has left the box, by purpose.
    """
    conn = _open_db(db)
    try:
        job_counts = dict(
            conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        )
        dead_letters = conn.execute(
            "SELECT id, type, target_version, last_error FROM jobs "
            "WHERE status = 'failed' ORDER BY id"
        ).fetchall()
        egress_counts = conn.execute(
            "SELECT purpose, COUNT(*) FROM egress_log GROUP BY purpose ORDER BY purpose"
        ).fetchall()
    finally:
        conn.close()

    typer.echo(
        "jobs: "
        f"{job_counts.get('pending', 0)} pending, "
        f"{job_counts.get('running', 0)} running, "
        f"{job_counts.get('done', 0)} done, "
        f"{job_counts.get('failed', 0)} failed"
    )

    total_egress = sum(n for _, n in egress_counts)
    by_purpose = ", ".join(f"{purpose}: {n}" for purpose, n in egress_counts) or "none"
    typer.echo(f"egress: {total_egress} sends ({by_purpose})")

    typer.echo(f"dead-letters (failed jobs): {len(dead_letters)}")
    for job_id, job_type, target_version, last_error in dead_letters:
        typer.echo(
            f"  job {job_id} ({job_type}) target={_short(target_version)}: "
            f"{last_error or 'no error recorded'}"
        )


@app.command(name="jobs")
def jobs_(
    status: JobStatus | None = typer.Option(
        None, "--status", help="Only list jobs in this status (default: all)."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """List the derive jobs on the work queue (``jobs`` table, ``docs/storage.md``).

    One row per job — id, type, status, attempts, target version — newest last;
    a failed job also shows its last error. ``--status`` narrows the list to a
    single queue state.
    """
    conn = _open_db(db)
    try:
        if status is None:
            rows = conn.execute(
                "SELECT id, type, status, attempts, target_version, last_error "
                "FROM jobs ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, type, status, attempts, target_version, last_error "
                "FROM jobs WHERE status = ? ORDER BY id",
                (status.value,),
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("no jobs")
        return

    for job_id, job_type, job_status, attempts, target_version, last_error in rows:
        line = (
            f"{job_id}  {job_type:<7} {job_status:<8} "
            f"attempts={attempts}  target={_short(target_version)}"
        )
        if last_error:
            line += f"  ! {last_error}"
        typer.echo(line)


@app.command()
def egress(
    purpose: EgressPurpose | None = typer.Option(
        None, "--purpose", help="Only list sends of this purpose (default: all)."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """List what content has left the box for the cloud, and when (``egress_log``).

    The audit read-out over ``egress_log`` (``docs/externals.md`` "Egress log") —
    a straight answer to "what of mine has gone to the cloud, and when?". One row
    per cloud send, oldest first: id, ts, purpose, model, the version/passage ids
    sent, and which redactions were applied. ``--purpose`` narrows to ``enrich``
    or ``qa`` sends.
    """
    conn = _open_db(db)
    try:
        if purpose is None:
            rows = conn.execute(
                "SELECT id, ts, purpose, model, sent_targets, redactions "
                "FROM egress_log ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, purpose, model, sent_targets, redactions "
                "FROM egress_log WHERE purpose = ? ORDER BY id",
                (purpose.value,),
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("no egress")
        return

    for log_id, ts, log_purpose, model, sent_targets, redactions in rows:
        typer.echo(
            f"{log_id}  {ts}  {log_purpose:<7} {model:<20}  "
            f"sent: {_format_sent(sent_targets)}  "
            f"redactions: {_format_redactions(redactions)}"
        )


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
