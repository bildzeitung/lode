"""Worker loop: claim → run → retry/backoff → dead-letter (lode-i05.3).

ONE-SHOT DRAIN by default: acquire the advisory lock (i05.2), reset overdue
retries, then claim+run ready pending jobs until none remain and exit.
``--loop`` / ``--watch`` polls on an interval (exposed by :func:`drain` via the
``lode work`` Typer command in :mod:`lode.cli`).

**Handler registry** dispatches on ``jobs.type``:

- ``embed`` — registered now; runs the **vector-only** path
  (:func:`lode.embedding.embed`: chunk + embed + LanceDB vectors). The FTS
  lexical leg is **not here** (lode-xyb): the synchronous
  :class:`~lode.lexical.LexicalCacheBackend` in ``cli.py add`` writes
  ``passages`` + ``passages_fts`` right after the version commits.  Idempotent:
  the same head version can be re-embedded and converges to the same state.
- ``enrich`` — registered (lode-npx.1); the **fallback** handler for any
  ``enrich`` job not handled by the batch-submit pre-step. Runs
  :func:`lode.enrich.enrich_version` directly (immediate single-version Haiku
  call). In normal operation the batch pre-step claims all pending ``enrich``
  jobs before the main claim-run loop, so this handler fires only for jobs that
  escaped the batch step (e.g. a unit test injecting enrich jobs into a registry
  that skips the batch steps).
- ``refresh`` — *no handler*; accumulates harmlessly until the connectors step
  arrives (lode-i05.3 scope fence).

**Batch pre-steps (lode-npx.2)** run at the top of every :func:`drain` pass,
before the main claim-run loop:

1. :func:`_batch_collect_enrich` — find ``running`` enrich jobs that have a
   ``batch_handle``, poll each unique batch, and process results when the batch
   ends (``processing_status == 'ended'``). Succeeded results write enrichment
   to the DB and mark jobs ``done``; errored results apply backoff or
   dead-letter. Returns False for in-progress batches (tried again on next
   drain tick).
2. :func:`_batch_submit_enrich` — find **pending** enrich jobs (up to
   ``settings.enrichment_batch_flush_size``), gate out no_egress / tombstone /
   purged versions, and submit the rest to ``client.beta.messages.batches.create``
   (50%-off Batches API). Each submitted job row is updated to ``status='running'``
   with ``batch_handle`` set. On success returns the count submitted; on API
   failure marks all newly-claimed jobs ``failed`` and re-raises.

**Claim** (``_claim_one``): selects one job with
``status='pending' AND next_attempt_at <= now AND type IN (<registered>)``,
ordered by type priority (``embed > enrich``, ``docs/storage.md``:274) then
``created``, and atomically flips it to ``'running'`` with
``UPDATE … WHERE id=? AND status='pending'`` asserting rowcount=1 —
belt-and-suspenders behind the single-owner advisory lock.

**Run** (``run_one``):
- ok → ``status='done'``
- transient error → ``attempts += 1``, ``last_error`` set,
  ``status='failed'`` with
  ``next_attempt_at = now + exponential backoff`` (base/cap from
  :class:`~lode.config.Settings`); the drain loop calls
  ``_reset_retryable`` at the *start* of each pass so overdue retries are
  picked up without a separate scheduler
- max-attempts gate → ``status='dead'`` (terminal poison)

**Crash recovery** (lode-aor): if the worker (or the CLI's inline
immediate-enrich fast path) crashes mid-run, a job can be left in
``status='running'`` forever — no claim query selects ``'running'`` rows, and
reconcile's gap queries treat any non-``'dead'`` status as "not a gap", so
nothing would otherwise pick it back up. :func:`_reclaim_stale_running` closes
that gap: every :func:`drain` pass reclaims any job whose ``claimed_at`` is
older than ``settings.stale_running_timeout_s``, running it through the same
attempts/backoff/dead-letter accounting :func:`run_one` uses for a handler
failure. Applies uniformly to ``embed``, ``enrich``, and ``refresh``.
Batch-submitted enrich jobs (``batch_handle`` set) are excluded — they stay
``running`` until their batch ends by design (the batch_handle survives in the
DB for lode-i05.5 restart-resume), and reclaiming one here would risk
resubmitting a request already in flight.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lode.config import Settings, lance_dir as _lance_dir

log = logging.getLogger(__name__)

#: Handler signature: (conn, target_version, db_path, settings) -> None
#:
#: ``conn`` — open SQLite connection (same one the claim/run loop uses).
#: ``target_version`` — the version to process.
#: ``db_path`` — used to derive the LanceDB vector-store path (``lance_dir``).
#: ``settings`` — resolved settings (retry knobs, model IDs, etc.).
HandlerFn = Callable[[sqlite3.Connection, str, Path, Settings], None]

#: Module-level handler registry — maps ``jobs.type`` → handler function.
#:
#: Populated at module load by :func:`register`; the ``embed`` handler is
#: registered here. Tests inject a private ``_registry`` dict into
#: :func:`drain` / :func:`run_one` instead of touching this directly.
_REGISTRY: dict[str, HandlerFn] = {}


def register(job_type: str, handler: HandlerFn) -> None:
    """Register ``handler`` for ``job_type`` in the module-level registry."""
    _REGISTRY[job_type] = handler


def registered_types() -> tuple[str, ...]:
    """Return the job types the module-level registry can handle."""
    return tuple(_REGISTRY)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """Format ``dt`` as the schema's ISO-8601 millisecond-``Z`` timestamp.

    Matches SQLite's ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` (millisecond
    precision, e.g. ``2026-06-28T12:34:56.789Z``) so string comparisons in
    ``next_attempt_at <= ?`` clauses are chronologically correct.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _now_iso() -> str:
    """Return the current UTC time in the schema's ISO-8601 format."""
    return _iso(datetime.now(UTC))


def _backoff_next_attempt_at(new_attempts: int, settings: Settings) -> str:
    """ISO-8601 UTC timestamp for the next retry after ``new_attempts`` failures.

    Exponential backoff: ``min(base * 2^(new_attempts - 1), cap)``.

    - attempt 1 (1st failure): ``base * 1``
    - attempt 2: ``base * 2``
    - attempt 3: ``base * 4``
    - … capped at ``retry_backoff_cap_s``
    """
    delay = min(
        settings.retry_backoff_base_s * (2 ** (new_attempts - 1)),
        settings.retry_backoff_cap_s,
    )
    return _iso(datetime.now(UTC) + timedelta(seconds=delay))


def _reset_retryable(conn: sqlite3.Connection, now: str) -> int:
    """Flip ``status='failed' AND next_attempt_at <= now`` back to ``'pending'``.

    Called once at the start of each :func:`drain` pass to pick up jobs whose
    backoff window has expired since the last pass.  Returns the count of rows
    reset.

    Jobs set to ``'failed'`` *within* the current pass have a
    ``next_attempt_at`` in the future, so they are not flipped here and stay
    failed until the next pass (one-shot) or next poll tick (``--loop``).
    """
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'pending' "
            "WHERE status = 'failed' AND next_attempt_at <= ?",
            (now,),
        )
    return cur.rowcount


def _reclaim_stale_running(conn: sqlite3.Connection, settings: Settings) -> int:
    """Reclaim jobs stuck in ``status='running'`` past the staleness timeout (lode-aor).

    A worker (or the CLI's inline immediate-enrich fast path) that crashes or is
    killed between claiming a job (:func:`_claim_one`'s ``UPDATE ... SET
    status='running'``) and completing it (:func:`run_one`'s terminal ``UPDATE``)
    leaves that row permanently stuck: ``_claim_one`` only ever selects
    ``status='pending'``, and reconcile's gap queries treat any non-``'dead'``
    status (including ``'running'``) as "not a gap" — so without this step
    nothing would ever pick the row back up.

    **Selection:** ``status='running' AND batch_handle IS NULL AND (claimed_at
    IS NULL OR claimed_at <= now - settings.stale_running_timeout_s)``.
    Batch-backed enrich jobs (``batch_handle`` set) are excluded — their
    long-lived ``'running'`` status is intentional (lode-i05.5 owns their
    resume-on-restart semantics via ``_batch_collect_enrich``; reclaiming one
    here would abandon a Batches API request still in flight, or worse let it
    be resubmitted). A ``NULL`` ``claimed_at`` (a ``'running'`` row that
    predates this column, or one this migration never got a chance to stamp) is
    treated as indefinitely stale — there's no way to know its true age, and
    leaving it stuck forever is worse than reclaiming it early.

    **Reclaim:** each selected row is put through exactly the same
    attempts/backoff/dead-letter accounting :func:`run_one` uses for a
    transient handler failure — ``attempts += 1``; at ``retry_max_attempts`` →
    ``status='dead'``; otherwise → ``status='failed'`` with a backoff
    ``next_attempt_at`` (picked up by :func:`_reset_retryable` once it's due,
    same as any other retry). Reusing that machinery means a crash-reclaimed
    job obeys the identical max-attempts gate as one that failed cleanly — no
    parallel retry policy to keep in sync.

    Applies uniformly to every job ``type`` (``embed``, ``enrich``, ``refresh``)
    — the staleness signal is the same regardless of what kind of work was
    interrupted.

    Returns the count of jobs reclaimed.
    """
    cutoff = _iso(
        datetime.now(UTC) - timedelta(seconds=settings.stale_running_timeout_s)
    )
    rows = conn.execute(
        "SELECT id, attempts FROM jobs "
        "WHERE status = 'running' AND batch_handle IS NULL "
        "AND (claimed_at IS NULL OR claimed_at <= ?)",
        (cutoff,),
    ).fetchall()

    if not rows:
        return 0

    reclaimed = 0
    with conn:
        for job_id, attempts in rows:
            new_attempts = attempts + 1
            err = (
                "reclaimed: stuck in 'running' past staleness timeout (possible crash)"
            )
            if new_attempts >= settings.retry_max_attempts:
                cur = conn.execute(
                    "UPDATE jobs SET status = 'dead', attempts = ?, last_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (new_attempts, err, job_id),
                )
            else:
                next_at = _backoff_next_attempt_at(new_attempts, settings)
                cur = conn.execute(
                    "UPDATE jobs SET status = 'failed', attempts = ?, "
                    "last_error = ?, next_attempt_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (new_attempts, err, next_at, job_id),
                )
            reclaimed += cur.rowcount

    return reclaimed


def _claim_one(
    conn: sqlite3.Connection,
    types: tuple[str, ...],
    now: str,
    *,
    target_version: str | None = None,
) -> int | None:
    """Atomically claim one ready pending job of a registered type.

    Selects the highest-priority, oldest ready job — ``status='pending' AND
    next_attempt_at <= now AND type IN types`` — ordered by type priority
    (``embed`` before ``enrich``) then ``created``, and flips it to
    ``'running'`` with an asserted CAS update.  Returns the job ``id`` or
    ``None`` if nothing is ready.

    ``target_version``, if given, restricts the candidate SELECT to jobs for
    that version — this is what lets a caller claim *the specific job it just
    enqueued* rather than whatever is oldest-pending across the whole queue
    (lode-a3x). The normal worker ``drain`` loop omits it and claims across
    all live jobs, as before.
    """
    if not types:
        return None
    placeholders = ", ".join("?" for _ in types)
    params: list[object] = [now, *types]
    version_clause = ""
    if target_version is not None:
        version_clause = "AND target_version = ? "
        params.append(target_version)
    row = conn.execute(
        f"SELECT id FROM jobs "
        f"WHERE status = 'pending' AND next_attempt_at <= ? "
        f"AND type IN ({placeholders}) "
        f"{version_clause}"
        f"ORDER BY "
        f"CASE type WHEN 'embed' THEN 0 WHEN 'enrich' THEN 1 ELSE 2 END, "
        f"created "
        f"LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    job_id = row[0]
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'running', claimed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, job_id),
        )
    if cur.rowcount != 1:
        # Belt-and-suspenders: another claimer got it — shouldn't happen under
        # the single-owner advisory lock, but safe to return None and retry.
        return None
    return job_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_one(
    conn: sqlite3.Connection,
    job_id: int,
    db_path: Path,
    settings: Settings,
    registry: dict[str, HandlerFn],
) -> bool:
    """Run a single claimed job (``status='running'``).

    Reads the job row, dispatches to the registered handler, and transitions
    the status:

    - Handler succeeds → ``status='done'``
    - Handler raises, attempts < max → ``status='failed'``, backoff
      ``next_attempt_at`` set, ``last_error`` recorded
    - Handler raises, attempts == max → ``status='dead'``

    Returns ``True`` on success (``done``), ``False`` on error (``failed`` or
    ``dead``).  The registry must contain a handler for the job's type (call
    sites guarantee this because ``_claim_one`` filters to registered types).
    """
    row = conn.execute(
        "SELECT type, target_version, attempts FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        log.warning("job %d disappeared before run — skipping", job_id)
        return False

    job_type, target_version, attempts = row
    handler = registry[job_type]
    short = target_version[:12]

    try:
        handler(conn, target_version, db_path, settings)
        with conn:
            conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))
        log.info("job %d (%s target=%s) done", job_id, job_type, short)
        return True

    except Exception as exc:  # noqa: BLE001
        new_attempts = attempts + 1
        err = str(exc)
        log.warning(
            "job %d (%s target=%s) failed (attempt %d/%d): %s",
            job_id,
            job_type,
            short,
            new_attempts,
            settings.retry_max_attempts,
            err,
        )
        if new_attempts >= settings.retry_max_attempts:
            with conn:
                conn.execute(
                    "UPDATE jobs SET status = 'dead', attempts = ?, last_error = ? "
                    "WHERE id = ?",
                    (new_attempts, err, job_id),
                )
            log.error(
                "job %d dead-lettered after %d attempt(s): %s",
                job_id,
                new_attempts,
                err,
            )
        else:
            next_at = _backoff_next_attempt_at(new_attempts, settings)
            with conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', attempts = ?, "
                    "last_error = ?, next_attempt_at = ? "
                    "WHERE id = ?",
                    (new_attempts, err, next_at, job_id),
                )
        return False


def claim_and_run_one(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    types: tuple[str, ...],
    _registry: dict[str, HandlerFn] | None = None,
    *,
    target_version: str | None = None,
) -> bool:
    """Atomically claim and run one ready pending job of ``types``, if any.

    Reuses the exact same claim (:func:`_claim_one`) and run (:func:`run_one`)
    primitives the ``drain`` loop uses. This lets a caller outside the worker
    loop — the CLI's interactive immediate-enrich fast path (lode-npx.2) —
    opportunistically fast-track a job it just enqueued, with identical
    claim / backoff / dead-letter semantics and no duplicated retry logic.

    ``target_version``, if given, is passed through to :func:`_claim_one` so
    the claim is scoped to that version's job rather than the oldest pending
    job of ``types`` across the whole queue (lode-a3x) — required for a caller
    that wants to fast-track the *specific* job it just enqueued, not an
    arbitrary backlog job of the same type. The plain ``lode work`` drain loop
    omits it.

    Returns ``True`` if a job was claimed and run (regardless of whether it
    then succeeded or failed), ``False`` if there was nothing ready to claim —
    e.g. a concurrent ``lode work`` already won the claim race, or (when
    ``target_version`` is given) no live job matches that version. A caller
    should treat ``False`` as a harmless no-op: the job stays live for the
    normal worker path to pick up.

    ``_registry`` is injectable for tests (mirrors :func:`drain`); production
    callers omit it and the module-level :data:`_REGISTRY` is used.
    """
    registry = _registry if _registry is not None else _REGISTRY
    job_id = _claim_one(conn, types, _now_iso(), target_version=target_version)
    if job_id is None:
        return False
    run_one(conn, job_id, db_path, settings, registry)
    return True


def _batch_collect_enrich(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    _client: object | None = None,
) -> int:
    """Poll in-flight Batches API requests and process any that have ended.

    Finds all distinct ``batch_handle`` values on running enrich jobs, calls
    :func:`lode.enrich.collect_enrich_batch` for each, and returns the total
    count of jobs whose batch ended (results processed this pass — not all may
    have been individually ``done``, some may be ``failed`` / ``dead``).

    Batches still in progress are left untouched; they will be checked again on
    the next drain tick. This IS the resume-on-restart mechanism (lode-i05.5):
    every ``drain()`` call — including the one at worker startup, before
    :mod:`lode.cli`'s ``work`` command enters its loop — starts by re-polling
    every persisted ``batch_handle`` found in the DB, regardless of which
    process submitted it or whether that process is still running. No
    in-memory state is required to resume: the job row's ``running`` status
    plus its ``batch_handle`` is the only durable record needed, and this step
    never calls ``batches.create`` — only ``retrieve``/``results`` — so a
    restart can never resubmit.

    ``_client`` is injectable for tests.
    """
    from lode.enrich import collect_enrich_batch

    batch_ids: list[str] = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT batch_handle FROM jobs "
            "WHERE type = 'enrich' AND status = 'running' AND batch_handle IS NOT NULL"
        ).fetchall()
    ]

    if not batch_ids:
        return 0

    kwargs: dict = {}
    if _client is not None:
        kwargs["client"] = _client

    ended = 0
    for batch_id in batch_ids:
        if collect_enrich_batch(conn, batch_id, settings, **kwargs):
            ended += 1

    return ended


def _batch_submit_enrich(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    _client: object | None = None,
) -> int:
    """Claim pending enrich jobs and submit them to the Batches API (50% off).

    Finds up to ``settings.enrichment_batch_flush_size`` pending enrich jobs,
    claims each with an asserted CAS (``UPDATE ... WHERE status='pending'``,
    rowcount == 1) so a job the interactive immediate-enrich already grabbed is
    dropped rather than double-submitted, then calls
    :func:`lode.enrich.submit_enrich_batch` with only the jobs actually claimed.

    On API success: the batch handle is stored on each submitted job row (by
    :func:`lode.enrich.submit_enrich_batch`); gated-out jobs are marked ``done``;
    count of submitted requests is returned.

    On API failure: all newly-claimed jobs are reverted to ``failed`` with a
    short backoff so they are retried on the next drain tick, then the exception
    is re-raised (logged at WARNING — the embed drain continues).

    Returns the number of jobs included in the submitted batch (0 if no pending
    enrich jobs or all gated out).

    ``_client`` is injectable for tests.
    """
    from lode.enrich import submit_enrich_batch

    flush_size = settings.enrichment_batch_flush_size
    rows = conn.execute(
        "SELECT id, target_version FROM jobs "
        "WHERE type = 'enrich' AND status = 'pending' AND next_attempt_at <= ? "
        "ORDER BY created "
        "LIMIT ?",
        (_now_iso(), flush_size),
    ).fetchall()

    if not rows:
        return 0

    # Pre-claim each job with an asserted CAS (rowcount == 1), exactly as
    # _claim_one does, and submit ONLY the jobs this step actually won. The
    # interactive `lode add` immediate-enrich (claim_and_run_one) runs without
    # the worker lock and can flip one of these rows 'pending' -> 'running'
    # between the SELECT above and here; passing the raw SELECT to
    # submit_enrich_batch would then submit a job already claimed (and possibly
    # already run) elsewhere -- a double API spend. A lost CAS (rowcount 0)
    # means someone else owns it, so we drop it from this batch.
    claimed_rows: list[tuple[int, str]] = []
    with conn:
        for job_id, version_id in rows:
            cur = conn.execute(
                "UPDATE jobs SET status = 'running' "
                "WHERE id = ? AND status = 'pending'",
                (job_id,),
            )
            if cur.rowcount == 1:
                claimed_rows.append((job_id, version_id))

    if not claimed_rows:
        return 0

    # submit_enrich_batch persists batch_handle on these rows; on failure we
    # revert exactly the jobs we claimed here (not a concurrent claimer's).
    job_ids = [jid for jid, _ in claimed_rows]

    kwargs: dict = {}
    if _client is not None:
        kwargs["client"] = _client

    try:
        batch_id = submit_enrich_batch(conn, claimed_rows, settings, **kwargs)
        submitted = sum(
            1
            for row in conn.execute(
                "SELECT id FROM jobs WHERE id IN ({}) AND batch_handle IS NOT NULL".format(
                    ",".join("?" * len(job_ids))
                ),
                job_ids,
            ).fetchall()
        )
        if batch_id:
            log.info(
                "_batch_submit_enrich: submitted %d job(s) as batch=%s",
                submitted,
                batch_id,
            )
        return submitted

    except Exception as exc:
        log.warning("_batch_submit_enrich: API call failed: %s — reverting jobs", exc)
        # Revert all pre-claimed jobs to 'failed' with a short backoff so they
        # are retried on the next pass (not immediately — avoids hammering the API).
        delay = min(settings.retry_backoff_base_s, settings.retry_backoff_cap_s)
        next_at = _iso(datetime.now(UTC) + timedelta(seconds=delay))
        with conn:
            conn.executemany(
                "UPDATE jobs SET status = 'failed', last_error = ?, "
                "next_attempt_at = ? WHERE id = ? AND status = 'running'",
                [(str(exc), next_at, jid) for jid in job_ids],
            )
        return 0


def drain(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings | None = None,
    _registry: dict[str, HandlerFn] | None = None,
    *,
    _batch_client: object | None = None,
) -> int:
    """Claim+run all ready pending jobs until none remain.

    **Batch pre-steps** (lode-npx.2) run before the main claim-run loop:

    1. :func:`_batch_collect_enrich` — poll any in-flight Batches API requests
       and process results for batches that have ended.
    2. :func:`_batch_submit_enrich` — find pending ``enrich`` jobs and submit
       them to the Batches API (up to ``settings.enrichment_batch_flush_size``).

    Then: calls :func:`_reclaim_stale_running` (lode-aor) to dead-letter/retry
    any job left ``'running'`` past ``settings.stale_running_timeout_s`` by a
    crash, :func:`_reset_retryable` to pick up overdue retries
    (``status='failed' AND next_attempt_at <= now``), and loops
    :func:`_claim_one` → :func:`run_one` until nothing is claimable (``embed``
    and any residual ``enrich`` jobs not claimed by the batch step).

    Returns the total number of jobs claimed and run by the **main loop**
    (including failures and dead-letters). Batch pre-step and reclaim activity
    is logged but not included in the return count.

    ``_registry`` is injectable for tests; production callers omit it and the
    module-level :data:`_REGISTRY` is used. ``_batch_client`` is injectable for
    tests (passed through to the batch pre-steps).
    """
    settings = settings or Settings()
    registry = _registry if _registry is not None else _REGISTRY
    types = tuple(registry)

    # Batch pre-steps: collect in-flight batches, then submit pending enrich jobs.
    _batch_collect_enrich(conn, settings, _client=_batch_client)
    _batch_submit_enrich(conn, settings, _client=_batch_client)

    reclaimed = _reclaim_stale_running(conn, settings)
    if reclaimed:
        log.warning("reclaimed %d stale 'running' job(s) (possible crash)", reclaimed)

    now = _now_iso()
    reset = _reset_retryable(conn, now)
    if reset:
        log.debug("reset %d overdue failed job(s) to pending", reset)

    processed = 0
    while True:
        now = _now_iso()
        job_id = _claim_one(conn, types, now)
        if job_id is None:
            break
        run_one(conn, job_id, db_path, settings, registry)
        processed += 1

    return processed


# ---------------------------------------------------------------------------
# Embed handler (registered at module load)
# ---------------------------------------------------------------------------


def _embed_handler(
    conn: sqlite3.Connection,
    target_version: str,
    db_path: Path,
    settings: Settings,
) -> None:
    """Embed handler: vector leg only (lode-x6r.5, lode-xyb).

    The sole path for async embedding after capture: capture enqueues the
    ``embed`` derive job via :func:`~lode.jobs.enqueue_derive_jobs`; this
    handler drains it.  Deferred imports avoid paying the fastembed / LanceDB
    cost on commands that never embed.

    :func:`lode.embedding.embed` chunks the body, upserts the ``passages``
    rows, embeds each passage via the pinned local ONNX model, and replaces
    the version's vectors in LanceDB.

    The FTS leg is **not here** (lode-xyb): the synchronous
    :class:`~lode.lexical.LexicalCacheBackend` injected into ``cli.py add``
    writes ``passages`` + ``passages_fts`` right after the version commits, so
    the note is keyword-findable before this handler runs.  The handler running
    again would just re-write identical FTS rows (idempotent), but dropping it
    is cleaner and keeps this handler model-bearing-only.
    """
    from lode.embedding import embed

    # Vector leg: chunk + embed + persist passage rows + store vectors in LanceDB.
    embed(conn, target_version, lance_dir=_lance_dir(db_path), settings=settings)


# Register the embed handler on module load.
register("embed", _embed_handler)


# ---------------------------------------------------------------------------
# Enrich handler (registered at module load — lode-npx.1)
# ---------------------------------------------------------------------------


def _enrich_handler(
    conn: sqlite3.Connection,
    target_version: str,
    db_path: Path,
    settings: Settings,
) -> None:
    """Enrich handler: Haiku structured extraction of tags/entities/edges.

    Dispatches to :func:`lode.enrich.enrich_version` which:
    - gates on ``no_egress`` / tombstone / purged (returns without error),
    - redacts secrets before egress,
    - calls Claude Haiku with structured outputs (tool-use + Pydantic),
    - writes tags/entities to ``annotations`` and inferred edges to ``edges``
      (all ``source='ai'``), and
    - audits the egress in ``egress_log``.

    Deferred import keeps the Haiku / Anthropic SDK cost off code paths that
    never enrich.  The ``db_path`` parameter is accepted but unused: enrichment
    writes only to the SQLite DB, not to the LanceDB vector store.
    """
    from lode.enrich import enrich_version

    enrich_version(conn, target_version, settings)


# Register the enrich handler on module load.
register("enrich", _enrich_handler)
