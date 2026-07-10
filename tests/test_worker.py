"""Tests for lode.worker — the async work queue loop (lode-i05.3 / lode-npx.2).

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

Acceptance criteria (bd show lode-npx.2 — batch pre-steps):

- _batch_collect_enrich: polls in-flight batches, processes results when ended.
- _batch_submit_enrich: claims pending enrich jobs and submits to Batches API.
- drain() runs batch pre-steps before the main claim-run loop.

Strategy: all tests inject a stub registry (``_registry`` parameter) so they
run offline with no real embedder, LanceDB, or fastembed model.  The module-
level ``_REGISTRY`` (with the real embed handler) is not touched.  Batch pre-step
tests inject ``_batch_client`` (a MagicMock) so no real Anthropic calls are made.
"""

import sqlite3
import unittest.mock as mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lode.config import Settings
from lode.enrich import ENRICH_PROMPT_VER
from lode.jobs import enqueue_derive_jobs
from lode.storage import init_db
from lode.worker import (
    _REGISTRY,
    HandlerFn,
    _batch_collect_enrich,
    _batch_submit_enrich,
    _claim_one,
    _now_iso,
    _reclaim_stale_running,
    _reset_retryable,
    claim_and_run_one,
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
        "SELECT id, type, status, attempts, last_error, next_attempt_at, claimed_at, "
        "prompt_ver FROM jobs WHERE id = ?",
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
        "claimed_at": row[6],
        "prompt_ver": row[7],
    }


def _insert_job(
    conn: sqlite3.Connection,
    job_type: str = "embed",
    target_version: str = "ver-1",
    status: str = "pending",
    attempts: int = 0,
    next_attempt_at: str | None = None,
    claimed_at: str | None = None,
    batch_handle: str | None = None,
) -> int:
    """Insert a job row directly; returns the new row id."""
    now = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (type, target_version, status, attempts, "
            "next_attempt_at, claimed_at, batch_handle) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_type,
                target_version,
                status,
                attempts,
                next_attempt_at or now,
                claimed_at,
                batch_handle,
            ),
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


def test_claim_sets_claimed_at(conn: sqlite3.Connection, db_path: Path) -> None:
    """A claim stamps claimed_at (lode-aor) -- the signal _reclaim_stale_running uses."""
    job_id = _insert_job(conn, "embed", "ver-1")
    assert _job(conn, job_id)["claimed_at"] is None
    now = _now_iso()
    _claim_one(conn, ("embed",), now)
    assert _job(conn, job_id)["claimed_at"] == now


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


def test_claim_with_target_version_ignores_other_pending_jobs_of_same_type(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """``target_version`` scopes the claim to that version's job (lode-a3x).

    Without the filter, ``_claim_one`` claims the oldest pending job of the
    requested type(s) regardless of version -- the bug that got lode-npx.2
    bounced. Seed an older backlog job first, then claim scoped to a newer
    version's job and assert the backlog job is left untouched.
    """
    backlog_id = _insert_job(conn, "enrich", "ver-backlog")
    target_id = _insert_job(conn, "enrich", "ver-target")

    claimed = _claim_one(conn, ("enrich",), _now_iso(), target_version="ver-target")

    assert claimed == target_id
    assert _job(conn, target_id)["status"] == "running"
    assert _job(conn, backlog_id)["status"] == "pending"


def test_claim_with_target_version_returns_none_when_no_match(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """A pending job exists, but not for the requested version — no claim."""
    job_id = _insert_job(conn, "enrich", "ver-1")
    claimed = _claim_one(conn, ("enrich",), _now_iso(), target_version="ver-other")
    assert claimed is None
    assert _job(conn, job_id)["status"] == "pending"


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
    row = _job(conn, job_id)
    assert row["status"] == "done"
    # embed jobs never carry a prompt_ver (schema's job-identity design).
    assert row["prompt_ver"] is None


def test_run_enrich_success_stamps_prompt_ver(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A successful enrich job stamps prompt_ver=ENRICH_PROMPT_VER on 'done' (lode-q47).

    This is the fix for the enrich-gap thrash bug: before lode-q47 nothing
    ever stamped a job's own prompt_ver, so lode.reconcile's enrich-gap step
    had to infer "current" from a summary annotation instead — a signal that
    broke when Haiku legitimately returned an empty summary.
    """
    job_id = _insert_job(conn, "enrich", "ver-1")
    _claim_one(conn, ("enrich",), _now_iso())
    noop_enrich_registry: dict[str, HandlerFn] = {
        "enrich": lambda conn, tv, db, s: None
    }
    ok = run_one(conn, job_id, db_path, settings, noop_enrich_registry)
    assert ok is True
    row = _job(conn, job_id)
    assert row["status"] == "done"
    assert row["prompt_ver"] == ENRICH_PROMPT_VER


def test_run_appends_handler_outcome_to_outcomes_sink(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A handler's returned outcome string is appended to `outcomes` (lode-1gr.4).

    This is the channel a caller (lode.worker.drain, and ultimately 'lode
    work') uses to surface a per-job outcome line -- e.g. what the real
    _embed_handler returns ("embedded <short-id>: N passages").
    """
    job_id = _insert_job(conn, target_version="ver-outcome")
    _claim_one(conn, ("embed",), _now_iso())
    registry: dict[str, HandlerFn] = {
        "embed": lambda conn, tv, db, s: f"embedded {tv}: 3 passages"
    }
    outcomes: list[str] = []
    ok = run_one(conn, job_id, db_path, settings, registry, outcomes=outcomes)
    assert ok is True
    assert outcomes == ["embedded ver-outcome: 3 passages"]


def test_run_does_not_append_when_handler_returns_none(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A handler returning None (nothing to report) leaves `outcomes` empty."""
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    outcomes: list[str] = []
    ok = run_one(conn, job_id, db_path, settings, _noop_registry(), outcomes=outcomes)
    assert ok is True
    assert outcomes == []


def test_run_does_not_append_outcome_on_failure(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A failing handler never gets to return an outcome -- `outcomes` stays empty."""
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    outcomes: list[str] = []
    ok = run_one(
        conn, job_id, db_path, settings, _failing_registry(), outcomes=outcomes
    )
    assert ok is False
    assert outcomes == []


def test_run_outcomes_default_none_is_a_no_op(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """Omitting outcomes (the default) does not error -- purely additive param."""
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    registry: dict[str, HandlerFn] = {"embed": lambda conn, tv, db, s: "some outcome"}
    ok = run_one(conn, job_id, db_path, settings, registry)
    assert ok is True


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
# Dead-letter hook (lode-at8) — 'refresh' tombstones its external on 'dead'
# ---------------------------------------------------------------------------


def _always_raising_refresh_registry(
    msg: str = "connection refused",
) -> dict[str, HandlerFn]:
    """Registry with a 'refresh' handler that always raises RuntimeError.

    Mirrors :func:`_failing_registry`'s shape but for ``type='refresh'`` —
    stands in for a real :class:`~lode.webfetch.TransientFetchError` that
    keeps recurring across every retry (``lode.drawdown.refresh_external``
    lets any exception propagate uncaught, per its own tests), without
    pulling in httpx/trafilatura or hitting the network.
    """

    def _fail(conn, tv, db, s):
        raise RuntimeError(msg)

    return {"refresh": _fail}


def test_run_refresh_dead_letter_writes_tombstone_snapshot(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """lode-at8: a 'refresh' job that dead-letters leaves a tombstone snapshot.

    Before this fix: a 'refresh' job that exhausted its retries reached
    'dead' with no record against the external at all -- head_snapshot_id
    stayed NULL, indistinguishable from a draw-down still in flight (the
    ticket's exact reproduction). Now the worker's dead-letter hook
    (_refresh_dead_letter_hook) tombstones the external the same way a
    PERMANENT (non-retrying) fetch failure already would.
    """
    external_id = "https://example.com/dead-link"
    job_id = _insert_job(
        conn, "refresh", external_id, attempts=settings.retry_max_attempts - 1
    )
    _claim_one(conn, ("refresh",), _now_iso())
    ok = run_one(
        conn, job_id, db_path, settings, _always_raising_refresh_registry("timeout")
    )
    assert ok is False
    assert _job(conn, job_id)["status"] == "dead"

    row = conn.execute(
        "SELECT status, body FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert row is not None, "dead-lettered refresh job must leave a snapshot record"
    status, body = row
    assert status == "tombstone"
    assert "timeout" in body  # the job's last_error is folded into the tombstone

    (head_snapshot_id,) = conn.execute(
        "SELECT head_snapshot_id FROM externals WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert head_snapshot_id is not None, "head must point at the tombstone, not NULL"


def test_run_refresh_transient_failure_writes_no_tombstone(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A 'refresh' failure that still has a retry coming must not tombstone yet."""
    external_id = "https://example.com/retrying-link"
    job_id = _insert_job(conn, "refresh", external_id)  # attempts=0, retries left
    _claim_one(conn, ("refresh",), _now_iso())
    ok = run_one(
        conn, job_id, db_path, settings, _always_raising_refresh_registry("blip")
    )
    assert ok is False
    assert _job(conn, job_id)["status"] == "failed"

    row = conn.execute(
        "SELECT 1 FROM externals WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert row is None, "a still-retrying job must not create an externals row yet"


def test_reclaim_refresh_dead_letter_writes_tombstone_snapshot(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """The crash-reclaim path (lode-aor) fires the same dead-letter hook.

    A 'refresh' job stuck 'running' past the staleness timeout, already at
    max attempts, is reclaimed straight to 'dead' by _reclaim_stale_running
    -- this must tombstone the external exactly like run_one's own
    max-attempts gate does.
    """
    external_id = "https://example.com/crashed-link"
    job_id = _insert_job(
        conn,
        job_type="refresh",
        target_version=external_id,
        status="running",
        attempts=settings.retry_max_attempts - 1,
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )
    count = _reclaim_stale_running(conn, settings)
    assert count == 1
    assert _job(conn, job_id)["status"] == "dead"

    (status,) = conn.execute(
        "SELECT status FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert status == "tombstone"


def test_dead_letter_hook_exception_does_not_propagate(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """lode-at8: a raising dead-letter hook must not wedge the worker.

    The job's status is already durably committed to 'dead' before the hook
    runs, so a hook that raises degrades to the accepted "tombstone not
    written yet" gap rather than propagating out of run_one and aborting the
    drain loop (or bubbling out of the interactive `lode add`
    immediate-enrich path, which calls run_one directly with no wrapping try).
    """
    external_id = "https://example.com/hook-explodes"
    job_id = _insert_job(
        conn, "refresh", external_id, attempts=settings.retry_max_attempts - 1
    )
    _claim_one(conn, ("refresh",), _now_iso())

    def _boom(conn, target_version, last_error, settings):  # noqa: ARG001
        raise RuntimeError("hook blew up")

    with mock.patch.dict("lode.worker._DEAD_LETTER_HOOKS", {"refresh": _boom}):
        # Must NOT raise, despite the registered hook raising.
        ok = run_one(
            conn,
            job_id,
            db_path,
            settings,
            _always_raising_refresh_registry("timeout"),
        )

    assert ok is False
    assert _job(conn, job_id)["status"] == "dead"  # status transition still committed
    # The hook failed, so no tombstone was written -- the accepted degraded
    # state, not a propagated crash.
    row = conn.execute(
        "SELECT 1 FROM externals WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# claim_and_run_one — CLI immediate-enrich fast path (lode-npx.2)
# ---------------------------------------------------------------------------


def test_claim_and_run_one_runs_the_pending_job(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A ready pending job is claimed and run; returns True."""
    job_id = _insert_job(conn, "embed", "ver-1")
    ran = claim_and_run_one(
        conn, db_path, settings, ("embed",), _registry=_noop_registry()
    )
    assert ran is True
    assert _job(conn, job_id)["status"] == "done"


def test_claim_and_run_one_returns_false_when_nothing_pending(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """No ready job of the requested type(s) — a harmless no-op."""
    ran = claim_and_run_one(
        conn, db_path, settings, ("enrich",), _registry=_noop_registry()
    )
    assert ran is False


def test_claim_and_run_one_returns_false_when_already_claimed(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """Losing the claim race (e.g. to a concurrent `lode work`) is a no-op, not an error."""
    job_id = _insert_job(conn, "enrich", "ver-1")
    # Simulate a concurrent worker winning the claim first.
    _claim_one(conn, ("enrich",), _now_iso())
    ran = claim_and_run_one(
        conn, db_path, settings, ("enrich",), _registry=_noop_registry()
    )
    assert ran is False
    # The job is untouched by claim_and_run_one — still 'running' from the
    # earlier claim, not re-claimed or re-run.
    assert _job(conn, job_id)["status"] == "running"


def test_claim_and_run_one_failure_uses_normal_backoff_accounting(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A handler failure goes through run_one's own attempts/backoff — no hand-rolled retry."""
    job_id = _insert_job(conn, "embed", "ver-1")
    ran = claim_and_run_one(
        conn, db_path, settings, ("embed",), _registry=_failing_registry("boom")
    )
    assert ran is True
    row = _job(conn, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "boom" in row["last_error"]


def test_claim_and_run_one_defaults_to_module_registry(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """Omitting _registry dispatches through the real module-level registry."""
    job_id = _insert_job(conn, "embed", "ver-1")
    calls: list[str] = []
    original = _REGISTRY["embed"]
    _REGISTRY["embed"] = lambda conn, tv, db, s: calls.append(tv)
    try:
        ran = claim_and_run_one(conn, db_path, settings, ("embed",))
    finally:
        _REGISTRY["embed"] = original
    assert ran is True
    assert calls == ["ver-1"]
    assert _job(conn, job_id)["status"] == "done"


def test_claim_and_run_one_with_target_version_ignores_backlog_job(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """``target_version`` makes claim_and_run_one claim only the caller's own job (lode-a3x).

    Regression test at the worker-primitive level for the bug that got
    lode-npx.2 bounced: the CLI's interactive immediate-enrich must claim the
    specific job it just enqueued, not an arbitrary older pending job of the
    same type.
    """
    backlog_id = _insert_job(conn, "enrich", "ver-backlog")
    target_id = _insert_job(conn, "enrich", "ver-target")
    enrich_registry: dict[str, HandlerFn] = {"enrich": lambda conn, tv, db, s: None}

    ran = claim_and_run_one(
        conn,
        db_path,
        settings,
        ("enrich",),
        _registry=enrich_registry,
        target_version="ver-target",
    )

    assert ran is True
    assert _job(conn, target_id)["status"] == "done"
    assert _job(conn, backlog_id)["status"] == "pending"


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
# _reclaim_stale_running — crash reclaim (lode-aor)
# ---------------------------------------------------------------------------


def test_reclaim_resets_stale_running_to_failed_with_backoff(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A 'running' job past the staleness timeout is reclaimed like a transient failure."""
    job_id = _insert_job(
        conn,
        status="running",
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )
    count = _reclaim_stale_running(conn, settings)
    assert count == 1
    row = _job(conn, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "reclaimed" in row["last_error"]
    assert row["next_attempt_at"] > _now_iso()


def test_reclaim_leaves_recently_claimed_running_alone(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A 'running' job claimed well within the timeout must not be touched."""
    job_id = _insert_job(conn, status="running", claimed_at=_now_iso())
    count = _reclaim_stale_running(conn, settings)
    assert count == 0
    row = _job(conn, job_id)
    assert row["status"] == "running"
    assert row["attempts"] == 0


def test_reclaim_dead_letters_at_max_attempts(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A stale 'running' job at max attempts is dead-lettered, not retried."""
    job_id = _insert_job(
        conn,
        status="running",
        attempts=settings.retry_max_attempts - 1,
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )
    count = _reclaim_stale_running(conn, settings)
    assert count == 1
    row = _job(conn, job_id)
    assert row["status"] == "dead"
    assert row["attempts"] == settings.retry_max_attempts


def test_reclaim_excludes_batch_backed_enrich_jobs(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A stale 'running' enrich job with a batch_handle is left alone (lode-i05.5 owns it)."""
    job_id = _insert_job(
        conn,
        job_type="enrich",
        status="running",
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
        batch_handle="batch-abc",
    )
    count = _reclaim_stale_running(conn, settings)
    assert count == 0
    assert _job(conn, job_id)["status"] == "running"


def test_reclaim_treats_null_claimed_at_as_stale(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A 'running' row with no claimed_at (pre-migration crash) is reclaimed, not left forever."""
    job_id = _insert_job(conn, status="running", claimed_at=None)
    count = _reclaim_stale_running(conn, settings)
    assert count == 1
    assert _job(conn, job_id)["status"] == "failed"


def test_reclaim_applies_to_every_job_type(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """The reclaim step is not embed-specific -- enrich and refresh are covered too."""
    stale = _past_iso(settings.stale_running_timeout_s + 60)
    embed_id = _insert_job(conn, job_type="embed", status="running", claimed_at=stale)
    enrich_id = _insert_job(
        conn,
        job_type="enrich",
        target_version="ver-2",
        status="running",
        claimed_at=stale,
    )
    refresh_id = _insert_job(
        conn,
        job_type="refresh",
        target_version="ver-3",
        status="running",
        claimed_at=stale,
    )
    count = _reclaim_stale_running(conn, settings)
    assert count == 3
    for job_id in (embed_id, enrich_id, refresh_id):
        assert _job(conn, job_id)["status"] == "failed"


def test_reclaim_does_not_touch_pending_done_or_dead(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """Only 'running' rows are candidates -- other terminal/live statuses are untouched."""
    stale = _past_iso(settings.stale_running_timeout_s + 60)
    pending_id = _insert_job(conn, status="pending", claimed_at=stale)
    done_id = _insert_job(conn, target_version="ver-2", status="done", claimed_at=stale)
    dead_id = _insert_job(conn, target_version="ver-3", status="dead", claimed_at=stale)
    count = _reclaim_stale_running(conn, settings)
    assert count == 0
    assert _job(conn, pending_id)["status"] == "pending"
    assert _job(conn, done_id)["status"] == "done"
    assert _job(conn, dead_id)["status"] == "dead"


def test_drain_reclaims_stale_running_job(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """drain() calls _reclaim_stale_running at the top of every pass.

    The reclaimed job goes to 'failed' with a future backoff, so it is not
    re-claimed within the same pass -- n reflects only jobs the main loop
    actually claimed and ran.
    """
    stale_id = _insert_job(
        conn,
        status="running",
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )
    n = drain(conn, db_path, settings, _registry=_noop_registry())
    assert n == 0  # nothing else was pending to claim/run this pass
    row = _job(conn, stale_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1


def test_reclaim_then_reconcile_sees_dead_lettered_job_as_a_gap(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """End-to-end regression for lode-aor's exact bug report.

    A worker crashes between claim and completion, leaving an embed job
    'running' forever. Before this ticket: neither _claim_one (selects only
    'pending') nor embed_gap's reconcile query (excludes anything != 'dead')
    would ever notice -- the row was permanently invisible, requiring manual
    DB surgery. Now: drain()'s _reclaim_stale_running step dead-letters the
    stuck row (attempts already exhausted here), which makes it a genuine gap
    -- so the next reconcile() pass re-enqueues fresh work for the head
    version with no manual intervention.
    """
    from lode.reconcile import _embed_gap_step
    from lode.reconcile import reconcile as _reconcile

    _insert_note_worker(conn, note_id="note-1", version_id="ver-1")
    stuck_id = _insert_job(
        conn,
        job_type="embed",
        target_version="ver-1",
        status="running",
        attempts=settings.retry_max_attempts - 1,
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )

    # Before the fix's mechanism runs: the stuck row is invisible to the gap query.
    gap_before = _reconcile(conn, steps=[("embed_gap", _embed_gap_step)])
    assert gap_before == 0

    # drain() reclaims the stale row -> dead-lettered (attempts exhausted).
    drain(conn, db_path, settings, _registry=_noop_registry())
    assert _job(conn, stuck_id)["status"] == "dead"

    # Now the gap is visible and self-heals with no manual DB surgery.
    gap_after = _reconcile(conn, steps=[("embed_gap", _embed_gap_step)])
    assert gap_after == 1
    (fresh_status,) = conn.execute(
        "SELECT status FROM jobs WHERE type = 'embed' AND id != ?", (stuck_id,)
    ).fetchone()
    assert fresh_status == "pending"


def test_drain_reclaimed_job_is_retried_on_a_later_pass(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A reclaimed job's backoff eventually expires and it runs to completion.

    Once its backoff has elapsed, the next drain() pass's _reset_retryable
    flips it back to 'pending' and the main loop claims and runs it -- no
    manual DB surgery needed to unstick it.
    """
    job_id = _insert_job(
        conn,
        status="running",
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )
    drain(conn, db_path, settings, _registry=_noop_registry())
    assert _job(conn, job_id)["status"] == "failed"

    # Force the backoff window to have elapsed (avoid a real-time sleep).
    conn.execute(
        "UPDATE jobs SET next_attempt_at = ? WHERE id = ?",
        (_past_iso(1), job_id),
    )
    conn.commit()

    n = drain(conn, db_path, settings, _registry=_noop_registry())
    assert n == 1
    assert _job(conn, job_id)["status"] == "done"


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


def test_drain_appends_main_loop_outcomes(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """drain() forwards `outcomes` to the main claim/run loop (lode-1gr.4).

    Each embed job processed by the main loop appends its handler's outcome
    line -- this is the channel 'lode work' uses to echo a per-note passage
    count, e.g. "embedded <short-id>: 3 passages".
    """
    _insert_job(conn, target_version="ver-a")
    _insert_job(conn, target_version="ver-b")
    registry: dict[str, HandlerFn] = {
        "embed": lambda conn, tv, db, s: f"embedded {tv}: 3 passages"
    }
    outcomes: list[str] = []
    n = drain(conn, db_path, settings, _registry=registry, outcomes=outcomes)
    assert n == 2
    assert sorted(outcomes) == [
        "embedded ver-a: 3 passages",
        "embedded ver-b: 3 passages",
    ]


def test_drain_no_op_pass_leaves_outcomes_empty(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A no-op drain pass (nothing pending) appends no outcome lines."""
    outcomes: list[str] = []
    n = drain(conn, db_path, settings, _registry=_noop_registry(), outcomes=outcomes)
    assert n == 0
    assert outcomes == []


def test_drain_collects_enrich_batch_outcome_via_batch_pre_step(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A later drain pass that collects a completed enrich batch appends an
    enrich outcome line via the batch pre-step (lode-1gr.4) -- not the main
    loop's return count, which never includes batch-collected enrich jobs.
    """
    from lode.enrich import EnrichmentResult, format_enrich_outcome

    _insert_note_worker(conn, note_id="note-1", version_id="ver-1")
    job_id = _insert_enrich_job_worker(
        conn, version_id="ver-1", status="running", batch_handle="collect-batch"
    )

    enrichment = EnrichmentResult(tags=["python", "api"], entities=["FastAPI"])
    result_obj = mock.MagicMock()
    result_obj.custom_id = "ver-1"
    result_obj.result.type = "succeeded"
    tool_block = mock.MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = enrichment.model_dump()
    result_obj.result.message.content = [tool_block]

    client = _fake_batch_client_worker(
        batch_id="collect-batch", results=[result_obj], processing_status="ended"
    )

    outcomes: list[str] = []
    n = drain(
        conn,
        db_path,
        settings,
        _registry=_noop_registry(),
        _batch_client=client,
        outcomes=outcomes,
    )
    # Batch-collected enrich outcomes are not counted in the main loop's
    # return value (docstring-documented) -- only the embed leg is, and
    # there's no embed job here.
    assert n == 0
    assert outcomes == [format_enrich_outcome("ver-1", enrichment)]
    assert _job(conn, job_id)["status"] == "done"


def test_drain_main_loop_skips_enrich_batch_in_flight(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """The main claim-run loop does not process enrich jobs claimed by the batch step.

    After lode-npx.2 the batch pre-step in drain() handles pending enrich jobs
    (submits them to the Batches API and marks them 'running').  The main claim-run
    loop only processes jobs in 'pending' status, so it never touches an enrich job
    that the batch step has already claimed.  n == 1 because only the embed job is
    claimed by the main loop.
    """
    # Insert a real note/version so submit_enrich_batch can load the body and
    # actually submit the job (rather than marking it done as "not found").
    _insert_note_worker(conn)
    # Enqueue both embed and enrich jobs for that version.
    enqueue_derive_jobs(conn, "ver-1")

    # Provide a no-op batch client: create() 'succeeds' (returns a batch handle),
    # retrieve() shows the batch still in_progress (so collect does nothing).
    batch_obj = mock.MagicMock()
    batch_obj.id = "batch-noop"
    status_obj = mock.MagicMock()
    status_obj.processing_status = "in_progress"
    fake_batch = mock.MagicMock()
    fake_batch.beta.messages.batches.create.return_value = batch_obj
    fake_batch.beta.messages.batches.retrieve.return_value = status_obj

    n = drain(
        conn, db_path, settings, _registry=_noop_registry(), _batch_client=fake_batch
    )
    assert n == 1  # only the embed job processed by the main loop

    # Enrich is 'running' — claimed by the batch step, not the main loop.
    (status,) = conn.execute("SELECT status FROM jobs WHERE type = 'enrich'").fetchone()
    assert status == "running"


def test_drain_enrich_never_dead_lettered(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """An enrich job is never dead-lettered even across many failing drain passes.

    The batch pre-step reverts a failed batch submission to 'failed' (with backoff)
    without incrementing ``attempts``, so no drain pass can push an enrich job to
    ``status='dead'`` via the batch-submit failure path.  Dead-letter only happens
    inside collect_enrich_batch when the Batches API itself returns an error result
    after retry_max_attempts.
    """
    enqueue_derive_jobs(conn, "ver-1")
    # Run drain many times with no batch client — the batch step fails gracefully
    # (no API key), reverts to 'failed', never reaches 'dead'.
    for _ in range(5):
        drain(conn, db_path, settings, _registry=_noop_registry())

    (status,) = conn.execute("SELECT status FROM jobs WHERE type = 'enrich'").fetchone()
    # Status may be 'pending' (if reset by _reset_retryable) or 'failed' (backoff
    # still active) — but must never be 'dead'.
    assert status != "dead"


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


@pytest.mark.parametrize("job_type", ["embed", "enrich", "refresh"])
def test_job_type_is_registered_by_default(job_type: str) -> None:
    """The module-level registry must have each core job type registered at
    import time (embed: original registry; enrich: lode-npx.1; refresh:
    lode-w0h.3)."""
    from lode.worker import registered_types

    assert job_type in registered_types()


# ---------------------------------------------------------------------------
# _embed_handler + the w0h.5 post-embed re-enrich gate (integration)
#
# Exercises the real embed() -> gate_reenrich() wiring _embed_handler adds
# (unit coverage of the gate's own decision logic lives in
# tests/test_externals.py). FastEmbedEmbedder is monkeypatched so no real
# ONNX model loads; chunking, embedding, the vector store, and the gate all
# run for real.
# ---------------------------------------------------------------------------


def _stub_embedder_returning(monkeypatch, vector: list[float]) -> None:
    from lode.embedding import FastEmbedEmbedder

    monkeypatch.setattr(
        FastEmbedEmbedder,
        "embed_passages",
        lambda self, texts: [vector for _ in texts],
    )


def test_embed_handler_gates_material_first_snapshot(
    conn: sqlite3.Connection, db_path: Path, monkeypatch
) -> None:
    from lode.externals import ingest_snapshot
    from lode.worker import _embed_handler

    _stub_embedder_returning(monkeypatch, [1.0, 0.0, 0.0, 0.0])
    settings = Settings(embedding_vector_dim=4)
    result = ingest_snapshot(
        conn, "https://example.com/x", "web", "hello world", settings=settings
    )

    outcome = _embed_handler(conn, result.snapshot_id, db_path, settings)

    assert outcome is not None
    assert "embedded" in outcome
    assert "material" in outcome
    rows = conn.execute(
        "SELECT type, status FROM jobs WHERE target_version = ? ORDER BY type",
        (result.snapshot_id,),
    ).fetchall()
    assert ("enrich", "pending") in rows


def test_embed_handler_gates_immaterial_carries_forward(
    conn: sqlite3.Connection, db_path: Path, monkeypatch
) -> None:
    from lode.externals import ingest_snapshot
    from lode.worker import _embed_handler

    settings = Settings(embedding_vector_dim=4)
    external_id = "https://example.com/x"
    first = ingest_snapshot(conn, external_id, "web", "version one", settings=settings)
    _stub_embedder_returning(monkeypatch, [1.0, 0.0, 0.0, 0.0])
    _embed_handler(conn, first.snapshot_id, db_path, settings)  # embeds the predecessor

    second = ingest_snapshot(conn, external_id, "web", "version two", settings=settings)
    outcome = _embed_handler(
        conn, second.snapshot_id, db_path, settings
    )  # same vector -> immaterial

    assert outcome is not None
    assert "immaterial" in outcome
    rows = conn.execute(
        "SELECT type, status FROM jobs WHERE target_version = ? ORDER BY type",
        (second.snapshot_id,),
    ).fetchall()
    # _embed_handler doesn't flip job status itself (run_one does) -- its own
    # 'embed' job stays pending here; the point is no 'enrich' job appears.
    assert rows == [("embed", "pending")]


def test_embed_handler_gate_is_a_no_op_for_a_note_version(
    conn: sqlite3.Connection, db_path: Path, monkeypatch
) -> None:
    """A note version's embed is untouched by the gate (no snapshots row for it)."""
    from lode.versions import save
    from lode.worker import _embed_handler

    _stub_embedder_returning(monkeypatch, [1.0, 0.0, 0.0, 0.0])
    settings = Settings(embedding_vector_dim=4)
    version = save(conn, "note-1", "hello world", settings=settings).version_id

    outcome = _embed_handler(conn, version, db_path, settings)

    assert outcome is not None
    assert "embedded" in outcome
    assert "material" not in outcome and "immaterial" not in outcome


# ---------------------------------------------------------------------------
# Batch pre-steps — _batch_submit_enrich / _batch_collect_enrich (lode-npx.2)
# ---------------------------------------------------------------------------


def _fake_batch_client_worker(
    batch_id: str = "wbatch-abc",
    results: list | None = None,
    processing_status: str = "ended",
) -> mock.MagicMock:
    """Mock Anthropic client for worker batch tests."""
    client = mock.MagicMock()
    batch = mock.MagicMock()
    batch.id = batch_id
    client.beta.messages.batches.create.return_value = batch
    status_obj = mock.MagicMock()
    status_obj.processing_status = processing_status
    client.beta.messages.batches.retrieve.return_value = status_obj
    client.beta.messages.batches.results.return_value = iter(results or [])
    return client


def _insert_enrich_job_worker(
    conn: sqlite3.Connection,
    version_id: str = "ver-1",
    status: str = "pending",
    batch_handle: str | None = None,
) -> int:
    """Insert an enrich job; return job id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (type, target_version, status, batch_handle) "
            "VALUES ('enrich', ?, ?, ?)",
            (version_id, status, batch_handle),
        )
    return cur.lastrowid


def _insert_note_worker(
    conn: sqlite3.Connection,
    note_id: str = "note-1",
    version_id: str = "ver-1",
    body: str = "test body",
) -> None:
    """Insert a note+version pair for worker batch tests."""
    with conn:
        conn.execute("INSERT INTO notes (note_id) VALUES (?)", (note_id,))
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) VALUES (?, ?, ?, 'create')",
            (version_id, note_id, body),
        )
        conn.execute(
            "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
            (version_id, note_id),
        )


def test_batch_submit_claims_pending_enrich_jobs(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """_batch_submit_enrich claims pending enrich jobs and marks them running."""
    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(conn)

    client = _fake_batch_client_worker(batch_id="test-batch")
    submitted = _batch_submit_enrich(conn, settings, _client=client)

    assert submitted == 1
    row = _job(conn, job_id)
    assert row["status"] == "running"


def test_batch_submit_stamps_claimed_at(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """_batch_submit_enrich stamps claimed_at, exactly as _claim_one does (lode-uhu)."""
    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(conn)

    before = _now_iso()
    client = _fake_batch_client_worker(batch_id="test-batch")
    submitted = _batch_submit_enrich(conn, settings, _client=client)
    after = _now_iso()

    assert submitted == 1
    row = _job(conn, job_id)
    assert row["claimed_at"] is not None
    assert before <= row["claimed_at"] <= after


def test_batch_submit_survives_crash_before_batch_handle_persist(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """Regression (lode-uhu): a crash between batches.create() returning and
    submit_enrich_batch's batch_handle persist (enrich.py) used to leave a row
    (running, batch_handle NULL, claimed_at NULL), which _reclaim_stale_running
    treats as immediately stale and resubmits -- risking a duplicate Batches
    API submission.

    Simulate the crash with a BaseException that escapes _batch_submit_enrich's
    `except Exception` revert handler entirely (mirroring a process kill, not
    a caught API error) after the pre-claim CAS has already stamped
    claimed_at but before any batch_handle would be persisted.
    """
    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(conn)

    with mock.patch("lode.enrich.submit_enrich_batch", side_effect=SystemExit):
        with pytest.raises(SystemExit):
            _batch_submit_enrich(conn, settings, _client=_fake_batch_client_worker())

    # The pre-claim CAS ran and stamped claimed_at before the (simulated)
    # crash; batch_handle never got persisted.
    row = _job(conn, job_id)
    assert row["status"] == "running"
    assert row["claimed_at"] is not None
    batch_handle = conn.execute(
        "SELECT batch_handle FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()[0]
    assert batch_handle is None

    # claimed_at is fresh -- _reclaim_stale_running must NOT treat this row
    # as immediately stale (which would risk a duplicate submission).
    count = _reclaim_stale_running(conn, settings)
    assert count == 0
    assert _job(conn, job_id)["status"] == "running"


def test_batch_submit_no_op_when_no_pending_enrich(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """_batch_submit_enrich returns 0 when there are no pending enrich jobs."""
    client = _fake_batch_client_worker()
    submitted = _batch_submit_enrich(conn, settings, _client=client)
    assert submitted == 0
    client.beta.messages.batches.create.assert_not_called()


def test_batch_submit_reverts_on_api_failure(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """_batch_submit_enrich reverts jobs to 'failed' if the API call raises."""
    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(conn)

    # Client that raises on create.
    client = mock.MagicMock()
    client.beta.messages.batches.create.side_effect = RuntimeError("api down")

    submitted = _batch_submit_enrich(conn, settings, _client=client)
    assert submitted == 0

    # Job reverted to 'failed' (not 'dead') — will be retried.
    row = _job(conn, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 0  # attempts NOT incremented by batch-submit failure


class _FrozenCursor:
    """Cursor stand-in whose ``fetchall`` yields a pre-captured row list."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return self._rows


class _RacingSelectConn:
    """Wrap a real connection to simulate a concurrent immediate-enrich claim.

    Right after ``_batch_submit_enrich``'s candidate SELECT returns the pending
    enrich jobs, an external claimer (an interactive ``lode add`` running
    ``claim_and_run_one`` without the worker lock) flips one of them
    ``pending`` -> ``running``. This is the TOCTOU the per-row CAS in
    ``_batch_submit_enrich`` guards against (lode-npx.2): the raced job must be
    dropped from the batch, never double-submitted.
    """

    def __init__(self, real: sqlite3.Connection, race_job_id: int) -> None:
        self._real = real
        self._race_job_id = race_job_id
        self._raced = False

    def execute(self, sql: str, *args):
        if (
            not self._raced
            and "FROM jobs" in sql
            and "status = 'pending'" in sql
            and "ORDER BY created" in sql
        ):
            rows = self._real.execute(sql, *args).fetchall()
            self._raced = True
            with self._real:
                self._real.execute(
                    "UPDATE jobs SET status = 'running' WHERE id = ?",
                    (self._race_job_id,),
                )
            return _FrozenCursor(rows)
        return self._real.execute(sql, *args)

    def executemany(self, sql: str, seq):
        return self._real.executemany(sql, seq)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_batch_submit_skips_job_claimed_by_concurrent_immediate_enrich(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A job an interactive immediate-enrich claims mid-flight is not double-submitted.

    Regression for the immediate-vs-batch double-spend TOCTOU (lode-npx.2): the
    batch step's SELECT sees two pending enrich jobs, then a concurrent claimer
    flips one to 'running' before the per-row CAS runs. Only the job the batch
    step actually wins (rowcount == 1) is submitted; the raced job is left for
    its concurrent claimer, so it never reaches the (paid) Batches API.
    """
    _insert_note_worker(conn, note_id="note-a", version_id="ver-a")
    _insert_note_worker(conn, note_id="note-b", version_id="ver-b")
    raced_job = _insert_enrich_job_worker(conn, version_id="ver-a")
    won_job = _insert_enrich_job_worker(conn, version_id="ver-b")

    client = _fake_batch_client_worker(batch_id="race-batch")
    racing = _RacingSelectConn(conn, race_job_id=raced_job)
    submitted = _batch_submit_enrich(racing, settings, _client=client)

    # Only the job the batch step won (ver-b) is submitted.
    assert submitted == 1
    requests = client.beta.messages.batches.create.call_args.kwargs["requests"]
    assert [r["custom_id"] for r in requests] == ["ver-b"]

    # The raced job stays running with NO batch handle — claimed by the
    # concurrent immediate-enrich, never submitted to the paid Batches API.
    raced = conn.execute(
        "SELECT status, batch_handle FROM jobs WHERE id = ?", (raced_job,)
    ).fetchone()
    assert raced == ("running", None)

    # The won job carries the batch handle.
    won = conn.execute(
        "SELECT status, batch_handle FROM jobs WHERE id = ?", (won_job,)
    ).fetchone()
    assert won == ("running", "race-batch")


def test_batch_collect_returns_false_when_in_progress(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """_batch_collect_enrich returns 0 for in-progress batches."""
    _insert_note_worker(conn)
    _insert_enrich_job_worker(conn, status="running", batch_handle="in-flight-batch")

    client = _fake_batch_client_worker(
        batch_id="in-flight-batch", processing_status="in_progress"
    )
    ended = _batch_collect_enrich(conn, settings, _client=client)
    assert ended == 0  # batch not ended, nothing processed


def test_batch_collect_returns_count_of_ended_batches(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """_batch_collect_enrich returns count of batches that ended this pass."""
    from lode.enrich import EnrichmentResult

    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(
        conn, status="running", batch_handle="done-batch"
    )

    # Build a succeeded result.
    enrichment = EnrichmentResult(tags=["test"])
    tool_block = mock.MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = enrichment.model_dump()
    result_obj = mock.MagicMock()
    result_obj.custom_id = "ver-1"
    result_obj.result.type = "succeeded"
    result_obj.result.message.content = [tool_block]

    client = _fake_batch_client_worker(
        batch_id="done-batch", results=[result_obj], processing_status="ended"
    )
    ended = _batch_collect_enrich(conn, settings, _client=client)
    assert ended == 1  # one batch ended

    # Job marked done.
    row = _job(conn, job_id)
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# Durable batch-handle persistence + resume-on-restart (lode-i05.5)
# ---------------------------------------------------------------------------


def test_batch_collect_resumes_after_restart_without_resubmit(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A batch_handle persisted by a prior process is re-polled and ingested on
    restart, with no resubmission (lode-i05.5).

    The job row (status='running', batch_handle set) is the only trace of the
    submitted batch -- nothing lives in memory across a restart. A fresh
    ``_batch_collect_enrich`` call, exactly as ``drain()`` runs it at worker
    startup, must find it via the DB, poll, ingest the result, and mark it
    done -- and must never call ``batches.create`` (that would double-spend).
    """
    from lode.enrich import EnrichmentResult

    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(
        conn, status="running", batch_handle="restart-batch"
    )

    enrichment = EnrichmentResult(tags=["resumed"])
    tool_block = mock.MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = enrichment.model_dump()
    result_obj = mock.MagicMock()
    result_obj.custom_id = "ver-1"
    result_obj.result.type = "succeeded"
    result_obj.result.message.content = [tool_block]

    client = _fake_batch_client_worker(
        batch_id="restart-batch", results=[result_obj], processing_status="ended"
    )

    ended = _batch_collect_enrich(conn, settings, _client=client)

    assert ended == 1
    client.beta.messages.batches.create.assert_not_called()
    assert _job(conn, job_id)["status"] == "done"


def test_batch_collect_in_flight_handle_survives_restart_no_resubmit(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """An in-flight batch handle is polled but never resubmitted, across
    repeated 'restart' passes, until the Batch actually ends (lode-i05.5).
    """
    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(
        conn, status="running", batch_handle="slow-batch"
    )

    client = _fake_batch_client_worker(
        batch_id="slow-batch", processing_status="in_progress"
    )

    # Simulate several worker-startup passes while the batch is still running.
    for _ in range(3):
        ended = _batch_collect_enrich(conn, settings, _client=client)
        assert ended == 0

    client.beta.messages.batches.create.assert_not_called()
    row = conn.execute(
        "SELECT status, batch_handle FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row == ("running", "slow-batch")


def test_worker_startup_resumes_batch_without_double_enqueue_or_resubmit(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """End-to-end restart simulation (lode-i05.5).

    Mirrors the exact sequence ``lode work`` runs at startup (``cli.py``):
    ``reconcile()`` first, then ``drain()``. A persisted batch handle from a
    prior (crashed/restarted) process must be resumed -- ingested and marked
    done -- with no resubmission, AND the enrich-gap reconcile step must not
    treat the in-flight ('running' + handle) job as a gap and re-enqueue a
    duplicate. Isolates the enrich_gap step so the unrelated embed_gap step
    (this fixture has no embed job) doesn't add noise to the assertion.
    """
    from lode.enrich import EnrichmentResult
    from lode.reconcile import _enrich_gap_step
    from lode.reconcile import reconcile as _reconcile

    _insert_note_worker(conn)
    _insert_enrich_job_worker(conn, status="running", batch_handle="resume-batch")

    enrichment = EnrichmentResult(tags=["resumed"])
    tool_block = mock.MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = enrichment.model_dump()
    result_obj = mock.MagicMock()
    result_obj.custom_id = "ver-1"
    result_obj.result.type = "succeeded"
    result_obj.result.message.content = [tool_block]

    client = _fake_batch_client_worker(
        batch_id="resume-batch", results=[result_obj], processing_status="ended"
    )

    gap = _reconcile(conn, steps=[("enrich_gap", _enrich_gap_step)])
    drain(conn, db_path, settings, _registry=_noop_registry(), _batch_client=client)

    assert gap == 0  # in-flight batch job (running + handle) is not a gap
    client.beta.messages.batches.create.assert_not_called()  # never resubmitted
    (status,) = conn.execute("SELECT status FROM jobs WHERE type = 'enrich'").fetchone()
    assert status == "done"
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE type = 'enrich'"
    ).fetchone()
    assert count == 1  # no duplicate enqueued by the reconcile scan


def test_drain_batch_steps_run_before_main_loop(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """drain() runs batch pre-steps before the main claim-run loop.

    With a batch client that 'submits' successfully: pending enrich jobs are
    moved to 'running' by the batch step; the main loop only processes embed.
    """
    _insert_note_worker(conn)
    enqueue_derive_jobs(conn, "ver-1")  # embed + enrich pending

    client = _fake_batch_client_worker(
        batch_id="pre-batch", processing_status="in_progress"
    )
    n = drain(conn, db_path, settings, _registry=_noop_registry(), _batch_client=client)

    assert n == 1  # only embed processed by main loop
    (enrich_status,) = conn.execute(
        "SELECT status FROM jobs WHERE type = 'enrich'"
    ).fetchone()
    # Enrich was claimed by the batch step — 'running', not 'pending' or 'dead'.
    assert enrich_status == "running"
