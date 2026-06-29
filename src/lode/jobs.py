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

``refresh(external)`` arrives with the connectors step, not from a note capture,
so it is not enqueued here.

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


def enqueue_derive_jobs(conn: sqlite3.Connection, target_version: str) -> None:
    """Insert one pending job per derive type for ``target_version`` on ``conn``.

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
    """
    conn.executemany(
        "INSERT INTO jobs (type, target_version) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [(job_type, target_version) for job_type in DERIVE_JOB_TYPES],
    )
