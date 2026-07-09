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
"""

import sqlite3

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
