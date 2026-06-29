"""Enqueue derive jobs for a freshly-saved version (lode-y42.1).

The capture path stays instant by doing **no AI work** itself: it persists the
version (:func:`lode.versions.save`) and then drops the *derived* work onto the
durable ``jobs`` queue (``docs/storage.md`` "The async work queue") for the
workers that land later. This module is the thin enqueue seam.

Two derive jobs are enqueued per captured version, in the doc's priority order
(``embed > enrich``):

- ``embed`` — fast, local, high priority; chunk + embed so semantic recall lands
  in seconds.
- ``enrich`` — slow, Claude (tags / entities / inferred edges); may lag.

``refresh(external)`` arrives with the connectors step, not from a note capture,
so it is not enqueued here.

The save and the enqueue are **separate transactions** here: the lane fence for
``lode-y42.1`` forbids editing ``versions.py`` to fold the enqueue into the save's
single transaction (the shape ``docs/storage.md`` ultimately wants). The
**reconciliation scan** (``docs/storage.md`` — re-enqueue any head version missing
derived work) is the self-healing net that covers the brief save/enqueue gap a
crash between the two could open; every job is idempotent by key, so a re-enqueue
is safe.
"""

import sqlite3

#: Derive job types enqueued on every capture, in priority order
#: (``docs/storage.md`` "embed > enrich"). ``refresh`` arrives with connectors.
DERIVE_JOB_TYPES = ("embed", "enrich")


def enqueue_derive_jobs(conn: sqlite3.Connection, target_version: str) -> None:
    """Insert one pending job per derive type for ``target_version``, in one txn.

    Each row lands with the schema defaults (``status='pending'``, ``attempts=0``);
    ``prompt_ver`` is left NULL for the worker/reconciliation pass to stamp. The
    ``with conn:`` wraps both inserts so a captured version never gets a partial
    set of derive jobs.

    The INSERT uses ``ON CONFLICT DO NOTHING`` against the partial unique index
    ``idx_jobs_live`` (``src/lode/schema.sql``): a duplicate enqueue of the same
    live (pending/running) ``(type, target_version[, prompt_ver])`` job is a
    no-op. Re-enqueue after the prior job is ``done``/``dead`` IS allowed because
    the index is scoped to live statuses only (``docs/storage.md`` §E2 idempotency
    key decisions, pinned 2026-06-28).
    """
    with conn:
        conn.executemany(
            "INSERT INTO jobs (type, target_version) VALUES (?, ?)"
            " ON CONFLICT DO NOTHING",
            [(job_type, target_version) for job_type in DERIVE_JOB_TYPES],
        )
