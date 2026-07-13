"""Derive-job enqueue seam (lode-y42.1, lode-i05.1).

The capture path stays instant by doing **no AI work** itself: it persists the
version (via :class:`lode.repository.Repository`) and drops the *derived* work onto
the durable ``jobs`` queue (``docs/storage.md`` "The async work queue") for the
workers that land later. This module holds the enqueue primitive.

Two derive jobs are enqueued per captured version, in the doc's priority order
(``embed > enrich``):

- ``embed`` — fast, local, high priority; chunk + embed so semantic recall lands
  in seconds.
- ``enrich`` — slow, Claude (tags / entities / inferred edges); may lag.

``refresh(external)`` arrives with the connectors step (``lode-w0h.3``): the web
draw-down trigger (:mod:`lode.drawdown`, called from
:meth:`lode.repository.Repository.save`) enqueues it via this same function
with an explicit ``types=("refresh",)`` override, keyed on an external's
canonical URL rather than a note ``version_id`` — not through
:data:`DERIVE_JOB_TYPES`, which stays the per-note-save default (embed +
enrich only).

**Transaction ownership (lode-i05.1, pinned 2026-06-28):** the enqueue is NOT its
own transaction. :func:`enqueue_derive_jobs` runs as a plain INSERT on the caller's
connection, inside whatever transaction the caller opened. In practice that caller
is always :meth:`lode.repository.Repository.save`, which wraps the version-write
and this enqueue in a single ``with conn:`` so both commit atomically. The
**reconciliation scan** (``docs/storage.md`` — re-enqueue any head version missing
derived work) is the self-healing net for the rare crash; every job is idempotent
by key, so a re-enqueue is safe.

**Retry clock + backoff (lode-ajda)** also lives here, not in :mod:`lode.worker`:
both the worker's main claim/run loop and :mod:`lode.enrich`'s Batches-API result
handler (:func:`~lode.enrich.collect_enrich_batch`) apply the identical
attempts/backoff/dead-letter transition to a ``jobs`` row on failure, and this
module already owns the table both are updating. Centralizing it here (rather
than, say, having ``lode.enrich`` import ``lode.worker``) collapses those two
into the single :func:`record_job_failure` — which is *not*, however, the only
code that fails a job; see its docstring for the one deliberate exception.
"""

import sqlite3
import time
from datetime import UTC, datetime, timedelta

from lode.config import Settings

#: Derive job types enqueued on every capture, in priority order
#: (``docs/storage.md`` "embed > enrich"). ``refresh`` arrives with connectors.
DERIVE_JOB_TYPES = ("embed", "enrich")


def enqueue_derive_jobs(
    conn: sqlite3.Connection,
    target_version: str,
    *,
    types: tuple[str, ...] = DERIVE_JOB_TYPES,
) -> None:
    """Insert one pending job per type in ``types`` for ``target_version`` on ``conn``.

    Each row lands with the schema defaults (``status='pending'``, ``attempts=0``);
    ``prompt_ver`` is left NULL for the worker/reconciliation pass to stamp.

    **No transaction boundary here** — this runs on the caller's connection inside
    whatever transaction the caller opened (see module docstring). The caller is
    responsible for committing or rolling back.

    The INSERT uses ``ON CONFLICT DO NOTHING`` against the partial unique index
    ``idx_jobs_live`` (``src/lode/schema.sql``): a duplicate enqueue of the same
    live (pending/running) ``(type, target_version[, prompt_ver])`` job is a
    no-op. Re-enqueue after the prior job is ``done``/``dead`` IS allowed because
    the index is scoped to live statuses only (``docs/storage.md`` §E2 idempotency
    key decisions, pinned 2026-06-28).

    ``types`` defaults to :data:`DERIVE_JOB_TYPES` (the full set — ``embed`` +
    ``enrich``). Callers that want a targeted single-type enqueue (e.g. the
    reconciliation scan's embed-gap step) pass an explicit subset; the underlying
    INSERT is the same in both cases.
    """
    conn.executemany(
        "INSERT INTO jobs (type, target_version) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [(job_type, target_version) for job_type in types],
    )


# ---------------------------------------------------------------------------
# Retry clock + backoff (lode-ajda) — see module docstring for why this is here
# ---------------------------------------------------------------------------


def iso(dt: datetime) -> str:
    """Format ``dt`` as the schema's ISO-8601 millisecond-``Z`` timestamp.

    Matches SQLite's ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` (millisecond
    precision, e.g. ``2026-06-28T12:34:56.789Z``) so string comparisons in
    ``next_attempt_at <= ?`` clauses are chronologically correct.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Wall-clock time corresponding to ``time.monotonic() == 0``. Ratcheted
# forward-only, so :func:`now` can never hand back a decreasing reading.
_now_epoch: datetime = datetime.min.replace(tzinfo=UTC)


def now() -> datetime:
    """Return the current UTC time, with two guarantees the jobs queue depends on.

    1. **Never decreases within this process.** The OS may step
       ``CLOCK_REALTIME`` (what ``datetime.now(UTC)`` reads) *backward* — NTP
       correction, or hypervisor catch-up after the guest was descheduled. A
       backward step is read by :func:`lode.worker._claim_one`'s
       ``next_attempt_at <= now`` predicate as "nothing is ready yet", and
       :func:`lode.worker.drain`'s loop breaks on the first miss — stranding an
       already-eligible job for the rest of the pass.
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
    ``docs/storage.md``; the repro is in lode-t1y. Originally lived in
    ``lode.worker`` — moved here (lode-ajda) so :mod:`lode.enrich` shares the
    same clock instead of reading a raw, unanchored ``datetime.now(UTC)``.
    """
    global _now_epoch
    elapsed = timedelta(seconds=time.monotonic())
    _now_epoch = max(_now_epoch, datetime.now(UTC) - elapsed)
    return _now_epoch + elapsed


def now_iso() -> str:
    """Return :func:`now` in the schema's ISO-8601 format."""
    return iso(now())


def backoff_next_attempt_at(new_attempts: int, settings: Settings) -> str:
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
    return iso(now() + timedelta(seconds=delay))


def record_job_failure(
    conn: sqlite3.Connection,
    job_id: int,
    current_attempts: int,
    error_msg: str,
    settings: Settings,
) -> tuple[int, bool]:
    """Apply the shared attempts/backoff/dead-letter transition for job ``job_id``.

    Increments ``attempts`` past ``current_attempts``; if the new count reaches
    ``settings.retry_max_attempts`` the row is marked ``status='dead'``,
    otherwise ``status='failed'`` with an exponential-backoff
    ``next_attempt_at`` (:func:`backoff_next_attempt_at`).

    Returns ``(new_attempts, dead_lettered)``. This function owns only the DB
    state transition — a caller that needs to run a dead-letter hook
    (:func:`lode.worker.run_one` does; :func:`lode.enrich._mark_job_failed`
    does not, since ``embed``/``enrich`` register none) or log differently
    branches on the returned ``dead_lettered`` flag itself.

    Shared by :func:`lode.worker.run_one` (a job's ``run()`` handler raised)
    and :func:`lode.enrich._mark_job_failed` (an errored/expired/canceled
    Batches API result) — previously two independent, drifting copies of this
    same transition (lode-ajda).

    **The one caller this does NOT serve** is
    :func:`lode.worker._reclaim_stale_running`, which keeps its own inline
    UPDATEs: it needs an ``AND status='running'`` CAS guard (the row may no
    longer be its claim) plus the per-row ``rowcount`` that guard yields, and it
    batches every reclaimed row into one outer ``with conn:`` — which this
    function's own ``with conn:`` cannot nest inside (sqlite3's connection
    context manager doesn't nest; the inner exit would commit the outer's
    partial work). It shares the backoff *formula* via
    :func:`backoff_next_attempt_at`, but the attempts increment and the
    ``>= retry_max_attempts`` dead-letter gate are genuinely duplicated there.
    **A change to the retry policy has to be made in both places** — the reclaim
    path promises a crash-reclaimed job obeys the identical max-attempts gate as
    a cleanly-failed one, and nothing enforces that but this note.
    """
    new_attempts = current_attempts + 1
    if new_attempts >= settings.retry_max_attempts:
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'dead', attempts = ?, last_error = ? "
                "WHERE id = ?",
                (new_attempts, error_msg, job_id),
            )
        return new_attempts, True
    next_at = backoff_next_attempt_at(new_attempts, settings)
    with conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', attempts = ?, "
            "last_error = ?, next_attempt_at = ? WHERE id = ?",
            (new_attempts, error_msg, next_at, job_id),
        )
    return new_attempts, False
