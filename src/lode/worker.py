"""Worker loop: claim → run → retry/backoff → dead-letter (lode-i05.3).

ONE-SHOT DRAIN by default: acquire the advisory lock (i05.2), reset overdue
retries, then claim+run ready pending jobs until none remain and exit.
``--loop`` / ``--watch`` polls on an interval (exposed by :func:`drain` via the
``lode work`` Typer command in :mod:`lode.cli`).

**Handler registry** dispatches on ``jobs.type``:

- ``embed`` — registered now; runs the existing chunk+embed+FTS path
  (:func:`lode.embedding.embed` for the vector leg,
  :class:`lode.lexical.LexicalIndex` for the BM25 leg). Idempotent: the same
  head version can be re-embedded and converges to the same state.
- ``enrich`` / ``refresh`` — *no handler*; jobs of unregistered types are
  left pending and **never claimed, never dead-lettered** until their handlers
  arrive. The claim query filters to registered types, so they accumulate
  harmlessly (lode-i05.3 scope fence).

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

**Crash recovery note**: if the worker crashes mid-run a job can be left in
``status='running'``. The reconciliation scan (i05.4, not yet landed) will
reset such orphaned running jobs; until then they remain stuck.
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


def _claim_one(
    conn: sqlite3.Connection,
    types: tuple[str, ...],
    now: str,
) -> int | None:
    """Atomically claim one ready pending job of a registered type.

    Selects the highest-priority, oldest ready job — ``status='pending' AND
    next_attempt_at <= now AND type IN types`` — ordered by type priority
    (``embed`` before ``enrich``) then ``created``, and flips it to
    ``'running'`` with an asserted CAS update.  Returns the job ``id`` or
    ``None`` if nothing is ready.
    """
    if not types:
        return None
    placeholders = ", ".join("?" for _ in types)
    row = conn.execute(
        f"SELECT id FROM jobs "
        f"WHERE status = 'pending' AND next_attempt_at <= ? "
        f"AND type IN ({placeholders}) "
        f"ORDER BY "
        f"CASE type WHEN 'embed' THEN 0 WHEN 'enrich' THEN 1 ELSE 2 END, "
        f"created "
        f"LIMIT 1",
        (now, *types),
    ).fetchone()
    if row is None:
        return None
    job_id = row[0]
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'pending'",
            (job_id,),
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


def drain(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings | None = None,
    _registry: dict[str, HandlerFn] | None = None,
) -> int:
    """Claim+run all ready pending jobs until none remain.

    Calls :func:`_reset_retryable` once at the start to pick up overdue
    retries (``status='failed' AND next_attempt_at <= now``), then loops
    :func:`_claim_one` → :func:`run_one` until nothing is claimable.

    Returns the total number of jobs claimed and run (including failures and
    dead-letters, not just successful completions).

    ``_registry`` is injectable for tests; production callers omit it and the
    module-level :data:`_REGISTRY` is used.
    """
    settings = settings or Settings()
    registry = _registry if _registry is not None else _REGISTRY
    types = tuple(registry)

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
    """Embed handler: chunk+embed (vector leg) + FTS (lexical leg).

    The sole path for embedding after capture (lode-x6r.5): capture enqueues
    the ``embed`` derive job via :func:`~lode.jobs.enqueue_derive_jobs`; this
    handler drains it.  Deferred imports avoid paying the fastembed / LanceDB
    cost on commands that never embed.

    :func:`lode.embedding.embed` chunks the body, upserts the ``passages``
    rows, embeds each passage via the pinned local ONNX model, and replaces
    the version's vectors in LanceDB.  ``chunk`` is called a second time for
    the FTS leg — deterministic chunking produces the same passages, so this
    is idempotent.
    """
    from lode.chunking import chunk
    from lode.embedding import embed
    from lode.lexical import LexicalIndex

    # Vector leg: chunk + embed + persist passage rows + store vectors in LanceDB.
    embed(conn, target_version, lance_dir=_lance_dir(db_path), settings=settings)

    # Lexical leg: populate passages_fts for BM25 keyword search.
    row = conn.execute(
        "SELECT body FROM versions WHERE version_id = ?", (target_version,)
    ).fetchone()
    if row is not None:
        passages = chunk(row[0], target_version, settings=settings)
        LexicalIndex(conn).replace_passages(target_version, passages)


# Register the embed handler on module load.
register("embed", _embed_handler)
