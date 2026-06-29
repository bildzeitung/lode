"""Tests for lode.jobs — the derive-job enqueue seam (lode-y42.1, lode-i05.6).

Covers: a capture enqueues exactly the embed + enrich derive jobs as pending
rows targeting the version (schema defaults applied, prompt_ver left NULL); the
enqueue is its own transaction (committed when it returns); and idempotency
constraints from lode-i05.6 — duplicate enqueue of a live (pending/running) job
is a no-op (ON CONFLICT DO NOTHING against idx_jobs_live), while re-enqueue
after done/dead IS allowed.
"""

from pathlib import Path

import pytest

from lode.jobs import DERIVE_JOB_TYPES, enqueue_derive_jobs
from lode.storage import init_db


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


def test_enqueues_embed_and_enrich_pending(conn) -> None:
    enqueue_derive_jobs(conn, "ver-1")
    rows = conn.execute(
        "SELECT type, target_version, status, attempts, prompt_ver, batch_handle "
        "FROM jobs ORDER BY type"
    ).fetchall()
    assert rows == [
        ("embed", "ver-1", "pending", 0, None, None),
        ("enrich", "ver-1", "pending", 0, None, None),
    ]


def test_next_attempt_at_column_is_set_on_enqueue(conn) -> None:
    """next_attempt_at must be present (non-NULL) on every newly-enqueued job."""
    enqueue_derive_jobs(conn, "ver-1")
    rows = conn.execute("SELECT next_attempt_at FROM jobs").fetchall()
    assert all(next_at is not None for (next_at,) in rows)


def test_priority_order_embed_before_enrich() -> None:
    # The doc's priority (embed > enrich) is encoded in the enqueue order.
    assert DERIVE_JOB_TYPES == ("embed", "enrich")


def test_enqueue_is_committed_when_it_returns(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    writer = init_db(db_path)
    try:
        enqueue_derive_jobs(writer, "ver-1")
    finally:
        writer.close()
    # A separate connection sees the rows -> the enqueue txn committed.
    reader = init_db(db_path)
    try:
        (n,) = reader.execute("SELECT COUNT(*) FROM jobs").fetchone()
    finally:
        reader.close()
    assert n == len(DERIVE_JOB_TYPES)


def test_distinct_versions_get_independent_job_sets(conn) -> None:
    enqueue_derive_jobs(conn, "ver-1")
    enqueue_derive_jobs(conn, "ver-2")
    (n1,) = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE target_version = ?", ("ver-1",)
    ).fetchone()
    (n2,) = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE target_version = ?", ("ver-2",)
    ).fetchone()
    assert (n1, n2) == (2, 2)


# --- Idempotency key tests (lode-i05.6) --------------------------------------


def test_duplicate_enqueue_of_live_embed_is_noop(conn) -> None:
    """Re-enqueue of the same pending embed job must be a no-op (lode-i05.6).

    embed jobs have NULL prompt_ver; the partial unique index uses
    COALESCE(prompt_ver, '') so NULL rows ARE deduplicated.
    """
    enqueue_derive_jobs(conn, "ver-1")
    enqueue_derive_jobs(conn, "ver-1")  # second call: ON CONFLICT DO NOTHING
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE type = 'embed' AND target_version = 'ver-1'"
    ).fetchone()
    assert n == 1  # still one row, not two


def test_duplicate_enqueue_of_live_enrich_is_noop(conn) -> None:
    """Re-enqueue of the same pending enrich job (with prompt_ver) is a no-op."""
    enqueue_derive_jobs(conn, "ver-1")
    enqueue_derive_jobs(conn, "ver-1")
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE type = 'enrich' AND target_version = 'ver-1'"
    ).fetchone()
    assert n == 1


def test_reenqueue_after_done_is_allowed(conn) -> None:
    """After a job completes (done), the same (type, version) can be re-enqueued.

    The partial index is scoped to pending/running only; done rows fall outside
    the WHERE clause and do not block a new enqueue.
    """
    enqueue_derive_jobs(conn, "ver-1")
    # Simulate completion: move both jobs to 'done'.
    with conn:
        conn.execute("UPDATE jobs SET status = 'done' WHERE target_version = 'ver-1'")
    # Re-enqueue should succeed (not be silently ignored).
    enqueue_derive_jobs(conn, "ver-1")
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE target_version = 'ver-1' AND status = 'pending'"
    ).fetchone()
    assert n == len(DERIVE_JOB_TYPES)  # fresh pending rows exist


def test_reenqueue_after_dead_is_allowed(conn) -> None:
    """After a job reaches dead (terminal), the same (type, version) can be re-enqueued.

    'dead' is the poison terminal at max-attempts; the partial index excludes it
    from the deduplication scope so re-derive is unblocked (lode-i05.6).
    """
    enqueue_derive_jobs(conn, "ver-1")
    # Simulate dead-letter: move both jobs to 'dead'.
    with conn:
        conn.execute("UPDATE jobs SET status = 'dead' WHERE target_version = 'ver-1'")
    # Re-enqueue should succeed.
    enqueue_derive_jobs(conn, "ver-1")
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE target_version = 'ver-1' AND status = 'pending'"
    ).fetchone()
    assert n == len(DERIVE_JOB_TYPES)


def test_dead_status_accepted_by_schema(conn) -> None:
    """The status CHECK must accept 'dead' (added in lode-i05.6)."""
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) VALUES (?, ?, ?)",
            ("embed", "ver-dead", "dead"),
        )
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE target_version = 'ver-dead'"
    ).fetchone()
    assert status == "dead"
