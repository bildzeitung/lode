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
- ``refresh`` — registered (lode-w0h.3); the shared fetch->ingest handler for
  the web draw-down connector. Dispatches to
  :func:`lode.drawdown.refresh_external`, which fetches the job's
  ``target_version`` (itself a canonical, directly-fetchable URL — a web
  ``external_id`` *is* its canonical form) and ingests the result as a
  mirrored snapshot. A :class:`~lode.webfetch.TransientFetchError` needs no
  special-casing here: it is an ``Exception`` like any other, so
  :func:`run_one`'s existing attempts/backoff/dead-letter accounting already
  covers it. The paste-triggered initial draw-down
  (:func:`lode.drawdown.detect_and_enqueue_drawdown`, called from
  :meth:`lode.repository.Repository.save`) enqueues the *first* ``refresh``
  job for a source; ``lode-w0h.6``'s later refresh policy reuses this same
  handler unchanged and adds only staleness/scheduling on top. Also registers
  a **dead-letter hook** (:func:`_refresh_dead_letter_hook`, lode-at8, see
  "Dead-letter hook" below) — a ``refresh`` job exhausting its retries writes
  a tombstone snapshot rather than leaving the external's ``head_snapshot_id``
  permanently ``NULL``.

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
   with ``batch_handle`` set. On success returns the count submitted; on a
   **transient** API failure marks all newly-claimed jobs ``failed`` (short
   backoff, not re-raised — the embed drain continues) and returns 0. On a
   **permanent** failure (:class:`lode.auth.AuthError` — lode-9yy) resets them
   to ``pending`` uncharged instead and re-raises, same taxonomy as
   :func:`run_one` (``docs/storage.md`` "Transient vs. permanent job
   failures").

**Claim** (``_claim_one``): selects one job with
``status='pending' AND next_attempt_at <= now AND type IN (<registered>)``,
ordered by type priority (``embed > enrich``, ``docs/storage.md``:274) then
``created``, and atomically flips it to ``'running'`` with
``UPDATE … WHERE id=? AND status='pending'`` asserting rowcount=1 —
belt-and-suspenders behind the single-owner advisory lock.

**Run** (``run_one``):
- ok → ``status='done'``; for ``type='enrich'`` this also stamps
  ``prompt_ver`` to the current :data:`lode.enrich.ENRICH_PROMPT_VER` on the
  same row (lode-q47) — this is the "worker/reconciliation pass" the
  :func:`lode.jobs.enqueue_derive_jobs` docstring promises would stamp it;
  before lode-q47 nothing ever did, so a ``done`` enrich job's ``prompt_ver``
  stayed permanently NULL and :mod:`lode.reconcile`'s enrich-gap step had to
  fall back to inspecting the ``summary`` annotation instead (a signal that
  broke for a legitimately empty summary). ``embed`` jobs are unaffected —
  their ``prompt_ver`` stays NULL per the schema's job-identity design
  (``docs/storage.md`` §"Schema decisions").
- transient error → ``attempts += 1``, ``last_error`` set,
  ``status='failed'`` with
  ``next_attempt_at = now + exponential backoff`` (base/cap from
  :class:`~lode.config.Settings`); the drain loop calls
  ``_reset_retryable`` at the *start* of each pass so overdue retries are
  picked up without a separate scheduler
- max-attempts gate → ``status='dead'`` (terminal poison)

**Dead-letter hook (lode-at8):** immediately after *either* dead-letter gate
above commits (:func:`run_one`'s max-attempts gate, or
:func:`_reclaim_stale_running`'s crash-reclaim gate), :func:`_run_dead_letter_hook`
invokes whatever hook :func:`register_dead_letter` registered for that job's
``type`` — a no-op if none was. Only ``refresh`` registers one today
(:func:`_refresh_dead_letter_hook`): it tombstones the external
(:func:`lode.externals.ingest_snapshot`, ``status='tombstone'``) so a
permanently-failed draw-down no longer looks identical, at the schema level,
to one still in flight (docs/externals.md "Draw-down rules" already documented
this as "on dead, the caller writes a tombstone snapshot" — this hook is that
caller). ``embed``/``enrich`` register no hook: neither ticket that touched
their dead-letter observability (lode-bvg) needed a write-side fix, only a
corrected *read* of the existing three-value ``enrichment_state``. The hook
runs as its **own, separate transaction**, sequentially *after* the
dead-status UPDATE's transaction has already committed — never nested in the
same ``with conn:`` — mirroring this codebase's established "sequential, not
nested" composition of standalone-transactional functions (e.g.
:func:`lode.drawdown.refresh_external`'s own ingest-then-repoint sequence). A
crash between the two commits leaves a job ``'dead'`` with no dead-letter
side effect recorded yet — a narrow, accepted gap (the job row itself already
carries the diagnostic in ``last_error``; nothing sweeps this gap today). See
``docs/decisions.md`` for the (a)-vs-(b) mechanism choice and rationale.

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
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lode.config import Settings, lance_dir as _lance_dir
from lode.ids import short_version_id

log = logging.getLogger(__name__)

#: Handler signature: (conn, target_version, db_path, settings) -> str | None
#:
#: ``conn`` — open SQLite connection (same one the claim/run loop uses).
#: ``target_version`` — the version to process.
#: ``db_path`` — used to derive the LanceDB vector-store path (``lance_dir``).
#: ``settings`` — resolved settings (retry knobs, model IDs, etc.).
#:
#: Return value (lode-1gr.4): an optional one-line human-readable outcome
#: summary (e.g. ``"embedded <short-id>: 3 passages"``), or ``None`` if there
#: is nothing to report (e.g. the job was gated/skipped). :func:`run_one`
#: appends a non-``None`` return to its ``outcomes`` sink when given one, so
#: ``lode work`` can echo per-job outcomes instead of relying on log lines.
HandlerFn = Callable[[sqlite3.Connection, str, Path, Settings], str | None]

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


#: Dead-letter hook signature: (conn, target_version, last_error, settings) -> None
#:
#: Invoked once, immediately after a job of the registered ``type`` reaches
#: the terminal ``'dead'`` status — never on a transient ``'failed'`` that
#: still has a retry coming. See the module docstring's "Dead-letter hook"
#: section for the transaction-composition contract (own transaction,
#: sequential not nested).
DeadLetterFn = Callable[[sqlite3.Connection, str, str, Settings], None]

#: Module-level dead-letter hook registry — maps ``jobs.type`` → hook.
#:
#: Distinct from :data:`_REGISTRY` (the run handlers): a job type can be
#: fully functional with no dead-letter hook registered (``embed``/``enrich``
#: register none today). Populated at module load by :func:`register_dead_letter`.
_DEAD_LETTER_HOOKS: dict[str, DeadLetterFn] = {}


def register_dead_letter(job_type: str, hook: DeadLetterFn) -> None:
    """Register ``hook`` to run once when a ``job_type`` job reaches ``'dead'``."""
    _DEAD_LETTER_HOOKS[job_type] = hook


def _run_dead_letter_hook(
    conn: sqlite3.Connection,
    job_type: str,
    target_version: str,
    last_error: str,
    settings: Settings,
) -> None:
    """Invoke ``job_type``'s registered dead-letter hook, if any (no-op otherwise).

    Best-effort: the job's status is already durably committed to ``'dead'``
    before this runs (:func:`run_one`'s max-attempts gate, or
    :func:`_reclaim_stale_running`'s crash-reclaim gate), so a hook that raises
    must never propagate and abort the drain loop — nor bubble out of the
    interactive ``lode add`` immediate-enrich path, which calls
    :func:`run_one` directly with no wrapping ``try``. A failed hook degrades
    to exactly the narrow, already-accepted gap this module documents (the
    external's tombstone is simply not written yet, its diagnostic still on the
    job row's ``last_error``) rather than taking down the worker. The failure
    is logged at ``error`` level so it stays observable.
    """
    hook = _DEAD_LETTER_HOOKS.get(job_type)
    if hook is None:
        return
    try:
        hook(conn, target_version, last_error, settings)
    except Exception:  # noqa: BLE001
        log.exception(
            "dead-letter hook for job type %r (target=%s) failed; job remains "
            "'dead' but its dead-letter side effect was not recorded",
            job_type,
            target_version,
        )


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


# Wall-clock time corresponding to ``time.monotonic() == 0``. Ratcheted
# forward-only, so :func:`_now` can never hand back a decreasing reading.
_now_epoch: datetime = datetime.min.replace(tzinfo=UTC)


def _now() -> datetime:
    """Return the current UTC time, with two guarantees this module depends on.

    1. **Never decreases within this process.** The OS may step
       ``CLOCK_REALTIME`` (what ``datetime.now(UTC)`` reads) *backward* — NTP
       correction, or hypervisor catch-up after the guest was descheduled. A
       backward step is read by :func:`_claim_one`'s ``next_attempt_at <= now``
       predicate as "nothing is ready yet", and :func:`drain`'s loop breaks on
       the first miss — stranding an already-eligible job for the rest of the
       pass.
    2. **Never reads *behind* ``CLOCK_REALTIME``.** Not every timestamp this
       clock is compared against comes *from* it: ``jobs.next_attempt_at``
       defaults to SQLite's own ``strftime('now')`` (``schema.sql``), and a job
       is typically enqueued by one process and claimed by another. A clock that
       merely never went backward would lag those writers permanently after a
       *forward* step and strand jobs just the same — trading one bug for a
       worse one.

    Both fall out of ratcheting the monotonic epoch forward only: the epoch
    never decreases and neither does ``elapsed``, giving (1); the epoch is
    always ``>= wall - elapsed``, giving (2). Absorbing a backward step means
    running slightly ahead of true time until the wall clock catches up — the
    right trade, since a job retried a hair late beats a job stranded.

    The version-chain twin of this hazard, and the rule it implies, are in
    ``docs/storage.md``; the repro is in lode-t1y.
    """
    global _now_epoch
    elapsed = timedelta(seconds=time.monotonic())
    _now_epoch = max(_now_epoch, datetime.now(UTC) - elapsed)
    return _now_epoch + elapsed


def _now_iso() -> str:
    """Return :func:`_now` in the schema's ISO-8601 format."""
    return _iso(_now())


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
    return _iso(_now() + timedelta(seconds=delay))


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
    interrupted. A job reclaimed straight to ``'dead'`` (attempts already
    exhausted) runs the same dead-letter hook (lode-at8, module docstring)
    :func:`run_one` does — invoked *after* this function's own ``with conn:``
    batch has committed, so a crash mid-reclaim never leaves a hook call
    racing an uncommitted status write.

    Returns the count of jobs reclaimed.
    """
    cutoff = _iso(_now() - timedelta(seconds=settings.stale_running_timeout_s))
    rows = conn.execute(
        "SELECT id, attempts, type, target_version FROM jobs "
        "WHERE status = 'running' AND batch_handle IS NULL "
        "AND (claimed_at IS NULL OR claimed_at <= ?)",
        (cutoff,),
    ).fetchall()

    if not rows:
        return 0

    reclaimed = 0
    newly_dead: list[tuple[str, str]] = []  # (job_type, target_version)
    err = "reclaimed: stuck in 'running' past staleness timeout (possible crash)"
    with conn:
        for job_id, attempts, job_type, target_version in rows:
            new_attempts = attempts + 1
            if new_attempts >= settings.retry_max_attempts:
                cur = conn.execute(
                    "UPDATE jobs SET status = 'dead', attempts = ?, last_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (new_attempts, err, job_id),
                )
                if cur.rowcount:
                    newly_dead.append((job_type, target_version))
            else:
                next_at = _backoff_next_attempt_at(new_attempts, settings)
                cur = conn.execute(
                    "UPDATE jobs SET status = 'failed', attempts = ?, "
                    "last_error = ?, next_attempt_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (new_attempts, err, next_at, job_id),
                )
            reclaimed += cur.rowcount

    for job_type, target_version in newly_dead:
        _run_dead_letter_hook(conn, job_type, target_version, err, settings)

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
    (``embed`` before ``enrich``) then ``id`` (insertion order), and flips it to
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
        # Tie-break by ``id``, not ``created``: ``jobs.id`` is INTEGER PRIMARY
        # KEY (a rowid alias), so it *is* insertion order and cannot go backward
        # the way the wall-clock ``created`` can (docs/storage.md).
        f"ORDER BY "
        f"CASE type WHEN 'embed' THEN 0 WHEN 'enrich' THEN 1 ELSE 2 END, "
        f"id "
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
    *,
    outcomes: list[str] | None = None,
) -> bool:
    """Run a single claimed job (``status='running'``).

    Reads the job row, dispatches to the registered handler, and transitions
    the status:

    - Handler succeeds → ``status='done'``. For ``type='enrich'`` the same
      UPDATE also stamps ``prompt_ver`` to the current
      :data:`lode.enrich.ENRICH_PROMPT_VER` (lode-q47) — this is the "job
      identity" half of the ``(type, target_version, prompt_ver)`` key
      ``docs/storage.md`` documents; :mod:`lode.reconcile`'s enrich-gap step
      reads it back to decide whether a ``done`` job is current. ``embed``
      jobs are untouched — their ``prompt_ver`` stays NULL by design. When
      ``outcomes`` is given and the handler returned a non-``None`` string
      (lode-1gr.4), that string is appended — this is the channel
      ``lode work`` uses to echo a per-job outcome line (e.g. ``"embedded
      <short-id>: 3 passages"``) for jobs processed by the main claim/run
      loop, i.e. ``embed`` and any fallback ``enrich`` job that escaped the
      batch pre-step.
    - Handler raises a **transient** error, attempts < max → ``status='failed'``,
      backoff ``next_attempt_at`` set, ``last_error`` recorded
    - Handler raises a **transient** error, attempts == max → ``status='dead'``
    - Handler raises a **permanent, user-actionable** error (currently only
      :class:`lode.auth.AuthError` — see ``docs/storage.md`` "Transient vs.
      permanent job failures", lode-9yy) → none of the above: the job is reset
      straight back to ``status='pending'`` with ``attempts`` untouched (no
      backoff, never ``'dead'``) and the exception is **re-raised** rather than
      absorbed, so it reaches the caller with its actionable message instead of
      being silently retried forever on something retrying can never fix.

    Returns ``True`` on success (``done``), ``False`` on a transient error
    (``failed`` or ``dead``); raises on a permanent error (see above).  The
    registry must contain a handler for the job's type (call sites guarantee
    this because ``_claim_one`` filters to registered types).
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
    short = short_version_id(target_version)

    try:
        outcome = handler(conn, target_version, db_path, settings)
        with conn:
            if job_type == "enrich":
                # Deferred import: only paid when an enrich job actually runs
                # (mirrors the deferred `from lode.enrich import ...` in
                # _enrich_handler below), keeping the Anthropic SDK import off
                # embed-only code paths.
                from lode.enrich import ENRICH_PROMPT_VER

                conn.execute(
                    "UPDATE jobs SET status = 'done', prompt_ver = ? WHERE id = ?",
                    (ENRICH_PROMPT_VER, job_id),
                )
            else:
                conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))
        log.info("job %d (%s target=%s) done", job_id, job_type, short)
        if outcome is not None and outcomes is not None:
            outcomes.append(outcome)
        return True

    except Exception as exc:  # noqa: BLE001
        # Deferred import, same discipline as the `enrich`-only import above:
        # only paid on a failure, and `lode.auth` (which pulls in `anthropic`)
        # is already loaded by then whenever `exc` actually is an AuthError —
        # it can only have been raised by `lode.auth.build_client`.
        from lode.auth import AuthError

        if isinstance(exc, AuthError):
            # Permanent, user-actionable failure (lode-9yy, docs/storage.md
            # "Transient vs. permanent job failures") — retrying can never
            # succeed, so this must NOT fall into the transient accounting
            # below: no attempts charged, no backoff, never dead-lettered.
            # Reset the claim to 'pending' (uncharged) and let the caller see
            # build_client's actionable message directly.
            with conn:
                conn.execute(
                    "UPDATE jobs SET status = 'pending', last_error = ? WHERE id = ?",
                    (str(exc), job_id),
                )
            log.error(
                "job %d (%s target=%s) hit a permanent, user-actionable "
                "failure — reset to 'pending', no retry charged: %s",
                job_id,
                job_type,
                short,
                exc,
            )
            raise

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
            # Own, separate transaction — after the dead-status UPDATE above
            # has committed, never nested in the same `with conn:` (lode-at8,
            # module docstring "Dead-letter hook").
            _run_dead_letter_hook(conn, job_type, target_version, err, settings)
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
    outcomes: list[str] | None = None,
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

    ``_client`` is injectable for tests. ``outcomes``, if given, is passed
    through to :func:`lode.enrich.collect_enrich_batch` (lode-1gr.4) so a
    drain pass that collects a completed batch appends a per-note outcome
    line for each succeeded result — the batch pre-step runs ahead of
    :func:`drain`'s main claim/run loop, so this is the only channel that
    surfaces those outcomes to the caller.
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
        if collect_enrich_batch(conn, batch_id, settings, outcomes=outcomes, **kwargs):
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
    rowcount == 1), stamping ``claimed_at`` exactly as :func:`_claim_one` does
    (lode-uhu) so a crash between the claim and :func:`lode.enrich.submit_enrich_batch`'s
    ``batch_handle`` persist leaves the row a real staleness window instead of
    being treated as immediately stale by :func:`_reclaim_stale_running`. A job
    the interactive immediate-enrich already grabbed is dropped rather than
    double-submitted, then :func:`lode.enrich.submit_enrich_batch` is called
    with only the jobs actually claimed.

    On API success: the batch handle is stored on each submitted job row (by
    :func:`lode.enrich.submit_enrich_batch`); gated-out jobs are marked ``done``;
    count of submitted requests is returned.

    On a **transient** API failure: all newly-claimed jobs are reverted to
    ``failed`` with a short backoff so they are retried on the next drain tick
    (logged at WARNING); the exception is *not* re-raised — the embed drain
    continues. On a **permanent, user-actionable** failure
    (:class:`lode.auth.AuthError` from :func:`lode.auth.build_client` —
    lode-9yy) the newly-claimed jobs are instead reset to ``pending`` with
    ``attempts`` untouched (no backoff charged, never dead-lettered), and the
    exception *is* re-raised — retrying can never fix a missing credential, so
    the caller (``lode work``) must see it immediately rather than have it
    silently absorbed into ordinary retry accounting (``docs/storage.md``
    "Transient vs. permanent job failures").

    Returns the number of jobs included in the submitted batch (0 if no pending
    enrich jobs or all gated out).

    ``_client`` is injectable for tests.
    """
    from lode.enrich import submit_enrich_batch

    flush_size = settings.enrichment_batch_flush_size
    rows = conn.execute(
        # ORDER BY id, not ``created``: ``jobs.id`` is INTEGER PRIMARY KEY (a
        # rowid alias), so it *is* insertion order, and unlike the wall-clock
        # ``created`` it cannot go backward (same rule as the version chain --
        # docs/storage.md). With LIMIT, a mis-ordered ``created`` would not just
        # reorder the batch but silently drop an older job out of it.
        "SELECT id, target_version FROM jobs "
        "WHERE type = 'enrich' AND status = 'pending' AND next_attempt_at <= ? "
        "ORDER BY id "
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
    now = _now_iso()
    with conn:
        for job_id, version_id in rows:
            cur = conn.execute(
                "UPDATE jobs SET status = 'running', claimed_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now, job_id),
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
        # Deferred import, same discipline as run_one's own AuthError check:
        # only paid on a failure, and `anthropic` is already loaded by then
        # whenever `exc` actually is an AuthError (it can only have been
        # raised by build_client, called just above).
        from lode.auth import AuthError

        if isinstance(exc, AuthError):
            # Permanent, user-actionable failure (lode-9yy, docs/storage.md
            # "Transient vs. permanent job failures") — retrying can never
            # succeed, so this must NOT be folded into the transient revert
            # below: no attempts charged, no backoff, never dead-lettered.
            # Reset the pre-claimed jobs to 'pending' (uncharged) and let the
            # caller (lode work) see build_client's actionable message.
            log.error(
                "_batch_submit_enrich: permanent, user-actionable failure — "
                "not retried, resetting %d job(s) to pending: %s",
                len(job_ids),
                exc,
            )
            with conn:
                conn.executemany(
                    "UPDATE jobs SET status = 'pending', last_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    [(str(exc), jid) for jid in job_ids],
                )
            raise

        log.warning("_batch_submit_enrich: API call failed: %s — reverting jobs", exc)
        # Revert all pre-claimed jobs to 'failed' with a short backoff so they
        # are retried on the next pass (not immediately — avoids hammering the API).
        delay = min(settings.retry_backoff_base_s, settings.retry_backoff_cap_s)
        next_at = _iso(_now() + timedelta(seconds=delay))
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
    outcomes: list[str] | None = None,
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

    ``outcomes`` (lode-1gr.4), if given, is a mutable list that this call
    appends human-readable per-job outcome lines to — from both channels: the
    batch pre-step (:func:`_batch_collect_enrich`, a *later* pass collecting a
    completed enrich batch) and the main loop (via :func:`run_one`, e.g.
    ``embed`` jobs). Left ``None`` (the default), behavior is unchanged from
    before lode-1gr.4 — this is purely additive so existing callers (and the
    ``int`` return contract) are unaffected.
    """
    settings = settings or Settings()
    registry = _registry if _registry is not None else _REGISTRY
    types = tuple(registry)

    # Batch pre-steps: collect in-flight batches, then submit pending enrich jobs.
    _batch_collect_enrich(conn, settings, _client=_batch_client, outcomes=outcomes)
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
        run_one(conn, job_id, db_path, settings, registry, outcomes=outcomes)
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
) -> str | None:
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

    **Post-embed re-enrich gate for a snapshot target (lode-w0h.5):** after
    the vector leg above, :func:`lode.externals.gate_reenrich` is called
    unconditionally on the same ``target_version``. It is a no-op (returns
    ``None``) for a note ``version_id`` — this handler runs for both note
    versions and external snapshots polymorphically (:func:`lode.embedding.
    embed`'s ``_version_body`` already resolves either), and the gate itself
    checks whether ``target_version`` is a live snapshot before doing
    anything. For a snapshot, it decides — now that this snapshot's own
    vectors exist — whether the change is material enough to enqueue an
    ``enrich`` job, or should instead carry the predecessor's enrichment
    forward. See that function's docstring for the full decision.

    Returns a one-line human-readable outcome (lode-1gr.4), e.g. ``"embedded
    <short-id>: 3 passages"``, optionally suffixed with the gate's own outcome
    line for a snapshot target, for :func:`run_one` to surface to ``lode
    work``'s echo.
    """
    from lode.embedding import embed
    from lode.externals import gate_reenrich

    # Vector leg: chunk + embed + persist passage rows + store vectors in LanceDB.
    count = embed(
        conn, target_version, lance_dir=_lance_dir(db_path), settings=settings
    )
    outcome = f"embedded {short_version_id(target_version)}: {count} passages"

    gate_outcome = gate_reenrich(
        conn, target_version, lance_dir=_lance_dir(db_path), settings=settings
    )
    if gate_outcome is not None:
        outcome = f"{outcome}; {gate_outcome}"
    return outcome


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
) -> str | None:
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

    Returns a one-line human-readable outcome (lode-1gr.4) via
    :func:`lode.enrich.format_enrich_outcome` when enrichment actually ran, or
    ``None`` when :func:`~lode.enrich.enrich_version` skipped the version
    (``no_egress`` / tombstone / purged) — this is the fallback handler for an
    enrich job that escaped the batch pre-step (see module docstring); the
    normal production path collects outcomes via
    :func:`lode.enrich.collect_enrich_batch` instead.
    """
    from lode.enrich import enrich_version, format_enrich_outcome

    result = enrich_version(conn, target_version, settings)
    if result is None:
        return None
    return format_enrich_outcome(target_version, result)


# Register the enrich handler on module load.
register("enrich", _enrich_handler)


# ---------------------------------------------------------------------------
# Refresh handler (registered at module load — lode-w0h.3)
# ---------------------------------------------------------------------------


def _refresh_handler(
    conn: sqlite3.Connection,
    target_version: str,
    db_path: Path,
    settings: Settings,
) -> str | None:
    """Refresh handler: fetch + ingest a web draw-down source (lode-w0h.3).

    Dispatches to :func:`lode.drawdown.refresh_external`, passing no
    ``fetcher`` override — production always resolves to the real
    :class:`~lode.webfetch.HttpxFetcher` (:func:`lode.webfetch.
    fetch_and_extract`'s own default when none is given). ``target_version``
    is the job's canonical ``external_id`` (a directly-fetchable URL, not a
    note version — the ``jobs.target_version`` column is a polymorphic
    string, not FK'd to ``versions``). Deferred import mirrors
    :func:`_embed_handler`/:func:`_enrich_handler`: the ``httpx``/
    ``trafilatura`` cost stays off code paths that never draw down a URL.

    The ``db_path`` parameter is accepted but unused, like the enrich
    handler's: draw-down writes only to the SQLite DB (``externals`` /
    ``snapshots``), never to the LanceDB vector store directly (the
    resulting ``embed`` job — enqueued by :func:`lode.externals.
    ingest_snapshot` for an ``ok`` snapshot — is what reaches LanceDB).
    """
    from lode.drawdown import refresh_external

    return refresh_external(conn, target_version, settings)


# Register the refresh handler on module load.
register("refresh", _refresh_handler)


# ---------------------------------------------------------------------------
# Refresh dead-letter hook (registered at module load — lode-at8)
# ---------------------------------------------------------------------------


def _refresh_dead_letter_hook(
    conn: sqlite3.Connection,
    target_external_id: str,
    last_error: str,
    settings: Settings,
) -> None:
    """Tombstone ``target_external_id`` when its 'refresh' job dead-letters (lode-at8).

    Closes the gap :mod:`lode.webfetch` and ``docs/externals.md`` already
    documented but nothing implemented: a TRANSIENT fetch failure
    (408/429/5xx/network) is not written as a snapshot by the fetch unit
    itself — it rides this module's existing attempts/backoff/dead-letter
    machinery, and "on dead, the caller writes a tombstone snapshot so the
    note edge still resolves" (docs/externals.md "Fetch-outcome taxonomy").
    This hook is that caller.

    Writes via :func:`lode.externals.ingest_snapshot` under the exact same
    ``status='tombstone'`` convention a PERMANENT (non-retrying) fetch
    failure already uses (:func:`lode.externals.tombstone_body`), so a URL
    that dies after exhausting retries is indistinguishable, at the schema
    level, from one that failed permanently on its very first fetch — both
    leave ``externals.head_snapshot_id`` pointing at a tombstone row rather
    than ``NULL``. The tombstone body folds in ``last_error`` (the job's own
    diagnostic, already persisted on the job row) so the record satisfies the
    ticket's "carrying the failure and its last error" acceptance criterion
    without a schema change — no new column, no new table.

    If ``target_external_id`` already has an ``ok`` head snapshot (i.e. a
    *later* refresh — ``lode-w0h.6``'s staleness policy, not the
    paste-triggered first draw-down — is the one that exhausted retries),
    this still moves the head to a tombstone: the dead-letter terminal means
    "retrying will not help right now", and docs/externals.md's
    TRANSIENT-failure row commits to writing a tombstone on ``dead``
    unconditionally, with no "unless there's prior content" carve-out. See
    ``docs/decisions.md`` for the rationale (recorded alongside the (a)
    worker-hook vs (b) reconcile-sweep mechanism choice).

    Deferred import mirrors :func:`_refresh_handler`: keeps the
    httpx/trafilatura-adjacent ``lode.drawdown``/``lode.externals`` import
    cost off code paths where no ``refresh`` job has ever dead-lettered.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.externals import ingest_snapshot, tombstone_body

    ingest_snapshot(
        conn,
        target_external_id,
        SOURCE_TYPE_WEB,
        tombstone_body(f"dead: {last_error}"),
        status="tombstone",
        settings=settings,
    )


# Register the refresh dead-letter hook on module load.
register_dead_letter("refresh", _refresh_dead_letter_hook)
