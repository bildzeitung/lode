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

    **``next_attempt_at`` is stamped from :func:`now_iso` (lode-0dnk), not the
    schema's own ``strftime('now')`` default — which this fix made redundant and
    lode-uk1i then dropped from ``schema.sql`` outright, so a writer that omits
    the column now fails loudly instead of taking the wrong clock.** Both had
    read the OS wall clock
    independently, which is exactly the crack :func:`now`'s own docstring warns
    about: ``CLOCK_REALTIME`` can step *backward* (NTP correction, or a
    hypervisor catching a descheduled guest back up — routine on a WSL2 VM). A
    freshly-enqueued job's own ``next_attempt_at`` — read from SQLite's clock —
    could then land *ahead of* a same-process claim's ``now()`` reading moments
    later, because ``now()``'s forward-only ratchet only protects **its own**
    repeat calls (docs/storage.md); on the first ``now()`` call in a process
    there is no prior high-water mark to ratchet against, so that first read
    could come out *behind* an independent, already-committed SQLite timestamp
    taken a moment earlier. The CLI's immediate-enrich fast path
    (:func:`lode.worker.claim_and_run_one`, called moments after this enqueue in
    the very same process) hit exactly this: the job it had just enqueued
    intermittently read as "not yet due" and silently sat pending
    (``tests/test_cli.py::test_add_claims_own_job_not_backlog_job``, confirmed
    via a scripted backward-step repro, not reproducible by CPU load alone).
    Routing both the enqueue and the claim through the same ratcheted clock
    closes the gap: the enqueue's own call becomes the ratchet's first (or a
    later) reading, so guarantee 1 — *never decreases within this process* —
    now covers the claim that follows it, no matter how soon after. A
    cross-process claim (the plain ``lode work`` drain loop, run by a different
    process than the one that enqueued) is unaffected either way — it was
    already relying on guarantee 2 (never behind ``CLOCK_REALTIME``) alone, and
    still does; that accepted "a job retried a hair late beats a job stranded"
    trade-off is unchanged.
    """
    next_attempt = now_iso()
    conn.executemany(
        "INSERT INTO jobs (type, target_version, next_attempt_at) VALUES (?, ?, ?) "
        "ON CONFLICT DO NOTHING",
        [(job_type, target_version, next_attempt) for job_type in types],
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
       clock is compared against comes from *this process's* ratchet — another
       process stamped its own (``_now_epoch`` is a module-level global), and
       the forward migration backfills ``next_attempt_at = created``, a raw
       ``strftime('now')`` reading. A clock that merely never went backward
       would lag those writers permanently after a *forward* step and strand
       jobs just the same — trading one bug for a worse one. What (2) does and
       does not cover for each writer: ``docs/storage.md``.

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


def next_failure_state(
    current_attempts: int, settings: Settings
) -> tuple[int, bool, str | None]:
    """Pure retry-policy decision for a job that just failed (lode-yb9t).

    No ``conn``, no SQL, no transaction — this is the *policy* half of the
    shared attempts/backoff/dead-letter transition, factored out of
    :func:`record_job_failure` so a caller that cannot use that function's
    persistence (see :func:`lode.worker._reclaim_stale_running`, which needs
    its own CAS-guarded UPDATE and outer batched transaction) can still share
    the decision instead of duplicating it.

    Returns ``(new_attempts, dead_lettered, next_attempt_at)``:
    ``new_attempts`` is ``current_attempts + 1``; ``dead_lettered`` is whether
    that count reached ``settings.retry_max_attempts``; ``next_attempt_at`` is
    the exponential-backoff ISO-8601 timestamp (:func:`backoff_next_attempt_at`)
    when not dead-lettered, or ``None`` when it is (no further attempt is
    scheduled).
    """
    new_attempts = current_attempts + 1
    if new_attempts >= settings.retry_max_attempts:
        return new_attempts, True, None
    return new_attempts, False, backoff_next_attempt_at(new_attempts, settings)


def cas_update_running(
    conn: sqlite3.Connection,
    job_id: int,
    claimed_at: str | None,
    set_clause: str,
    set_params: tuple,
) -> bool:
    """Execute one CAS-guarded UPDATE against a specific ``'running'`` claim (lode-nggm).

    The single shared primitive behind every writer in this module and
    :mod:`lode.worker` that needs to confirm it still holds a ``'running'``
    claim before mutating it — closing hole 3 from lode-nggm (the
    ``UPDATE ... WHERE id = ? AND status = 'running'`` + check-rowcount idiom
    was hand-rolled at four call sites; a change to the guard shape had to be
    made by hand in each).

    Guards on ``id`` + ``status = 'running'`` + ``claimed_at`` — not
    ``status`` alone. ``status='running'`` is not a unique claim identity, it
    is a state a row can cycle back through: reclaimed to ``'failed'``, reset
    to ``'pending'``, and re-claimed to ``'running'`` (with a *new*
    ``claimed_at``) by a different worker, all inside one stall exceeding
    ``settings.stale_running_timeout_s`` — before the original, still-stalled
    caller finally writes using the STALE ``claimed_at`` it read at claim
    time. A status-only guard cannot tell that later claim from its own and
    would clobber it (lode-nggm hole 2, this ABA case). Comparing
    ``claimed_at`` too closes it: the exact claim has to still be in place,
    not just *a* claim.

    ``claimed_at IS ?`` (not ``= ?``) is SQLite's NULL-safe equality — needed
    because a ``'running'`` row that predates the ``claimed_at`` column, or
    one a migration never got a chance to stamp, reads back NULL, and SQL's
    ``= NULL`` never matches (including against a NULL parameter).

    ``set_clause`` is interpolated into the SQL, so it must always be a
    **literal** written at the call site (every current one is); only
    ``set_params`` may carry runtime values, bound as parameters. Never pass
    caller- or user-derived text as ``set_clause``.

    No transaction management here — the caller wraps this in whatever
    transaction shape it needs: a lone ``with conn:`` around a single call
    (:func:`record_job_failure` below, :func:`lode.worker.run_one`'s
    ``AuthError`` arm), or one outer ``with conn:`` batching several calls
    (:func:`lode.worker._reclaim_stale_running`, which cannot nest a second
    ``with conn:`` inside its own — sqlite3's connection context manager
    doesn't nest).

    Returns True if the row matched and was updated (the claim was still
    held), False if the claim was already lost (rowcount 0).
    """
    cur = conn.execute(
        f"UPDATE jobs SET {set_clause} WHERE id = ? AND status = 'running' "
        f"AND claimed_at IS ?",
        (*set_params, job_id, claimed_at),
    )
    return cur.rowcount == 1


def record_job_failure(
    conn: sqlite3.Connection,
    job_id: int,
    current_attempts: int,
    claimed_at: str | None,
    error_msg: str,
    settings: Settings,
) -> tuple[int, bool, bool]:
    """Apply the shared attempts/backoff/dead-letter transition for job ``job_id``.

    Increments ``attempts`` past ``current_attempts``; if the new count reaches
    ``settings.retry_max_attempts`` the row is marked ``status='dead'``,
    otherwise ``status='failed'`` with an exponential-backoff
    ``next_attempt_at`` (:func:`next_failure_state` /
    :func:`backoff_next_attempt_at`).

    **CAS-guarded on the exact claim (lode-3jte, tightened lode-nggm):** both
    UPDATEs go through :func:`cas_update_running`, which guards on ``id`` +
    ``status = 'running'`` + ``claimed_at`` — the same guard
    :func:`lode.worker.run_one`'s ``except AuthError`` arm and
    :func:`lode.worker._reclaim_stale_running` use on their own writes to this
    table. ``claimed_at`` is the value the *caller* read when it first claimed
    this job (see the ``claimed_at`` parameter below) — not re-read here,
    since by the time this runs the row may already belong to someone else's
    claim. A caller reaching this function (a handler raised, or a Batches API
    result came back errored) does not necessarily still hold the claim it
    started with: e.g. ``cli._enrich_immediately`` reaches
    :func:`lode.worker.run_one` via ``claim_and_run_one`` with no worker lock
    held, so a concurrent ``lode work`` drain's ``_reclaim_stale_running`` can
    reclaim the row as stale mid-handler and drive it straight to a terminal
    ``'dead'`` (firing its dead-letter hook) before this function's UPDATE
    runs. A status-only guard would then resurrect the dead-lettered job back
    to ``'failed'`` (and double-charge ``attempts`` on top) — exactly the
    resurrection :func:`lode.worker.run_one`'s ``AuthError`` arm was hardened
    against (lode-9yy). Guarding on ``claimed_at`` too additionally closes the
    ABA case where the row cycles all the way back to a *different*
    ``'running'`` claim before this write lands (lode-nggm hole 2) — a
    status-only guard cannot tell that claim from this call's own.

    Returns ``(new_attempts, dead_lettered, claim_lost)``. ``claim_lost`` is
    True when :func:`cas_update_running`'s rowcount was 0 — the row was no
    longer this exact claim by the time this ran, so **neither** UPDATE
    actually took effect; ``new_attempts``/``dead_lettered`` then describe
    what *would* have been applied, not what's on the row. A caller must check
    ``claim_lost`` before acting on ``dead_lettered``: it must not run a
    dead-letter hook when the claim was lost, since whoever won the CAS
    already ran it (:func:`lode.worker.run_one` does exactly this).

    This function owns only the DB state transition — a caller that needs to
    run a dead-letter hook (:func:`lode.worker.run_one` does;
    :func:`lode.enrich._mark_job_failed` does not, since ``embed``/``enrich``
    register none) or log differently branches on the returned
    ``dead_lettered``/``claim_lost`` flags itself.

    Shared by :func:`lode.worker.run_one` (a job's ``run()`` handler raised)
    and :func:`lode.enrich._mark_job_failed` (an errored/expired/canceled
    Batches API result) — previously two independent, drifting copies of this
    same transition (lode-ajda).

    **The one caller that doesn't call this function directly** is
    :func:`lode.worker._reclaim_stale_running`: it batches every reclaimed row
    into one outer ``with conn:``, which this function's own ``with conn:``
    cannot nest inside (sqlite3's connection context manager doesn't nest; the
    inner exit would commit the outer's partial work). It shares the same
    policy (:func:`next_failure_state`, lode-yb9t) and the same
    :func:`cas_update_running` primitive this function calls (lode-nggm) —
    calling the bare primitive itself, inside its own transaction, rather than
    this function's transaction-wrapping shell. So the SQL guard shape and the
    retry policy are both centralized now; only the transaction-batching shell
    is genuinely duplicated there.
    """
    new_attempts, dead_lettered, next_at = next_failure_state(
        current_attempts, settings
    )
    if dead_lettered:
        with conn:
            claim_held = cas_update_running(
                conn,
                job_id,
                claimed_at,
                "status = 'dead', attempts = ?, last_error = ?",
                (new_attempts, error_msg),
            )
        return new_attempts, True, not claim_held
    with conn:
        claim_held = cas_update_running(
            conn,
            job_id,
            claimed_at,
            "status = 'failed', attempts = ?, last_error = ?, next_attempt_at = ?",
            (new_attempts, error_msg, next_at),
        )
    return new_attempts, False, not claim_held
