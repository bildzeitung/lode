"""Reconciliation scan: re-enqueue head versions missing fresh derived work (lode-i05.4).

The reconciliation scan is the **self-healing net** for crashes, dropped jobs, and
the tiny window between a version write and its enqueue (see ``docs/storage.md``
"Reconciliation scan on startup + periodically"). It runs:

- **at worker startup** — before the first drain pass, so any gap left by a
  crash or incomplete run is filled immediately;
- **periodically in ``--loop`` mode** — at the start of each drain tick, so
  the queue stays healthy over long-running worker sessions.

**Step registry** — mirrors :mod:`lode.worker`'s handler-registry shape:

- ``embed_gap`` — registered now (Phase A). Finds head versions missing a
  live (non-dead) embed job — i.e. the vector leg never ran, was dead-lettered,
  or somehow lost its job row — and re-enqueues an ``embed`` job for each.
  Signal re-keyed in lode-xyb: ``passages`` rows are now written synchronously
  on save, so their presence no longer implies vectors exist; the embed job
  status is the reliable proxy for "vector leg completed."
  Excludes soft-deleted (``op='delete'``) and purged (``purged_at IS NOT NULL``)
  heads.
- *enrich-gap step* — **not registered here**; E7 appends it once the
  enrichment tables and ``prompt_ver`` semantics exist. The seam is open.

**Idempotency** — each step re-enqueues via :func:`lode.jobs.enqueue_derive_jobs`,
which uses ``INSERT … ON CONFLICT DO NOTHING`` against the ``idx_jobs_live``
partial unique index (lode-i05.6). Running the scan repeatedly produces no
duplicate jobs: a version whose embed job is already pending or running is a
silent no-op at the INSERT level. Re-enqueue after ``done``/``dead`` IS allowed
(the index is scoped to ``pending``/``running`` only) — so a re-derive after a
crash or a prompt-ver bump is handled correctly.

**Single enqueue path** — steps call :func:`lode.jobs.enqueue_derive_jobs`
(optionally with a ``types`` subset), never a second hand-rolled INSERT. The
INSERT SQL lives in one place: ``jobs.py``.
"""

import logging
import sqlite3
from collections.abc import Callable

from lode import jobs

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------

#: Scan step signature: receives an open SQLite connection; returns the count of
#: gap versions found (each triggers a targeted ``enqueue_derive_jobs`` call).
#: Steps run within no outer transaction of their own — each step opens ``with
#: conn:`` for the batch enqueue internally.
StepFn = Callable[[sqlite3.Connection], int]

#: Module-level step registry — list of ``(name, fn)`` pairs in run order.
#:
#: Populated at module load by :func:`register_step`; the ``embed_gap`` step is
#: registered here. Tests inject a custom list into :func:`reconcile` instead of
#: touching this directly.
_STEPS: list[tuple[str, StepFn]] = []


def register_step(name: str, fn: StepFn) -> None:
    """Append ``fn`` to the module-level step registry under ``name``."""
    _STEPS.append((name, fn))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconcile(
    conn: sqlite3.Connection,
    steps: list[tuple[str, StepFn]] | None = None,
) -> int:
    """Run all registered scan steps; return the total count of gap versions found.

    Each step queries for a specific gap (e.g. "head version with no passages")
    and calls :func:`lode.jobs.enqueue_derive_jobs` for each gap version via
    ``ON CONFLICT DO NOTHING``, so the scan is safe to run at any time and any
    frequency.

    ``steps`` is injectable for tests; production callers omit it and the
    module-level :data:`_STEPS` list is used.  Returns ``0`` when no steps are
    registered or all steps find no gaps.
    """
    if steps is None:
        steps = _STEPS
    total = 0
    for name, step_fn in steps:
        count = step_fn(conn)
        if count:
            log.info("reconcile[%s]: %d gap version(s) enqueued", name, count)
        total += count
    return total


# ---------------------------------------------------------------------------
# Embed-gap step (registered at module load)
# ---------------------------------------------------------------------------


def _embed_gap_step(conn: sqlite3.Connection) -> int:
    """Embed gap: re-enqueue embed jobs for head versions missing a live embed job.

    **Gap signal (lode-xyb):** since ``passages`` + ``passages_fts`` are now
    written synchronously on save by :class:`~lode.lexical.LexicalCacheBackend`,
    a ``passages`` row existing no longer means "embed ran" — it just means "save
    ran."  The reliable signal for "embedding completed (vectors in LanceDB)" is a
    non-dead embed job: a ``pending``/``running``/``done``/``failed`` embed job for
    the version means the vector work is either in-flight or completed; a ``dead``
    (max-retries exhausted) job or the total absence of a job means the vector leg
    is missing.

    **Gap query:** live head versions — ``notes.head_version_id`` joined to
    ``versions``, where the head op is not ``'delete'`` (not a soft-delete
    tombstone) and ``purged_at IS NULL`` (not hard-deleted/purged) — with no
    embed job in status ``pending``, ``running``, ``done``, or ``failed``.  That
    is: no job at all, or all existing embed jobs are ``dead``.  Each such version
    is re-enqueued.

    **Enqueue:** calls :func:`lode.jobs.enqueue_derive_jobs` with
    ``types=("embed",)`` inside a single ``with conn:`` transaction.  The INSERT
    is ``ON CONFLICT DO NOTHING`` against ``idx_jobs_live``, so a version whose
    embed job is already pending or running produces no duplicate row — the scan
    is entirely idempotent.

    Returns the count of gap versions found (each triggered one
    ``enqueue_derive_jobs`` call; some may be no-ops for in-flight jobs).
    """
    gap_versions = conn.execute(
        """
        SELECT n.head_version_id
        FROM notes n
        JOIN versions v ON v.version_id = n.head_version_id
        WHERE n.head_version_id IS NOT NULL
          AND v.op != 'delete'
          AND v.purged_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.type = 'embed'
                AND j.target_version = n.head_version_id
                AND j.status != 'dead'
          )
        """
    ).fetchall()

    if not gap_versions:
        return 0

    # Batch-enqueue all gap versions in a single transaction; enqueue_derive_jobs
    # is a plain INSERT (no own txn) so the `with conn:` here is the boundary.
    with conn:
        for (version_id,) in gap_versions:
            jobs.enqueue_derive_jobs(conn, version_id, types=("embed",))

    return len(gap_versions)


# Register the embed-gap step on module load.
register_step("embed_gap", _embed_gap_step)
