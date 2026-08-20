"""``lode work`` -- drain the async work queue: claim, run, retry, or dead-letter each job."""

from typing import Annotated

import typer

from lode import cli
from lode.cli import _abort_on_provider_error, _DbOption, app
from lode.cli.status import _short
from lode.config import default_db_path, lance_dir
from lode.jobs_read import outstanding_jobs
from lode.llm_provider import LLMProviderError
from lode.lock import LockHeld, WorkerLock
from lode.storage import init_db


def _format_outstanding(jobs: list[tuple[int, str, str, str]]) -> str:
    """Render outstanding ``(id, type, status, target_version)`` rows for the CLI."""
    return ", ".join(
        f"{job_id} ({job_type} {status} target={_short(target_version)})"
        for job_id, job_type, status, target_version in jobs
    )


@app.command(
    help=(
        "Drain the async work queue once and exit.\n\n"
        "Run it when 'lode status' reports jobs pending or failed, to make "
        "enrichment and embeddings land now. One pass by default; --loop "
        "and --wait keep going instead, on the schedules below."
    )
)
def work(
    db: _DbOption = None,
    loop: Annotated[
        bool,
        typer.Option(
            "--loop",
            "--watch",
            help="Keep polling forever, --interval seconds apart.",
        ),
    ] = False,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            help="Polling interval in seconds.",
            min=0.1,
        ),
    ] = 5.0,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait",
            "--until-done",
            help="Block until drained; exits non-zero on timeout.",
        ),
    ] = False,
) -> None:
    """Drain the async work queue: claim, run, retry, or dead-letter each job.

    One-shot by default: acquires the single-instance lock, resets overdue
    failed jobs, then claims and runs ready pending jobs until none remain
    and exits. --loop / --watch keeps the loop alive forever instead,
    sleeping --interval seconds between passes. --wait / --until-done polls
    only until the queue is fully drained or a bounded timeout fires (see
    its own help), so you don't have to re-run this by hand to see an async
    enrich batch land.

    embed jobs run synchronously in the main loop. enrich jobs are
    submitted to a batch API ahead of that loop and collected on a later
    pass; refresh jobs have no handler yet and simply accumulate
    harmlessly. A second "lode work" while one is already running is
    refused.

    Each pass prints a per-job outcome line for what it actually produced
    (e.g. "enriched <short-id>: 4 tags, 2 entities, 3 edges, summary set",
    or "embedded <short-id>: 3 passages"), followed by a "drained N job(s)"
    summary. A one-shot run right after capture only submits the enrich
    batch (nothing to collect yet), so its outcome line appears on a later
    pass instead.

    Without --wait, if jobs are still pending or running once the pass
    ends, that is reported too -- naming each one -- so a single pass over
    a thrashing head stays visible instead of a bare "drained 0 job(s)"
    that looks like nothing happened.
    """
    if wait and loop:
        typer.echo(
            "--wait and --loop/--watch are mutually exclusive "
            "(--wait already polls until drained or timeout)",
            err=True,
        )
        raise typer.Exit(code=1)

    from lode.auth import AuthError
    from lode.embedding import FastEmbedEmbedder
    from lode.reconcile import reconcile as _reconcile
    from lode.vectorstore import VectorStore
    from lode.worker import drain as _drain

    # _resolve_settings() (not bare Settings()) so a config-file override -- e.g.
    # refresh_ttl_s -- actually reaches reconcile()'s steps and the drain loop.
    settings = cli._resolve_settings()
    db_path = db or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    # ONE embedder for this whole process (lode-j5r2). Constructed HERE, not
    # left to `drain()`'s per-call default, because that default would rebuild
    # (and reload the ONNX model on) every poll pass of the loop below.
    # Construction alone is cheap -- no model load, no network; the cost this
    # amortizes is paid lazily, on first embed -- so building it before the
    # lock, on a run that may exit on LockHeld, costs nothing.
    embedder = FastEmbedEmbedder(settings)
    # ONE VectorStore for this whole process too (lode-2brb), same reasoning:
    # every poll pass shares one opened LanceDB table instead of each embed
    # job reopening it. See VectorStore._open_or_create_table.
    store = VectorStore(lance_dir(db_path), settings)
    try:
        try:
            with WorkerLock(db_path):
                try:
                    deadline = (
                        cli.time.monotonic() + settings.work_wait_timeout_s
                        if wait
                        else None
                    )
                    while True:
                        # Reconciliation scan runs at startup (first pass) and
                        # periodically (each poll tick in --loop/--wait mode).
                        # Re-enqueues any head versions missing a fresh embed;
                        # idempotent by the live-job partial unique index
                        # (lode-i05.4). ``settings`` reaches every scan step —
                        # see reconcile.StepFn (lode-09n).
                        #
                        # ``gap`` is "gaps HANDLED", not queue depth (lode-cyly):
                        # the lexical_gap step heals inline and enqueues nothing,
                        # so a nonzero count here does not imply drain() has work.
                        # The outstanding-jobs report below is the queue signal.
                        gap = _reconcile(conn, settings)
                        if gap:
                            typer.echo(f"reconciled {gap} gap version(s)")
                        # Per-job outcome lines (lode-1gr.4): what this pass's
                        # embed jobs and any enrich batch it collected actually
                        # produced, ahead of the existing job-count summary.
                        outcomes: list[str] = []
                        n = _drain(
                            conn,
                            db_path,
                            settings,
                            outcomes=outcomes,
                            embedder=embedder,
                            store=store,
                        )
                        for outcome in outcomes:
                            typer.echo(outcome)
                        typer.echo(f"drained {n} job(s)")

                        if wait:
                            outstanding = outstanding_jobs(conn)
                            if not outstanding:
                                break
                            if cli.time.monotonic() >= deadline:
                                typer.echo(
                                    "--wait timed out after "
                                    f"{settings.work_wait_timeout_s}s with "
                                    f"{len(outstanding)} job(s) still in "
                                    f"flight: {_format_outstanding(outstanding)}",
                                    err=True,
                                )
                                raise typer.Exit(code=1)
                            cli.time.sleep(interval)
                            continue

                        # Logging parity with --wait (lode-olmi.13): --wait
                        # already surfaces outstanding jobs via the timeout
                        # message above (or by polling again next tick), so a
                        # thrashing head is visible across its passes. A
                        # one-shot pass has no "next tick" -- if reconcile()
                        # just re-enqueued jobs that this pass's drain() never
                        # got to (or can't -- e.g. no handler registered yet),
                        # the one-shot exits right after a bare "drained 0
                        # job(s)" with no sign anything is left, which is what
                        # hid the reconcile re-enqueue loop (lode-olmi.11) and
                        # the one-shot hang (lode-olmi.12) from a plain
                        # 'lode work'. Report the same outstanding-jobs detail
                        # --wait's own timeout path names, every pass, so a
                        # one-shot (or --loop) run is never silent about it.
                        outstanding = outstanding_jobs(conn)
                        if outstanding:
                            typer.echo(
                                f"{len(outstanding)} job(s) still outstanding "
                                f"after this pass: {_format_outstanding(outstanding)}"
                            )

                        if not loop:
                            break
                        cli.time.sleep(interval)
                except KeyboardInterrupt:
                    typer.echo("worker interrupted", err=True)
                except (AuthError, LLMProviderError) as err:
                    # Either arm of what drain() re-raises: an AuthError/
                    # LLMAuthError once the offending job is reset to 'pending'
                    # uncharged (lode-9yy, lode-568v.3), or the non-auth
                    # LLMProviderError it stashes from a stuck batch pre-step
                    # and re-raises at the end of the pass (lode-5zqa,
                    # lode-yx1c). Same pair, same rendering as `ask` above.
                    _abort_on_provider_error("work", err)
        except LockHeld as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None
    finally:
        conn.close()
