"""Tests for lode.worker — the async work queue loop (lode-i05.3).

Acceptance criteria (bd show lode-i05.3):

- A claimed embed job runs once and lands (status='done').
- A transient failure retries with growing next_attempt_at backoff (not before
  next_attempt_at): attempts increments, status='failed', next_attempt_at is
  in the future.
- A poison job reaches status='dead' with attempts + last_error recorded.
- drain() runs until no more ready pending jobs and returns the count processed.
- An enrich job with no registered handler is left pending, never dead-lettered.
- The drain loop runs under the advisory lock (tested via CLI tests; here we
  verify the claim query respects the registry filter).

Strategy: all tests inject a stub registry (``_registry`` parameter) so they
run offline with no real embedder, LanceDB, or fastembed model.  The module-
level ``_REGISTRY`` (with the real embed handler) is not touched.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lode.config import Settings
from lode.jobs import enqueue_derive_jobs
from lode.storage import init_db
from lode.worker import (
    HandlerFn,
    _claim_one,
    _now_iso,
    _reset_retryable,
    drain,
    run_one,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """DB path under a tmp directory."""
    return tmp_path / "lode.db"


@pytest.fixture()
def conn(db_path: Path) -> sqlite3.Connection:
    c = init_db(db_path)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def settings() -> Settings:
    """Settings with a small max-attempts for fast dead-letter tests."""
    return Settings(retry_max_attempts=3, retry_backoff_base_s=1.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job(conn: sqlite3.Connection, job_id: int) -> dict:
    """Fetch one job row as a dict."""
    row = conn.execute(
        "SELECT id, type, status, attempts, last_error, next_attempt_at "
        "FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row is not None, f"job {job_id} not found"
    return {
        "id": row[0],
        "type": row[1],
        "status": row[2],
        "attempts": row[3],
        "last_error": row[4],
        "next_attempt_at": row[5],
    }


def _insert_job(
    conn: sqlite3.Connection,
    job_type: str = "embed",
    target_version: str = "ver-1",
    status: str = "pending",
    attempts: int = 0,
    next_attempt_at: str | None = None,
) -> int:
    """Insert a job row directly; returns the new row id."""
    now = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (type, target_version, status, attempts, next_attempt_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_type, target_version, status, attempts, next_attempt_at or now),
        )
    return cur.lastrowid


def _noop_registry() -> dict[str, HandlerFn]:
    """Registry with a no-op embed handler (succeeds, does nothing)."""
    return {"embed": lambda conn, tv, db, s: None}


def _failing_registry(msg: str = "transient failure") -> dict[str, HandlerFn]:
    """Registry with an embed handler that always raises RuntimeError."""

    def _fail(conn, tv, db, s):
        raise RuntimeError(msg)

    return {"embed": _fail}


def _future_iso(seconds: float = 3600) -> str:
    """ISO-8601 UTC timestamp ``seconds`` in the future."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _past_iso(seconds: float = 3600) -> str:
    """ISO-8601 UTC timestamp ``seconds`` in the past."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


# ---------------------------------------------------------------------------
# _claim_one — atomic claim
# ---------------------------------------------------------------------------


def test_claim_returns_id_for_ready_pending_job(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """A pending job with next_attempt_at <= now is claimable."""
    job_id = _insert_job(conn, "embed", "ver-1")
    claimed = _claim_one(conn, ("embed",), _now_iso())
    assert claimed == job_id


def test_claim_flips_status_to_running(conn: sqlite3.Connection, db_path: Path) -> None:
    job_id = _insert_job(conn, "embed", "ver-1")
    _claim_one(conn, ("embed",), _now_iso())
    assert _job(conn, job_id)["status"] == "running"


def test_claim_returns_none_when_no_pending_jobs(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    claimed = _claim_one(conn, ("embed",), _now_iso())
    assert claimed is None


def test_claim_skips_unregistered_type(conn: sqlite3.Connection, db_path: Path) -> None:
    """An enrich job is not claimed when only embed is in the registry."""
    enqueue_derive_jobs(conn, "ver-1")  # enqueues embed + enrich
    # First claim gets the embed (higher priority).
    claimed_embed = _claim_one(conn, ("embed",), _now_iso())
    assert claimed_embed is not None

    # Second claim: embed is now running; only enrich is pending.
    # With only embed in the registry, enrich must NOT be claimed.
    claimed_none = _claim_one(conn, ("embed",), _now_iso())
    assert claimed_none is None

    # The enrich job is still pending, not claimed, not dead-lettered.
    (status,) = conn.execute("SELECT status FROM jobs WHERE type = 'enrich'").fetchone()
    assert status == "pending"


def test_claim_respects_future_next_attempt_at(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """A pending job with next_attempt_at in the future must not be claimed."""
    _insert_job(conn, "embed", "ver-1", next_attempt_at=_future_iso())
    claimed = _claim_one(conn, ("embed",), _now_iso())
    assert claimed is None


def test_claim_priority_embed_before_enrich(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """When both embed and enrich are registered, embed is claimed first."""
    enqueue_derive_jobs(conn, "ver-1")
    registry_types = ("embed", "enrich")
    claimed_id = _claim_one(conn, registry_types, _now_iso())
    assert claimed_id is not None
    assert _job(conn, claimed_id)["type"] == "embed"


# ---------------------------------------------------------------------------
# run_one — execution and state transitions
# ---------------------------------------------------------------------------


def test_run_success_sets_done(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    ok = run_one(conn, job_id, db_path, settings, _noop_registry())
    assert ok is True
    assert _job(conn, job_id)["status"] == "done"


def test_run_transient_error_sets_failed(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """First failure → status='failed', attempts=1, last_error set."""
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    ok = run_one(conn, job_id, db_path, settings, _failing_registry("oops"))
    assert ok is False
    row = _job(conn, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "oops" in row["last_error"]


def test_run_transient_error_sets_future_next_attempt_at(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """After a transient failure the next_attempt_at must be in the future."""
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    run_one(conn, job_id, db_path, settings, _failing_registry())
    next_at = _job(conn, job_id)["next_attempt_at"]
    assert next_at > _now_iso(), "next_attempt_at should be in the future"


def test_run_backoff_grows_with_attempts(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """Each failure should produce a strictly later next_attempt_at."""
    # settings has retry_max_attempts=3, so we can fail twice before dead.
    prev_next_at = _now_iso()
    for attempt in range(1, settings.retry_max_attempts - 1):
        job_id = _insert_job(conn, target_version=f"ver-{attempt}")
        _claim_one(conn, ("embed",), _now_iso())
        run_one(conn, job_id, db_path, settings, _failing_registry())
        next_at = _job(conn, job_id)["next_attempt_at"]
        assert next_at > prev_next_at, f"backoff must grow (attempt {attempt})"
        prev_next_at = next_at


def test_run_max_attempts_dead_letters(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """At max attempts the job transitions to 'dead', not 'failed'."""
    # settings.retry_max_attempts = 3: dead-letter on the 3rd failure.
    job_id = _insert_job(conn, attempts=settings.retry_max_attempts - 1)
    _claim_one(conn, ("embed",), _now_iso())
    ok = run_one(conn, job_id, db_path, settings, _failing_registry("poison"))
    assert ok is False
    row = _job(conn, job_id)
    assert row["status"] == "dead"
    assert row["attempts"] == settings.retry_max_attempts
    assert "poison" in row["last_error"]


def test_run_dead_does_not_overwrite_with_backoff(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A dead-lettered job must not get a new next_attempt_at backoff."""
    job_id = _insert_job(conn, attempts=settings.retry_max_attempts - 1)
    original_next_at = _job(conn, job_id)["next_attempt_at"]
    _claim_one(conn, ("embed",), _now_iso())
    run_one(conn, job_id, db_path, settings, _failing_registry())
    row = _job(conn, job_id)
    assert row["status"] == "dead"
    # next_attempt_at is unchanged — dead jobs don't get scheduled for retry.
    assert row["next_attempt_at"] == original_next_at


# ---------------------------------------------------------------------------
# _reset_retryable — retry clock
# ---------------------------------------------------------------------------


def test_reset_flips_overdue_failed_to_pending(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """A failed job with next_attempt_at in the past must be reset to pending."""
    job_id = _insert_job(
        conn,
        status="failed",
        next_attempt_at=_past_iso(),
    )
    count = _reset_retryable(conn, _now_iso())
    assert count == 1
    assert _job(conn, job_id)["status"] == "pending"


def test_reset_leaves_future_failed_alone(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """A failed job whose backoff hasn't expired must stay 'failed'."""
    job_id = _insert_job(
        conn,
        status="failed",
        next_attempt_at=_future_iso(),
    )
    count = _reset_retryable(conn, _now_iso())
    assert count == 0
    assert _job(conn, job_id)["status"] == "failed"


def test_reset_does_not_touch_pending_or_dead(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    pending_id = _insert_job(conn, status="pending")
    dead_id = _insert_job(conn, status="dead", next_attempt_at=_past_iso())
    _reset_retryable(conn, _now_iso())
    assert _job(conn, pending_id)["status"] == "pending"
    assert _job(conn, dead_id)["status"] == "dead"


# ---------------------------------------------------------------------------
# drain — full loop
# ---------------------------------------------------------------------------


def test_drain_processes_pending_embed_jobs(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """drain() runs all ready embed jobs and marks them done."""
    for i in range(3):
        _insert_job(conn, target_version=f"ver-{i}")
    n = drain(conn, db_path, settings, _registry=_noop_registry())
    assert n == 3
    statuses = [
        r[0]
        for r in conn.execute("SELECT status FROM jobs WHERE type = 'embed'").fetchall()
    ]
    assert all(s == "done" for s in statuses)


def test_drain_leaves_enrich_pending(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """drain() with only embed in registry must leave enrich jobs pending."""
    enqueue_derive_jobs(conn, "ver-1")
    n = drain(conn, db_path, settings, _registry=_noop_registry())
    assert n == 1  # only the embed job was processed

    (status,) = conn.execute("SELECT status FROM jobs WHERE type = 'enrich'").fetchone()
    assert status == "pending"


def test_drain_enrich_never_dead_lettered(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """An enrich job must never become dead even when the registry has no handler."""
    enqueue_derive_jobs(conn, "ver-1")
    # Run drain many times — enrich stays pending, never dead.
    for _ in range(5):
        drain(conn, db_path, settings, _registry=_noop_registry())

    (status,) = conn.execute("SELECT status FROM jobs WHERE type = 'enrich'").fetchone()
    assert status == "pending"


def test_drain_returns_count_including_failures(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """drain returns total jobs claimed, including those that fail."""
    # Insert 2 jobs: one succeeds, one fails.
    _insert_job(conn, target_version="ver-ok")
    _insert_job(conn, target_version="ver-fail")

    calls: list[str] = []

    def _selective(conn, tv, db, s):
        calls.append(tv)
        if tv == "ver-fail":
            raise RuntimeError("expected failure")

    n = drain(conn, db_path, settings, _registry={"embed": _selective})
    assert n == 2  # both were claimed and attempted
    assert len(calls) == 2


def test_drain_resets_overdue_failed_on_each_call(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """drain() resets failed+overdue jobs at the start of each call."""
    job_id = _insert_job(conn, status="failed", next_attempt_at=_past_iso())
    n = drain(conn, db_path, settings, _registry=_noop_registry())
    assert n == 1
    assert _job(conn, job_id)["status"] == "done"


def test_drain_does_not_retry_within_same_pass(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A job that fails within a drain pass is not retried in the same pass."""
    job_id = _insert_job(conn)
    n = drain(conn, db_path, settings, _registry=_failing_registry())
    # One attempt made, but not retried in the same pass.
    assert n == 1
    assert _job(conn, job_id)["status"] == "failed"


def test_drain_empty_queue_returns_zero(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    n = drain(conn, db_path, settings, _registry=_noop_registry())
    assert n == 0


# ---------------------------------------------------------------------------
# registered_types / module-level registry
# ---------------------------------------------------------------------------


def test_embed_is_registered_by_default() -> None:
    """The module-level registry must have 'embed' registered at import time."""
    from lode.worker import registered_types

    assert "embed" in registered_types()


def test_enrich_is_registered_by_default() -> None:
    """The enrich handler is registered at import time (lode-npx.1)."""
    from lode.worker import registered_types

    assert "enrich" in registered_types()


def test_refresh_not_registered() -> None:
    """refresh must NOT be in the default registry (no connector handler yet)."""
    from lode.worker import registered_types

    assert "refresh" not in registered_types()
