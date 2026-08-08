"""``lode jobs`` -- list the derive jobs on the work queue (see docs/storage.md)."""

from typing import Annotated

import typer

from lode.cli import _DbOption, _open_db, app
from lode.cli.status import JobStatus, _short
from lode.jobs_read import list_jobs


@app.command(name="jobs")
def jobs_(
    status: Annotated[
        JobStatus | None,
        typer.Option("--status", help="Only list jobs in this status (default: all)."),
    ] = None,
    db: _DbOption = None,
) -> None:
    """List the derive jobs on the work queue (see docs/storage.md).

    One row per job -- id, type, status, attempts, target version -- newest
    last; a failed job also shows its last error. --status narrows the list
    to a single queue state.
    """
    conn = _open_db(db)
    try:
        rows = list_jobs(conn, status.value if status is not None else None)
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
