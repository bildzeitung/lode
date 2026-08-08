"""Worker loop: claim → run → retry/backoff → dead-letter (lode-i05.3).

ONE-SHOT DRAIN by default: acquire the advisory lock (i05.2), reset overdue
retries, then claim+run ready pending jobs until none remain and exit.
``--loop`` / ``--watch`` polls on an interval (exposed by :func:`drain` via the
``lode work`` Typer command in :mod:`lode.cli`).

**Handler registry** dispatches on ``jobs.type``:

- ``embed`` — registered now; runs the **vector-only** path
  (:func:`lode.embedding.embed`: chunk + embed + LanceDB vectors). The FTS
  lexical leg is **not here** (lode-xyb): the synchronous
  :class:`~lode.lexical.LexicalCacheBackend` in ``cli/add.py``'s ``add`` writes
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
   purged versions, and submit the rest via the ``LLMProvider`` seam's
   ``submit_batch`` (``client.beta.messages.batches.create`` for the Anthropic
   provider, 50%-off Batches API). Each submitted job row is updated to ``status='running'``
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
**Late-success guard (lode-uda1):** the hook is passed the dead-lettered
job's own ``claimed_at`` and skips its tombstone write when the external's
head is already a non-tombstone snapshot fetched at or after that claim — a
real, successful fetch (this job's own still-in-flight handler racing
:func:`_reclaim_stale_running`, or another refresh) beat the dead-letter
verdict, and the verdict must not overwrite that fact. See
``docs/externals.md`` "Fetch-outcome taxonomy" / ``docs/storage.md`` "Crash
recovery" for the settled semantics.

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

import functools
import logging
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from lode import jobs
from lode.config import Settings
from lode.config import lance_dir as _lance_dir
from lode.ids import short_version_id
from lode.progress import op_progress
from lode.sql_ids import placeholders

if TYPE_CHECKING:
    from lode.embedding import Embedder
    from lode.vectorstore import VectorStore

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


#: Dead-letter hook signature:
#: (conn, target_version, last_error, claimed_at, settings) -> None
#:
#: Invoked once, immediately after a job of the registered ``type`` reaches
#: the terminal ``'dead'`` status — never on a transient ``'failed'`` that
#: still has a retry coming. See the module docstring's "Dead-letter hook"
#: section for the transaction-composition contract (own transaction,
#: sequential not nested). ``claimed_at`` (lode-uda1) is the dead-lettered
#: job's own claim timestamp — the last point at which its dead-letter
#: verdict was known to still be true; a hook needing to tell that verdict
#: apart from a fact that has since superseded it (e.g. the handler's own
#: fetch actually succeeded before a crash-reclaim dead-lettered it) compares
#: against this rather than "now".
DeadLetterFn = Callable[[sqlite3.Connection, str, str, str | None, Settings], None]

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
    claimed_at: str | None,
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

    ``claimed_at`` is the dead-lettered job's own claim timestamp (lode-uda1)
    — passed through unchanged to whatever hook is registered.
    """
    hook = _DEAD_LETTER_HOOKS.get(job_type)
    if hook is None:
        return
    try:
        hook(conn, target_version, last_error, claimed_at, settings)
    except Exception:
        log.exception(
            "dead-letter hook for job type %r (target=%s) failed; job remains "
            "'dead' but its dead-letter side effect was not recorded",
            job_type,
            target_version,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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

    **Reclaim:** each selected row is put through exactly the same retry
    *policy* :func:`run_one` uses for a transient handler failure —
    ``attempts += 1``; at ``retry_max_attempts`` → ``status='dead'``;
    otherwise → ``status='failed'`` with a backoff ``next_attempt_at``
    (picked up by :func:`_reset_retryable` once it's due, same as any other
    retry). That decision comes from :func:`lode.jobs.next_failure_state`
    (lode-yb9t) — the same pure function :func:`lode.jobs.record_job_failure`
    calls — rather than a second, inline copy of the attempts-increment and
    max-attempts gate. The persistence goes through
    :func:`lode.jobs.cas_update_running` (lode-nggm) — the same CAS-guarded
    primitive :func:`~lode.jobs.record_job_failure` uses — but called bare,
    inside this loop's own outer ``with conn:`` batching every reclaimed row,
    rather than through that function's transaction-wrapping shell (which
    can't nest a second ``with conn:`` inside this one). The guard is on the
    exact ``claimed_at`` this SELECT just read, not ``status`` alone — the row
    could otherwise have already moved to a *different* ``'running'`` claim by
    the time this UPDATE runs (this loop's own ``with conn:`` serializes its
    rows against each other, but not against another process's write between
    the SELECT above and this transaction opening). A crash-reclaimed job is
    therefore structurally guaranteed to obey the identical max-attempts gate
    as one that failed cleanly — no parallel retry policy to keep in sync.

    Applies uniformly to every job ``type`` (``embed``, ``enrich``, ``refresh``)
    — the staleness signal is the same regardless of what kind of work was
    interrupted. A job reclaimed straight to ``'dead'`` (attempts already
    exhausted) runs the same dead-letter hook (lode-at8, module docstring)
    :func:`run_one` does — invoked *after* this function's own ``with conn:``
    batch has committed, so a crash mid-reclaim never leaves a hook call
    racing an uncommitted status write.

    Returns the count of jobs reclaimed.
    """
    cutoff = jobs.iso(jobs.now() - timedelta(seconds=settings.stale_running_timeout_s))
    rows = conn.execute(
        "SELECT id, attempts, type, target_version, claimed_at FROM jobs "
        "WHERE status = 'running' AND batch_handle IS NULL "
        "AND (claimed_at IS NULL OR claimed_at <= ?)",
        (cutoff,),
    ).fetchall()

    if not rows:
        return 0

    reclaimed = 0
    # (job_type, target_version, claimed_at) -- claimed_at (lode-uda1) is the
    # exact claim this reclaim just dead-lettered, passed through to the
    # dead-letter hook so it can tell its own verdict apart from a fact
    # (a real snapshot fetched_at at-or-after this claim) that supersedes it.
    newly_dead: list[tuple[str, str, str | None]] = []
    err = "reclaimed: stuck in 'running' past staleness timeout (possible crash)"
    with conn:
        for job_id, attempts, job_type, target_version, claimed_at in rows:
            # Policy decision shared with jobs.record_job_failure (lode-yb9t)
            # — only the CAS-guarded persistence below is specific to reclaim.
            new_attempts, dead_lettered, next_at = jobs.next_failure_state(
                attempts, settings
            )
            if dead_lettered:
                claim_held = jobs.cas_update_running(
                    conn,
                    job_id,
                    claimed_at,
                    "status = 'dead', attempts = ?, last_error = ?",
                    (new_attempts, err),
                )
                if claim_held:
                    newly_dead.append((job_type, target_version, claimed_at))
                    # Per-job source name (lode-ympb) -- mirrors run_one's
                    # sibling "failed"/dead-letter log lines (job_type +
                    # short(target)) so a connector job (JIRA/Confluence/web
                    # refresh) crash-reclaimed straight to dead is identifiable
                    # by source, not just counted in drain()'s aggregate
                    # "reclaimed %d stale running job(s)" line.
                    log.error(
                        "job %d (%s target=%s) dead-lettered by crash-reclaim "
                        "after %d attempt(s): %s",
                        job_id,
                        job_type,
                        short_version_id(target_version),
                        new_attempts,
                        err,
                    )
            else:
                claim_held = jobs.cas_update_running(
                    conn,
                    job_id,
                    claimed_at,
                    "status = 'failed', attempts = ?, last_error = ?, next_attempt_at = ?",
                    (new_attempts, err, next_at),
                )
            reclaimed += 1 if claim_held else 0

    for job_type, target_version, claimed_at in newly_dead:
        _run_dead_letter_hook(conn, job_type, target_version, err, claimed_at, settings)

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
    params: list[object] = [now, *types]
    version_clause = ""
    if target_version is not None:
        version_clause = "AND target_version = ? "
        params.append(target_version)
    row = conn.execute(
        f"SELECT id FROM jobs "
        f"WHERE status = 'pending' AND next_attempt_at <= ? "
        f"AND type IN ({placeholders(len(types))}) "
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
    - Handler raises a **transient** error but a concurrent
      ``_reclaim_stale_running`` already reclaimed this row mid-handler
      (lode-3jte) → :func:`lode.jobs.record_job_failure`'s CAS guard (on the
      exact claim — ``id`` + ``status='running'`` + ``claimed_at``, tightened
      lode-nggm) makes the UPDATE above a no-op (reports ``claim_lost``); this
      function leaves the row exactly as the reclaim left it and does **not**
      run the dead-letter hook a second time — the same resurrection the
      ``AuthError`` arm below already guards against (lode-9yy), mirrored here
      for the transient path.
    - Handler raises a **permanent, user-actionable** error
      (:class:`lode.auth.AuthError` or :class:`~lode.llm_provider.LLMAuthError`,
      matching this function's own catch below — see ``docs/storage.md``
      "Transient vs. permanent job failures", lode-9yy, lode-568v.3) → none of
      the above: the job is reset straight back to ``status='pending'`` with
      ``attempts`` untouched (no backoff, never ``'dead'``) and the exception is
      **re-raised** rather than absorbed, so it reaches the caller with its
      actionable message instead of being silently retried forever on something
      retrying can never fix.

    Returns ``True`` on success (``done``), ``False`` on a transient error
    (``failed`` or ``dead``); raises on a permanent error (see above).  The
    registry must contain a handler for the job's type (call sites guarantee
    this because ``_claim_one`` filters to registered types).
    """
    row = conn.execute(
        "SELECT type, target_version, attempts, claimed_at FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        log.warning("job %d disappeared before run — skipping", job_id)
        return False

    job_type, target_version, attempts, claimed_at = row
    handler = registry[job_type]
    short = short_version_id(target_version)

    # Deferred import so the Anthropic/OpenAI SDKs stay off this module's
    # import graph; bound before the `try` because an `except` clause header
    # needs the classes. LLMAuthError (lode-568v.3) is caught alongside
    # AuthError -- a missing OpenAI/Azure credential is exactly as permanent,
    # user-actionable a failure as a missing Anthropic one (lode-9yy), and
    # lode-568v.2's tracked follow-up said this widening was needed once a
    # second provider's credential failures had no existing exception type to
    # preserve (docs/decisions.md).
    from lode.auth import AuthError
    from lode.llm_provider import LLMAuthError

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

    # Permanent, user-actionable failure (lode-9yy, docs/storage.md "Transient
    # vs. permanent job failures") — retrying can never succeed, so it must not
    # reach the transient accounting below: no attempts charged, no backoff,
    # never dead-lettered. Reset the claim to 'pending' (uncharged) and re-raise
    # so the caller sees build_client's actionable message.
    #
    # Ordered ahead of `except Exception` — AuthError/LLMAuthError are both
    # RuntimeError subclasses, so the generic arm would otherwise swallow them.
    # That swallow IS the bug lode-9yy fixes.
    except (AuthError, LLMAuthError) as exc:
        # CAS on the exact claim (id + status='running' + claimed_at — lode-nggm
        # tightened this from status alone): this job is not necessarily still
        # ours. `cli._enrich_immediately` reaches run_one via claim_and_run_one,
        # which runs WITHOUT the worker lock, so a concurrent `lode work` drain
        # can reclaim this row as stale mid-handler and drive it to a terminal
        # 'dead' -- or cycle it all the way back to a *different* 'running'
        # claim before this write lands (the ABA case a status-only guard can't
        # see). Unguarded, the reset would then resurrect that dead job (whose
        # dead-letter hook has already fired) back to 'pending', or clobber the
        # new claimant. If we no longer hold this exact claim, leave the row to
        # whoever does.
        with conn:
            jobs.cas_update_running(
                conn,
                job_id,
                claimed_at,
                "status = 'pending', last_error = ?",
                (str(exc),),
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

    except Exception as exc:
        err = str(exc)
        log.warning(
            "job %d (%s target=%s) failed (attempt %d/%d): %s",
            job_id,
            job_type,
            short,
            attempts + 1,
            settings.retry_max_attempts,
            err,
            exc_info=True,
        )
        # Shared attempts/backoff/dead-letter transition (lode-ajda) — also used
        # by lode.enrich._mark_job_failed for a Batches API result, so there is
        # exactly one implementation of this state machine.
        new_attempts, dead, claim_lost = jobs.record_job_failure(
            conn, job_id, attempts, claimed_at, err, settings
        )
        if claim_lost:
            # CAS-guarded on the exact claim (lode-3jte, tightened lode-nggm):
            # the row was no longer this exact claim by the time
            # record_job_failure's UPDATE ran — same race run_one's AuthError
            # arm above already guards against (lode-9yy), now also closing the
            # ABA case where the row cycled back to a *different* 'running'
            # claim (lode-nggm hole 2). A concurrent `_reclaim_stale_running`
            # reclaimed this job as stale mid-handler and already drove it to a
            # terminal state (firing its own dead-letter hook if it
            # dead-lettered); neither UPDATE above took effect, so there is
            # nothing further to log as dead here and running the hook again
            # would fire it a second time for the same job (lode-at8 promises
            # exactly once).
            log.info(
                "job %d (%s target=%s) transient failure but the claim was "
                "lost to a concurrent reclaim — no update applied, no "
                "dead-letter hook run",
                job_id,
                job_type,
                short,
            )
            return False
        if dead:
            log.error(
                "job %d (%s target=%s) dead-lettered after %d attempt(s): %s",
                job_id,
                job_type,
                short,
                new_attempts,
                err,
            )
            # Own, separate transaction — after the dead-status UPDATE inside
            # record_job_failure above has committed, never nested in the same
            # `with conn:` (lode-at8, module docstring "Dead-letter hook").
            # claimed_at is this job's own claim (read at the top of run_one,
            # lode-uda1): the hook uses it to tell this dead-letter verdict
            # apart from a fact that has since superseded it.
            _run_dead_letter_hook(
                conn, job_type, target_version, err, claimed_at, settings
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
    job_id = _claim_one(conn, types, jobs.now_iso(), target_version=target_version)
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

    ``_client`` is injectable for tests (an ``LLMProvider``). ``outcomes``, if given, is passed
    through to :func:`lode.enrich.collect_enrich_batch` (lode-1gr.4) so a
    drain pass that collects a completed batch appends a per-note outcome
    line for each succeeded result — the batch pre-step runs ahead of
    :func:`drain`'s main claim/run loop, so this is the only channel that
    surfaces those outcomes to the caller.

    **Per-handle isolation (lode-knnt).** Each ``batch_handle`` is polled
    inside its own ``try``, so one poisoned handle cannot stop the OTHER,
    healthy handles in the same pass from being collected. The catch is
    **consequence-scoped, not type-scoped** — it absorbs whatever type
    ``collect_enrich_batch`` raises — and the failure is **deferred, not
    swallowed**: the first one is re-raised once every handle has had its
    turn, so :func:`drain` still sees it. ``AuthError``/``LLMAuthError`` is
    the exception, re-raised immediately mid-loop: a missing credential is
    not handle-specific, so there is nothing to gain from attempting the
    rest.

    Note the ``SELECT`` and imports above sit OUTSIDE that per-handle ``try``,
    so a failure there is not isolated per handle — it propagates to
    :func:`drain`, which catches it all the same. ``docs/storage.md``
    "Transient vs. permanent job failures" owns the full rationale.

    **Consecutive-failure budget (lode-u6he).** The counter's two events live
    here: every non-auth failure below calls
    :func:`_record_batch_collect_failure` (dead-lettering the handle's
    still-``running`` rows once ``settings.batch_collect_failure_budget`` is
    reached), and every poll that does NOT raise — batch still in progress,
    or ended and processed — calls :func:`_reset_batch_collect_failures`.
    ``docs/storage.md`` "Transient vs. permanent job failures" owns why the
    budget exists and what dead-lettering does and does not discard.
    """
    batch_ids: list[str] = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT batch_handle FROM jobs "
            "WHERE type = 'enrich' AND status = 'running' AND batch_handle IS NOT NULL"
        ).fetchall()
    ]

    if not batch_ids:
        return 0

    # Below the early-return guard: no reason to import lode.enrich on a drain with
    # no enrich work at all (lode-4q97). Hygiene, not the load-bearing fix -- what
    # keeps an embed-only drain SDK-free is that lode.enrich and lode.auth are both
    # cheap to import (their `import anthropic` is TYPE_CHECKING-guarded).
    from lode.auth import AuthError
    from lode.enrich import collect_enrich_batch
    from lode.llm_provider import LLMAuthError

    kwargs: dict = {}
    if _client is not None:
        kwargs["provider"] = _client

    ended = 0
    deferred_exc: Exception | None = None
    for batch_id in batch_ids:
        try:
            if collect_enrich_batch(
                conn, batch_id, settings, outcomes=outcomes, **kwargs
            ):
                ended += 1
            _reset_batch_collect_failures(conn, batch_id)
        except AuthError, LLMAuthError:
            # Not handle-specific -- every remaining handle shares the same
            # credentials and would fail identically. Propagate immediately;
            # drain()'s own stash-and-continue contract takes it from here.
            raise
        except Exception as exc:
            log.warning(
                "_batch_collect_enrich: batch=%s poll failed, skipping this "
                "pass (will retry next tick): %s",
                batch_id,
                exc,
                exc_info=True,
            )
            _record_batch_collect_failure(conn, batch_id, exc, settings)
            # Deferred, not swallowed: every OTHER handle still gets its turn
            # (per-handle isolation, lode-knnt) before this is raised below.
            if deferred_exc is None:
                deferred_exc = exc

    if deferred_exc is not None:
        raise deferred_exc

    return ended


def _reset_batch_collect_failures(conn: sqlite3.Connection, batch_id: str) -> None:
    """Zero ``batch_collect_failures`` for ``batch_id`` after a poll that did
    not raise (lode-u6he).

    ``AND batch_collect_failures != 0`` keeps the steady state free: a healthy
    handle is polled on every tick for as long as the batch is in flight (up
    to the Batches API's 24h SLA), and without that clause each of those
    polls would dirty and re-commit every row on the handle to rewrite 0 as 0.
    """
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_collect_failures = 0 "
            "WHERE batch_handle = ? AND status = 'running' "
            "AND batch_collect_failures != 0",
            (batch_id,),
        )


def _record_batch_collect_failure(
    conn: sqlite3.Connection, batch_id: str, exc: Exception, settings: Settings
) -> None:
    """Bump the consecutive collect-failure count for ``batch_id`` and
    dead-letter its still-``running`` jobs once the budget is exhausted
    (lode-u6he).

    Why the count is a column and why there is no final salvage collect call
    is owned by ``docs/storage.md`` "Transient vs. permanent job failures"
    (Consecutive-failure budget) — read it before changing either.

    Two local notes that doc does not own. The bulk ``UPDATE`` deliberately
    does not go through :func:`lode.jobs.cas_update_running`: that primitive
    guards one ``id`` + ``claimed_at`` against the reclaim/re-claim ABA race,
    and a ``running`` row with ``batch_handle`` set is exactly the row
    :func:`_reclaim_stale_running` excludes, so it cannot cycle out and back
    under us. And no :func:`_run_dead_letter_hook` fires here: the registry
    is keyed by job type and only ``refresh`` registers one, so for these
    (always ``enrich``) rows it would be a no-op.
    """
    with conn:
        rows = conn.execute(
            "UPDATE jobs SET batch_collect_failures = batch_collect_failures + 1 "
            "WHERE batch_handle = ? AND status = 'running' "
            "RETURNING batch_collect_failures",
            (batch_id,),
        ).fetchall()
        # Every row on a handle is incremented and reset together, so MAX is
        # just "the" count -- but it is also the safe read if that ever stops
        # holding, and it needs no ORDER BY to say so.
        failures = max((r[0] for r in rows), default=0)

        if failures >= settings.batch_collect_failure_budget:
            error_msg = f"batch collect failed {failures} time(s) in a row: {exc}"
            cur = conn.execute(
                "UPDATE jobs SET status = 'dead', last_error = ? "
                "WHERE batch_handle = ? AND status = 'running'",
                (error_msg, batch_id),
            )
            log.error(
                "_record_batch_collect_failure: batch=%s dead-lettered %d job(s) "
                "after %d consecutive collect failure(s): %s",
                batch_id,
                cur.rowcount,
                failures,
                exc,
            )


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

    ``_client`` is injectable for tests (an ``LLMProvider``).

    **What sits outside this function's own try (lode-2mnj).** The internal
    try below opens at the :func:`lode.enrich.submit_enrich_batch` call — the
    pending-jobs SELECT, the deferred imports, and the ENTIRE pre-claim CAS
    loop above it are not covered by anything this function catches, so a
    failure there (e.g. ``sqlite3.OperationalError`` from the CAS loop racing
    an interactive immediate-enrich claim past the busy_timeout) propagates
    uncaught to the caller. ``docs/storage.md`` "Transient vs. permanent job
    failures" owns what the caller does about that.
    """
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
        (jobs.now_iso(), flush_size),
    ).fetchall()

    if not rows:
        return 0

    # Below the early-return guard, same as _batch_collect_enrich (lode-4q97).
    # AuthError/LLMAuthError are bound here rather than in the handler because
    # an `except` clause header needs the classes (LLMAuthError widening,
    # lode-568v.3 -- see run_one's identical comment).
    from lode.auth import AuthError
    from lode.enrich import submit_enrich_batch
    from lode.llm_provider import LLMAuthError

    # Pre-claim each job with an asserted CAS (rowcount == 1), exactly as
    # _claim_one does, and submit ONLY the jobs this step actually won. The
    # interactive `lode add` immediate-enrich (claim_and_run_one) runs without
    # the worker lock and can flip one of these rows 'pending' -> 'running'
    # between the SELECT above and here; passing the raw SELECT to
    # submit_enrich_batch would then submit a job already claimed (and possibly
    # already run) elsewhere -- a double API spend. A lost CAS (rowcount 0)
    # means someone else owns it, so we drop it from this batch.
    claimed_rows: list[tuple[int, str]] = []
    now = jobs.now_iso()
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
        kwargs["provider"] = _client

    try:
        batch_id = submit_enrich_batch(conn, claimed_rows, settings, **kwargs)
        submitted = sum(
            1
            for row in conn.execute(
                f"SELECT id FROM jobs WHERE id IN ({placeholders(len(job_ids))}) "
                "AND batch_handle IS NOT NULL",
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

    # Permanent, user-actionable failure (lode-9yy, docs/storage.md "Transient
    # vs. permanent job failures") — retrying can never succeed, so it must not
    # be folded into the transient revert below: no attempts charged, no backoff,
    # never dead-lettered. Reset the pre-claimed jobs to 'pending' (uncharged)
    # and re-raise so `lode work` sees build_client's actionable message.
    # Ordered ahead of `except Exception` — AuthError/LLMAuthError are both
    # RuntimeError subclasses, so the generic arm would otherwise swallow them.
    #
    # Deliberately NOT widened, even though drain()'s outer catch around this
    # whole pre-step now IS bare `Exception` (lode-2mnj): this arm has the
    # transient `except Exception` path below it, which reverts the pre-claimed
    # jobs with backoff so an ordinary 429/5xx retries next tick under the usual
    # attempts/dead-letter accounting. A non-auth provider error raised from
    # INSIDE this try is transient BY DESIGN. drain()'s catch is wider because
    # it also spans everything ahead of this try (the SELECT, the imports, the
    # pre-claim CAS loop) which has no such accounting to fall into. Making
    # these two "consistent" would turn every transient submit failure into a
    # permanent, uncharged reset plus a hard non-zero exit on every tick.
    except (AuthError, LLMAuthError) as exc:
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

    except Exception as exc:
        log.warning(
            "_batch_submit_enrich: API call failed: %s — reverting jobs",
            exc,
            exc_info=True,
        )
        # Revert all pre-claimed jobs to 'failed' with a short backoff so they
        # are retried on the next pass (not immediately — avoids hammering the API).
        # First-failure backoff (min(base * 2**0, cap) == min(base, cap)) via the
        # shared helper rather than open-coded, so this path inherits any future
        # change to the retry curve — e.g. jitter, which is exactly what a batch
        # of jobs reverting together off one API error would want. NOTE: no
        # attempt is charged here (`attempts` is untouched); the literal 1 selects
        # the shortest rung of the curve, it is not this job's attempt count.
        next_at = jobs.backoff_next_attempt_at(1, settings)
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
    embedder: Embedder | None = None,
    store: VectorStore | None = None,
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

    **One shared embedder per call, not one per job (lode-j5r2).** Every
    ``embed`` job this call runs gets the same
    :class:`~lode.embedding.FastEmbedEmbedder` — ``embedder`` if given, else one
    constructed here — instead of each building its own, which cost a full ONNX
    load (~1.5s measured) and a live HuggingFace revision probe *per job*.
    Sharing across *calls* is the caller's decision, made by passing the same
    instance in every time; ``lode work`` does that, so a whole polling session
    (``--loop`` or ``--wait``) pays once per process. The mechanism, and why a
    test-injected ``embed`` stub never sees it, is commented at the swap below.

    The trade (``docs/decisions.md``, lode-j5r2): a long-lived embedder keeps
    the ONNX model resident for the whole process — intended, and unchanged. A
    long-lived ``model_revision()`` latch was the other half, and is no longer
    accepted: a FAILED probe is re-armed once per call (lode-fxse), commented
    at the same swap below.

    Returns the total number of jobs claimed and run by the **main loop**
    (including failures and dead-letters). Batch pre-step and reclaim activity
    is logged but not included in the return count.

    **Permanent failures** (:class:`lode.auth.AuthError`, or any
    :class:`~lode.llm_provider.LLMProviderError` — ``docs/storage.md``
    "Transient vs. permanent job failures" owns the taxonomy and the
    rationale; lode-9yy, widened to ``LLMProviderError`` by lode-5zqa):
    ``drain`` raises rather than returning, so ``lode work`` exits non-zero.
    But it raises **last**, not on the spot: the error is stashed and
    re-raised only after the reclaim, the retry reset, and the main claim/run
    loop have all run, so the credential-free ``embed`` jobs are never
    starved by a missing key or by a batch wedged on bad data. Both pre-steps'
    own ``try`` (below) in fact catch wider than this named taxonomy — see
    next.

    **Per-handle isolation + independent pre-steps (lode-knnt, lode-2mnj).**
    One stuck ``batch_handle`` no longer stops any *other* handle in the same
    pass (:func:`_batch_collect_enrich` isolates each one; see its
    docstring), and the two pre-steps below now run under their **own**
    ``try`` each rather than sharing one, so a collect-side failure no longer
    skips the submit step. Both catches are bare ``Exception`` (lode-2mnj
    widened the submit arm to match collect's lode-knnt fix): each pre-step
    can raise something unclassified from code sitting outside its own
    internal ``try``, and a narrow catch here let exactly that abort ``drain``
    before the credential-free embed work ran. ``docs/storage.md`` "Transient
    vs. permanent job failures" owns the rationale.

    **Consecutive-failure budget (lode-u6he).** Neither of those, on its own,
    makes a stuck batch un-stuck — that is what the budget adds: a handle
    whose poll keeps *raising* is dead-lettered after
    ``settings.batch_collect_failure_budget`` consecutive failures, so it
    reaches a terminal state without a human. See
    :func:`_batch_collect_enrich`.

    **One shared VectorStore per call too (lode-2brb), same shape.** ``store``
    if given, else one constructed here, is threaded into every ``embed`` job
    the same way ``embedder`` is, so a drain's jobs share one opened LanceDB
    table instead of each job reopening it
    (:meth:`~lode.vectorstore.VectorStore._open_or_create_table`).

    ``_registry`` is injectable for tests; production callers omit it and the
    module-level :data:`_REGISTRY` is used. ``_batch_client`` is injectable for
    tests (an ``LLMProvider``, passed through to the batch pre-steps).
    ``embedder`` is likewise optional — see "One shared embedder per call" above.

    ``outcomes`` (lode-1gr.4), if given, is a mutable list that this call
    appends human-readable per-job outcome lines to — from both channels: the
    batch pre-step (:func:`_batch_collect_enrich`, a *later* pass collecting a
    completed enrich batch) and the main loop (via :func:`run_one`, e.g.
    ``embed`` jobs). Left ``None`` (the default), behavior is unchanged from
    before lode-1gr.4 — this is purely additive so existing callers (and the
    ``int`` return contract) are unaffected.

    **Progress instrumentation (lode-olmi.15):** ``drain.batch_collect``,
    ``drain.batch_submit``, and ``drain.run_jobs`` each log a start/heartbeat/
    done line via :func:`lode.progress.op_progress` (cadence
    ``settings.progress_heartbeat_interval_s``) — so a plain ``lode work`` that
    stalls inside one of these always shows *which* step it is stuck in,
    instead of producing no output until it finishes or hangs forever.
    """
    settings = settings or Settings()
    registry = _registry if _registry is not None else _REGISTRY
    types = tuple(registry)

    # Hoist ONE embedder across this call's main loop (lode-j5r2), rather than
    # let each `embed` job's handler build its own. Guarded on identity, not
    # job type membership: only swap in the shared instance when the
    # registered "embed" handler IS the real `_embed_handler` -- a test that
    # injects its own fake "embed" handler (e.g. `_noop_registry()`, or a
    # handler counting calls) must see that handler unchanged, never a
    # `functools.partial` wrapper it didn't ask for. `run_registry` is a
    # shallow copy so the module-level `_REGISTRY` singleton is never mutated.
    #
    # The guard fails OPEN -- a non-matching handler silently costs the
    # amortization rather than erroring -- so what keeps it honest is
    # test_drain_shares_one_embedder_across_all_embed_jobs_in_the_loop, which
    # drives the REAL registry and goes red the moment this stops matching.
    run_registry = registry
    if registry.get("embed") is _embed_handler:
        if embedder is None:
            from lode.embedding import FastEmbedEmbedder

            embedder = FastEmbedEmbedder(settings)
        # Retry a FAILED HF revision probe once per drain() call -- once per
        # poll tick of a --loop/--wait session, not once per job -- so a
        # single failed probe no longer latches model_revision = NULL for a
        # shared embedder's whole process lifetime (lode-fxse; the
        # accepted-but-unfixed half of lode-j5r2's trade, docs/decisions.md).
        # Unconditional, including for the instance just constructed above:
        # what to re-arm is FastEmbedEmbedder.reset_revision_probe()'s own
        # decision (a no-op on a fresh instance, and on a SUCCESSFUL prior
        # probe), which keeps worker.py blind to the embedder's internal probe
        # state -- never reach into `_revision_probed` here. Duck-typed, not
        # required, exactly like _embedder_model_revision's model_revision()
        # probe; tests/test_network_guard.py pins that the shared test stub
        # mirrors this method, and tests/test_worker.py's real_embedder test
        # pins that the string below still names a method the real class has.
        reset_probe = getattr(embedder, "reset_revision_probe", None)
        if reset_probe is not None:
            reset_probe()
        # Same hoist for the VectorStore (lode-2brb) -- see the docstring above.
        if store is None:
            from lode.vectorstore import VectorStore

            store = VectorStore(_lance_dir(db_path), settings)
        run_registry = dict(registry)
        run_registry["embed"] = functools.partial(
            _embed_handler, embedder=embedder, store=store
        )

    # Batch pre-steps: collect in-flight batches, then submit pending enrich jobs.
    #
    # A permanent, user-actionable failure here (AuthError — docs/storage.md
    # "Transient vs. permanent job failures") must still reach the caller, but it
    # must NOT abort the credential-free work below (lode-9yy review). `embed`
    # jobs are produced by the LOCAL fastembed model and have nothing to do with
    # Anthropic credentials, yet both pre-steps run BEFORE the main claim/run
    # loop. Letting the raise unwind drain() from here would mean an unkeyed
    # user's embed jobs never drain again — every `lode work` would abort on the
    # enrich pre-step (a pending enrich job is enqueued by every `add`, so one is
    # essentially always there) before a single embed ran, silently killing the
    # dense half of retrieval. That trades "enrich is retried forever" for "the
    # whole queue stops", which is strictly worse.
    #
    # lode-5zqa: the identical starvation applies to a STUCK batch (a poll that
    # keeps failing on the same bad data), not just a missing credential -- which
    # is why the catches below degrade the stuck step rather than aborting the
    # pass (see "Both catches are bare `Exception`" below for how far that
    # widening now goes, per lode-2mnj). That does not by itself make the batch
    # un-stuck -- lode-u6he's consecutive-failure budget does; see drain's
    # docstring.
    #
    # lode-knnt: each pre-step gets its OWN try, so a collect-side failure no
    # longer also skips the submit step. `pre_step_failure` keeps whichever raised
    # FIRST; a second one is dropped rather than overwriting it.
    #
    # Both catches are bare `Exception` (lode-2mnj widened the submit arm to
    # match collect's lode-knnt fix). The invariant drain needs is not
    # type-scoped at all: NO pre-step may abort the pass before the
    # credential-free embed work runs. `_batch_submit_enrich` already handles
    # every failure it can classify internally (AuthError/LLMAuthError: reset
    # + re-raise; any other API failure: revert + return 0, no raise) -- so
    # anything reaching THIS try is by definition unclassified (e.g. the
    # pending-jobs SELECT, the deferred imports, or the pre-claim CAS loop,
    # all of which sit outside `_batch_submit_enrich`'s own try), and
    # stash-and-re-raise is strictly better than letting it abort the pass.
    # WHY is owned by docs/storage.md "Transient vs. permanent job failures";
    # _batch_collect_enrich's own docstring covers the loop side.
    #
    # So: stash it, finish the work that CAN succeed, and re-raise at the end.
    # The main loop drains `embed` ahead of `enrich` (_claim_one orders on type),
    # so the embeds land before any residual enrich job re-raises out of run_one.
    # Progress instrumentation (lode-olmi.15): the three potentially-slow steps
    # below -- the two batch pre-steps and the main claim/run loop -- each log a
    # "starting"/heartbeat/"done" line via op_progress so a plain `lode work`
    # always shows which one is currently running, rather than the prior silence.
    # The reclaim/reset sweeps between them are left uninstrumented on purpose:
    # they are fast local UPDATEs with no network or model call to stall on.
    heartbeat_interval_s = settings.progress_heartbeat_interval_s
    pre_step_failure: Exception | None = None
    try:
        with op_progress(
            "drain.batch_collect", heartbeat_interval_s=heartbeat_interval_s
        ):
            _batch_collect_enrich(
                conn, settings, _client=_batch_client, outcomes=outcomes
            )
    except Exception as exc:
        log.debug("drain: batch_collect pre-step failed", exc_info=True)
        if pre_step_failure is None:
            pre_step_failure = exc

    try:
        with op_progress(
            "drain.batch_submit", heartbeat_interval_s=heartbeat_interval_s
        ):
            _batch_submit_enrich(conn, settings, _client=_batch_client)
    except Exception as exc:
        log.debug("drain: batch_submit pre-step failed", exc_info=True)
        if pre_step_failure is None:
            pre_step_failure = exc

    reclaimed = _reclaim_stale_running(conn, settings)
    if reclaimed:
        log.warning("reclaimed %d stale 'running' job(s) (possible crash)", reclaimed)

    now = jobs.now_iso()
    reset = _reset_retryable(conn, now)
    if reset:
        log.debug("reset %d overdue failed job(s) to pending", reset)

    processed = 0
    with op_progress("drain.run_jobs", heartbeat_interval_s=heartbeat_interval_s):
        while True:
            now = jobs.now_iso()
            job_id = _claim_one(conn, types, now)
            if job_id is None:
                break
            log.info("drain.run_jobs: running job %s", job_id)
            run_one(conn, job_id, db_path, settings, run_registry, outcomes=outcomes)
            processed += 1

    # The credential-free work is done; now surface the failure a batch pre-step
    # stashed (if a residual enrich job in the main loop above didn't already
    # re-raise it out of run_one first). NOT necessarily a permanent one since
    # lode-2mnj: both arms catch bare `Exception`, so this may equally be an
    # unclassified/transient fault -- the taxonomy decides how `lode work`
    # RENDERS it, not whether drain surfaces it.
    if pre_step_failure is not None:
        raise pre_step_failure

    return processed


# ---------------------------------------------------------------------------
# Embed handler (registered at module load)
# ---------------------------------------------------------------------------


def _embed_handler(
    conn: sqlite3.Connection,
    target_version: str,
    db_path: Path,
    settings: Settings,
    *,
    embedder: Embedder | None = None,
    store: VectorStore | None = None,
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
    :class:`~lode.lexical.LexicalCacheBackend` injected into ``cli/add.py``'s ``add``
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

    ``embedder``, if given, is threaded straight through to
    :func:`lode.embedding.embed`'s own ``embedder=`` seam instead of letting it
    construct a fresh one (lode-j5r2) — this is the seam :func:`drain` binds to
    share one instance across a whole drain; see its docstring for why. ``None``
    (the default) preserves the per-call construction, so a caller that invokes
    this handler directly is unaffected.

    ``store`` is threaded straight through to :func:`lode.embedding.embed`'s
    own ``store=`` seam (lode-2brb), bound by :func:`drain` the same way it
    binds ``embedder``. ``None`` preserves the per-call construction.
    """
    from lode.embedding import embed
    from lode.externals import gate_reenrich

    # Vector leg: chunk + embed + persist passage rows + store vectors in LanceDB.
    count = embed(
        conn,
        target_version,
        lance_dir=_lance_dir(db_path),
        settings=settings,
        embedder=embedder,
        store=store,
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
    claimed_at: str | None,
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

    If ``target_external_id`` already has an ``ok`` head snapshot **older**
    than ``claimed_at`` (i.e. a *later* refresh — ``lode-w0h.6``'s staleness
    policy, not the paste-triggered first draw-down — is the one that
    exhausted retries), this still moves the head to a tombstone: the
    dead-letter terminal means "retrying will not help right now", and
    docs/externals.md's TRANSIENT-failure row commits to writing a tombstone
    on ``dead`` unconditionally, with no "unless there's prior content"
    carve-out. See ``docs/decisions.md`` for the rationale (recorded
    alongside the (a) worker-hook vs (b) reconcile-sweep mechanism choice).

    **Late-success guard (lode-uda1):** the one case that carve-out does NOT
    cover, and where this hook must NOT tombstone: ``target_external_id``'s
    head is already an ``ok`` snapshot fetched *at or after* ``claimed_at``.
    That can only mean a fetch that started at-or-after this exact job's
    claim already landed real content — either this same job's own handler,
    still in flight when :func:`_reclaim_stale_running` dead-lettered it out
    from under itself (the race lode-uda1 exists to close: the reclaim's
    SELECT and this hook interleave between the handler's snapshot commit and
    its own terminal ``UPDATE``), or some other refresh that has since
    succeeded. Either way the dead-letter verdict is now stale — a guess that
    a fact has already overtaken — and tombstoning would silently replace
    successfully-fetched content with an absorbing tombstone head that
    reconcile's refresh/embed sweeps (both ``AND s.status != 'tombstone'``)
    would then never revisit. ``claimed_at`` of ``None`` (a legacy/never-
    stamped job row) disables the guard entirely — matches the unconditional
    behavior this hook always had before lode-uda1, since there is no claim
    timestamp to compare against.

    **The guard is atomic with the write (lode-elc8), not a separate read.**
    ``claimed_at`` is passed straight through to
    :func:`~lode.externals.ingest_snapshot`'s ``skip_if_head_at_or_after``,
    which does the head check *after* its own transaction has already taken
    SQLite's write lock — see that function's docstring for the mechanism
    and ``docs/storage.md`` for the empirical verification. Before lode-elc8
    this hook read the head via a separate, unprotected ``SELECT`` *before*
    ever calling ``ingest_snapshot`` (which opens its own independent
    transaction), so a real snapshot committed in the gap between that read
    and this write was still clobbered — a residual window lode-uda1's own
    writeup correctly flagged as narrowed, not closed. It is now closed
    outright, with no new transaction-control primitive (``BEGIN IMMEDIATE``
    was considered and rejected as unnecessary — see ``ingest_snapshot``'s
    docstring).

    **Both sides of that comparison are the same clock (lode-bmg9).** The
    comparison itself no longer lives in this function — since lode-elc8 it
    is inside :func:`~lode.externals.ingest_snapshot`'s
    ``skip_if_head_at_or_after`` guard — but its two operands are still
    ``snapshots.fetched_at`` and ``jobs.claimed_at``, and both come from
    :func:`lode.jobs.now_iso`: ``ingest_snapshot`` stamps ``fetched_at``
    explicitly rather than falling through to the schema's raw SQLite
    DEFAULT. Before lode-bmg9 they did not: ``fetched_at`` was
    ``CLOCK_REALTIME`` while ``claimed_at`` was the forward-ratcheted queue
    clock, which after a backward wall-clock step can read *ahead* of real
    time — making the ``>=`` test fail to fire for a real snapshot that
    genuinely landed after the claim, and the tombstone would clobber it. See
    ``docs/storage.md`` for the closed-residual writeup.

    Deferred import mirrors :func:`_refresh_handler`: keeps the
    httpx/trafilatura-adjacent ``lode.drawdown``/``lode.externals`` import
    cost off code paths where no ``refresh`` job has ever dead-lettered.

    **Generic over ``source_type`` (lode-gpzn.13).** This hook is shared by
    every connector that reuses the ``refresh`` job type (today: web; the
    Atlassian connectors, gpzn.3/gpzn.4, are next) — it must never assume
    ``target_external_id`` is a web source. If ``target_external_id`` already
    has an ``externals`` row (the Atlassian connectors' detection step
    persists one *synchronously*, source_type included, before their first
    ``refresh`` job is even enqueued — ``docs/decisions.md``'s Atlassian
    refinement A), its **existing** ``source_type`` is reused so the
    tombstone record never overwrites a JIRA/Confluence source with ``web``.
    Only when no row exists yet — today, only possible for a web target
    whose very first fetch dies before any snapshot is ever written, since
    :func:`lode.drawdown.detect_and_enqueue_drawdown` does not pre-create the
    row the way the Atlassian detection step will — does this fall back to
    :data:`lode.drawdown.SOURCE_TYPE_WEB`. ``ingest_snapshot``'s own upsert
    (``INSERT ... ON CONFLICT (external_id) DO NOTHING``) already leaves an
    existing row's ``source_type`` untouched regardless of what is passed,
    so this lookup does not change behavior for an existing row — it exists
    so the value passed in is never a misleading hardcoded ``web``, and so a
    first-write for a non-web target (should one ever reach this hook with
    no pre-created row) is labeled correctly instead of silently as ``web``.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.externals import ingest_snapshot, tombstone_body

    existing = conn.execute(
        "SELECT source_type FROM externals WHERE external_id = ?",
        (target_external_id,),
    ).fetchone()
    source_type = existing[0] if existing is not None else SOURCE_TYPE_WEB

    result = ingest_snapshot(
        conn,
        target_external_id,
        source_type,
        tombstone_body(f"dead: {last_error}"),
        status="tombstone",
        settings=settings,
        skip_if_head_at_or_after=claimed_at,
    )
    if result is None:
        # Can only happen when claimed_at was not None (ingest_snapshot's
        # guard only activates then) -- i.e. the guard fired.
        log.info(
            "refresh dead-letter hook for %s skipped: head snapshot already "
            "'ok' and fetched at-or-after this job's claim (%s) -- a real "
            "fetch beat the dead-letter verdict",
            target_external_id,
            claimed_at,
        )


# Register the refresh dead-letter hook on module load.
register_dead_letter("refresh", _refresh_dead_letter_hook)
