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

import logging
import sqlite3
import sys
import threading
import time
import unittest.mock as mock
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import lode.jobs as jobs
from lode.auth import AuthError
from lode.config import Settings
from lode.enrich import ENRICH_PROMPT_VER
from lode.jobs import enqueue_derive_jobs
from lode.jobs import now_iso as _now_iso
from lode.storage import init_db
from lode.worker import (
    _REGISTRY,
    HandlerFn,
    _batch_collect_enrich,
    _batch_submit_enrich,
    _claim_one,
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


def _parse_iso(ts: str) -> datetime:
    """Parse a schema-format ISO-8601 timestamp (the inverse of ``worker._iso``)."""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


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
    """After a transient failure, next_attempt_at must reflect the applied backoff.

    Baseline is captured BEFORE run_one() and the assertion is a delta against
    it, with zero clock reads after the call -- asserting "is it still in the
    future" two statements later races the 1.0s backoff against however long
    wall-clock time elapses under load before the assertion runs (lode-vnud,
    the same defect lode-0x1 fixes for the reclaim-backoff test).
    """
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    before = _now_iso()
    run_one(conn, job_id, db_path, settings, _failing_registry())
    next_at = _job(conn, job_id)["next_attempt_at"]
    delta = (_parse_iso(next_at) - _parse_iso(before)).total_seconds()
    assert delta >= settings.retry_backoff_base_s, (
        "next_attempt_at should reflect the applied backoff"
    )


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
# Permanent, user-actionable failures (lode-9yy) — AuthError is not transient
# ---------------------------------------------------------------------------


def _auth_error_registry(msg: str = "no credentials (test)") -> dict[str, HandlerFn]:
    """Registry with an embed handler that always raises AuthError.

    Stands in for the real failure shape: a handler that (transitively) calls
    :func:`lode.auth.build_client` with no credentials resolvable.
    """

    def _fail(conn, tv, db, s):
        raise AuthError(msg)

    return {"embed": _fail}


def test_run_auth_error_resets_to_pending_uncharged(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A permanent AuthError must NOT be folded into the transient accounting:
    it is re-raised (not absorbed as a job outcome), and the job is reset straight
    back to 'pending' — no attempts charged, no backoff."""
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    with pytest.raises(AuthError, match="no creds"):
        run_one(conn, job_id, db_path, settings, _auth_error_registry("no creds"))
    row = _job(conn, job_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert "no creds" in row["last_error"]


def test_run_auth_error_at_max_attempts_still_does_not_dead_letter(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """Even a job already at retry_max_attempts - 1 must not dead-letter on an
    AuthError -- the permanent-failure path is taken before the max-attempts
    gate is ever consulted."""
    job_id = _insert_job(conn, attempts=settings.retry_max_attempts - 1)
    _claim_one(conn, ("embed",), _now_iso())
    with pytest.raises(AuthError):
        run_one(conn, job_id, db_path, settings, _auth_error_registry())
    row = _job(conn, job_id)
    assert row["status"] == "pending"
    assert row["attempts"] == settings.retry_max_attempts - 1  # untouched


def test_run_auth_error_reset_does_not_resurrect_a_reclaimed_job(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """The AuthError reset is CAS'd on status='running' — it must never revive a
    job a concurrent reclaim already drove to a terminal 'dead'.

    ``cli._enrich_immediately`` reaches ``run_one`` via ``claim_and_run_one``,
    which runs WITHOUT the worker lock, so a concurrent ``lode work`` drain can
    hit ``_reclaim_stale_running`` and dead-letter this very row while the
    handler is still in flight. Unguarded, ``run_one``'s reset would then flip
    that dead job (whose dead-letter hook has already fired) back to 'pending'
    and re-arm it forever. The handler below stands in for that concurrent
    reclaim: it terminalizes the row, then raises AuthError.
    """
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())

    def _reclaimed_then_auth_error(conn_, tv, db, s):
        # Stand-in for a concurrent _reclaim_stale_running dead-lettering us.
        with conn_:
            conn_.execute(
                "UPDATE jobs SET status = 'dead', attempts = ? WHERE id = ?",
                (settings.retry_max_attempts, job_id),
            )
        raise AuthError("no credentials (test)")

    with pytest.raises(AuthError):
        run_one(conn, job_id, db_path, settings, {"embed": _reclaimed_then_auth_error})

    row = _job(conn, job_id)
    assert row["status"] == "dead"  # NOT resurrected to 'pending'
    assert row["attempts"] == settings.retry_max_attempts


def test_run_transient_error_unaffected_by_auth_error_carve_out(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A plain (non-AuthError) exception still follows the ordinary transient
    path -- the AuthError carve-out must not change behavior for anything
    else."""
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    ok = run_one(conn, job_id, db_path, settings, _failing_registry("plain oops"))
    assert ok is False
    row = _job(conn, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1


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


def test_run_refresh_dead_letter_log_names_source_and_reason(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """lode-gpzn.5: the dead-letter log line names the failing source and the
    reason, not just the bare job id -- e.g. a JIRA/Confluence connector
    hitting an unreachable/bad base URL (a TransientFetchError riding this
    same generic retry/dead-letter machinery, per _always_raising_refresh_
    registry's own docstring) must be identifiable in ``lode work``'s output
    once it exhausts retries, not just "job 5 dead-lettered ...: <error>"
    with no indication of *what* died.

    Before this fix, run_one's dead-letter log call passed only ``job_id``
    and ``err`` -- unlike the sibling 'failed' (still-retrying) log line a
    few lines above it, which already includes ``job_type``/``target``.
    """
    external_id = "ABC-999"
    job_id = _insert_job(
        conn, "refresh", external_id, attempts=settings.retry_max_attempts - 1
    )
    _claim_one(conn, ("refresh",), _now_iso())
    with caplog.at_level(logging.ERROR):
        ok = run_one(
            conn,
            job_id,
            db_path,
            settings,
            _always_raising_refresh_registry("bad base url: unreachable host"),
        )
    assert ok is False
    assert _job(conn, job_id)["status"] == "dead"

    dead_letter_lines = [
        line for line in caplog.text.splitlines() if "dead-lettered" in line
    ]
    assert len(dead_letter_lines) == 1
    (line,) = dead_letter_lines
    assert external_id in line, "dead-letter log line must name the source"
    assert "bad base url: unreachable host" in line, (
        "dead-letter log line must name the reason"
    )


def test_refresh_dead_letter_hook_is_generic_over_source_type(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lode-gpzn.13: a non-web target's dead-letter tombstone must carry its
    own source_type, not the hook's former hardcoded 'web' default.

    A JIRA/Confluence connector (gpzn.2, not yet built) persists the
    externals row -- source_type included -- synchronously at note-save
    detection time, before any 'refresh' job for that target is even
    enqueued (docs/decisions.md's Atlassian refinement A: the API base is
    PERSISTED on the external row at detection). This test reproduces that
    precondition directly (bypassing the not-yet-built connector) and
    asserts the shared _refresh_dead_letter_hook resolves the FAILING
    target's own source_type ('jira') and passes THAT into ingest_snapshot
    -- not the former hardcoded SOURCE_TYPE_WEB.

    Why we spy on the ingest_snapshot argument rather than only reading the
    externals row back: ingest_snapshot's externals upsert is
    ``INSERT ... ON CONFLICT (external_id) DO NOTHING``, so a pre-existing
    row's source_type is left untouched *regardless of the value passed in*.
    A row-readback assertion (``source_type == 'jira'``) therefore passes
    identically against the pre-fix hook that hardcoded 'web' -- it is
    vacuous. Capturing the value the hook actually computes and hands to
    ingest_snapshot is the only non-vacuous observable: it fails the moment
    the hook reverts to passing 'web'.
    """
    external_id = "JIRA-1234"
    conn.execute(
        "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
        (external_id, "jira"),
    )
    conn.commit()

    import lode.externals as externals_mod

    real_ingest_snapshot = externals_mod.ingest_snapshot
    captured: dict[str, str] = {}

    def _spy_ingest_snapshot(conn, external_id, source_type, body, **kwargs):
        captured["source_type"] = source_type
        return real_ingest_snapshot(conn, external_id, source_type, body, **kwargs)

    # The hook imports ingest_snapshot lazily (function-local `from
    # lode.externals import ingest_snapshot`), so the name is rebound from
    # the module at call time -- patching the module attribute is seen.
    monkeypatch.setattr(externals_mod, "ingest_snapshot", _spy_ingest_snapshot)

    job_id = _insert_job(
        conn, "refresh", external_id, attempts=settings.retry_max_attempts - 1
    )
    _claim_one(conn, ("refresh",), _now_iso())
    ok = run_one(
        conn, job_id, db_path, settings, _always_raising_refresh_registry("timeout")
    )
    assert ok is False
    assert _job(conn, job_id)["status"] == "dead"

    # Load-bearing: the hook resolved and passed the target's own source_type.
    assert captured.get("source_type") == "jira", (
        "hook must pass the failing target's own source_type into "
        "ingest_snapshot, not the hardcoded 'web' default"
    )

    (source_type,) = conn.execute(
        "SELECT source_type FROM externals WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert source_type == "jira", "dead-letter tombstone must not overwrite source_type"

    (status,) = conn.execute(
        "SELECT status FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert status == "tombstone"


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


def test_reclaim_dead_letter_logs_per_job_source(
    conn: sqlite3.Connection,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """lode-ympb: a crash-reclaimed job dead-lettered by _reclaim_stale_running
    must log a per-job line naming job_type/target_version, not just the
    aggregate 'reclaimed %d stale running job(s)' count -- mirroring
    run_one's own dead-letter/failed log lines (job_type + short(target)) so
    a connector job (JIRA/Confluence/web refresh) that crash-reclaims
    straight to dead is identifiable by source.
    """
    external_id = "https://example.com/crashed-source-visibility"
    job_id = _insert_job(
        conn,
        job_type="refresh",
        target_version=external_id,
        status="running",
        attempts=settings.retry_max_attempts - 1,
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )
    with caplog.at_level(logging.ERROR):
        count = _reclaim_stale_running(conn, settings)
    assert count == 1
    assert _job(conn, job_id)["status"] == "dead"

    assert f"job {job_id} (refresh target=" in caplog.text
    assert "dead-lettered by crash-reclaim" in caplog.text


def test_reclaim_dead_letter_hook_does_not_beat_a_real_snapshot(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """lode-uda1: a late-succeeding handler's real snapshot must beat the
    reclaim dead-letter hook's tombstone, not the other way around.

    Forces the exact bad interleaving the ticket describes: the still-in-
    flight handler's real 'ok' snapshot commits FIRST (standing in for the
    fetch actually succeeding), and only THEN does
    ``_reclaim_stale_running`` dead-letter the row (still 'running',
    ``claimed_at`` older than the staleness timeout) and fire the hook.
    Before the lode-uda1 guard this hook unconditionally tombstoned,
    clobbering the successful fetch and leaving the external permanently
    absorbing (reconcile's refresh/embed sweeps both exclude a tombstoned
    head, ``reconcile.py`` "AND s.status != 'tombstone'"). The guard must
    recognize the head is already 'ok' and fetched at-or-after this job's
    claim, and skip the tombstone write.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.externals import ingest_snapshot

    external_id = "https://example.com/raced-success"
    job_id = _insert_job(
        conn,
        job_type="refresh",
        target_version=external_id,
        status="running",
        attempts=settings.retry_max_attempts - 1,
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )

    # The bad interleaving: the handler's real snapshot lands BEFORE the
    # reclaim runs (fetched_at is "now" -- strictly after the stale claimed_at
    # above), simulating the fetch having actually succeeded while the row
    # was already reclaim-eligible.
    ingest_snapshot(
        conn, external_id, SOURCE_TYPE_WEB, "the real, successfully-fetched body"
    )

    count = _reclaim_stale_running(conn, settings)
    assert count == 1
    assert _job(conn, job_id)["status"] == "dead"

    # Head must still be the real 'ok' snapshot -- NOT overwritten by a
    # tombstone from the reclaim's dead-letter hook.
    status, body = conn.execute(
        "SELECT s.status, s.body FROM snapshots s "
        "JOIN externals e ON e.head_snapshot_id = s.snapshot_id "
        "WHERE e.external_id = ?",
        (external_id,),
    ).fetchone()
    assert status == "ok"
    assert body == "the real, successfully-fetched body"
    # Only the one real snapshot exists -- no tombstone row was inserted.
    (snapshot_count,) = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert snapshot_count == 1


def test_reclaim_dead_letter_hook_recognizes_a_deduped_success(
    conn: sqlite3.Connection, db_path: Path, settings: Settings, monkeypatch
) -> None:
    """lode-9tj4: a successful-but-content-IDENTICAL refetch must also beat
    the reclaim dead-letter hook's tombstone -- not just a content-CHANGED
    one (the case the test above already covers).

    Before this fix, ``ingest_snapshot``'s dedup early return
    (``snapshot_id == head_snapshot_id``) wrote nothing at all -- crucially,
    it never bumped the existing head row's ``fetched_at`` -- so a refresh
    that successfully re-verified UNCHANGED content left the guard nothing
    recent to see: ``head_fetched_at`` stayed pinned at the original fetch
    time, always older than a job claimed afterward. Since "content
    unchanged" is the common refresh outcome, this made the guard blind in
    the likely case, not merely a rare one. Reproduces the ticket's exact
    steps: an external with an existing 'ok' head; a refresh job claimed
    against it; the handler's fetch succeeds but dedups (identical body);
    the job then stalls and is reclaimed past the staleness timeout.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.externals import ingest_snapshot

    external_id = "https://example.com/unchanged-on-reclaim"
    # The external's ORIGINAL fetch happened long ago -- pin it far in the
    # past (year 2000) so it unambiguously PREDATES the job's claim below.
    # Without pinning this, the original ingest's real-clock fetched_at
    # would already land after claimed_at (which is only
    # stale_running_timeout_s + 60 seconds in the past) regardless of
    # whether the dedup bump works at all -- masking exactly the blind spot
    # this test exists to catch. Production shape: a refresh job only
    # exists for an already-drawn-down external, so the head predates it.
    with monkeypatch.context() as m:
        m.setattr("lode.externals.jobs.now_iso", lambda: "2000-01-01T00:00:00.000000Z")
        ingest_snapshot(conn, external_id, SOURCE_TYPE_WEB, "stable, unchanging body")

    job_id = _insert_job(
        conn,
        job_type="refresh",
        target_version=external_id,
        status="running",
        attempts=settings.retry_max_attempts - 1,
        claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
    )

    # The handler's fetch SUCCEEDS but returns content IDENTICAL to the
    # current head -- ingest_snapshot's dedup path, not a new snapshot.
    # Runs on the REAL clock (restored by the monkeypatch context above),
    # which is after the job's claimed_at set just above -- this is the
    # moment that must bump fetched_at forward for the guard to have
    # anything recent to see.
    result = ingest_snapshot(
        conn, external_id, SOURCE_TYPE_WEB, "stable, unchanging body"
    )
    assert result is not None
    assert result.deduped is True

    count = _reclaim_stale_running(conn, settings)
    assert count == 1
    assert _job(conn, job_id)["status"] == "dead"

    # Head must still be the real 'ok' snapshot -- NOT overwritten by a
    # tombstone from the reclaim's dead-letter hook.
    status, body = conn.execute(
        "SELECT s.status, s.body FROM snapshots s "
        "JOIN externals e ON e.head_snapshot_id = s.snapshot_id "
        "WHERE e.external_id = ?",
        (external_id,),
    ).fetchone()
    assert status == "ok"
    assert body == "stable, unchanging body"
    # Only the one real snapshot row exists -- the dedup never inserted a
    # second one, and no tombstone was written either.
    (snapshot_count,) = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert snapshot_count == 1


def test_reclaim_dead_letter_hook_guard_is_atomic_under_genuine_concurrency(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """lode-elc8: closes the residual read-then-write window lode-uda1's own
    guard left open (docs/storage.md "A dead-letter hook's write can race a
    late success too"). The PREVIOUS guard read the head via a separate,
    unprotected ``SELECT`` *before* calling ``ingest_snapshot`` (which opens
    its own independent transaction) -- so a real snapshot committed in the
    gap between that read and the write was still clobbered. The test above
    (``test_reclaim_dead_letter_hook_does_not_beat_a_real_snapshot``) only
    proves correctness for ONE hand-picked call order (the real snapshot
    fully commits, THEN the reclaim runs) -- it cannot, by construction,
    exercise the narrow gap between a read and a later write on the SAME
    connection, since there is only one connection involved.

    This test forces GENUINE two-connection concurrency instead: a second,
    independent ``sqlite3`` connection to the same on-disk database plays
    the still-in-flight handler, holding its real snapshot's write
    transaction OPEN (uncommitted) while the dead-letter hook's guarded call
    runs concurrently on the primary connection. Under this repo's actual
    settings (``PRAGMA journal_mode = WAL``, default deferred
    ``isolation_level``), a *plain autocommit* ``SELECT`` is NOT blocked by
    another connection's still-open write transaction -- it happily reads
    the last-committed state (verified empirically, see ``docs/storage.md``)
    -- so the old, separate-read guard would see "no head yet", proceed past
    its check, and only contend for the write lock inside its own
    ``ingest_snapshot`` call, landing its tombstone right after the real
    snapshot commits and clobbering it. The new guard
    (``ingest_snapshot``'s ``skip_if_head_at_or_after``) closes exactly this
    gap: forcing the write lock as its OWN transaction's very first
    statement means its head-read can only run once any earlier writer has
    fully committed (or while a later one is still blocked waiting on us) --
    never in the gap between someone else's read and write.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.worker import _refresh_dead_letter_hook

    external_id = "https://example.com/genuinely-concurrent-race"
    claimed_at = _past_iso(settings.stale_running_timeout_s + 60)
    hold_seconds = 0.3

    # The externals ROW MUST ALREADY EXIST -- this is what makes the test
    # exercise the interleaving that actually CORRUPTS, and it is also the
    # production shape (a `refresh` job only exists for an external that was
    # already drawn down, so its `externals` row was created by that first
    # ingest). Without this row, the pre-elc8 read-then-write guard does not
    # silently clobber at all: its unguarded `ingest_snapshot` takes the
    # `if not exists` branch, tries a plain `INSERT INTO externals`, and --
    # having read "not exists" BEFORE blocking on conn2's write lock -- hits
    # `sqlite3.IntegrityError: UNIQUE constraint failed` once conn2's commit
    # lands the row first. That aborts the whole tombstone transaction, so the
    # old code fails LOUDLY and the head survives. Mutation-testing this test
    # against the pre-elc8 guard with no pre-existing row therefore goes red on
    # a CRASH, not on the corruption -- proving the wrong thing. With the row
    # present, the old guard's `_external_head` returns (exists=True, head=None)
    # from its stale pre-lock read, skips the externals INSERT, blocks on the
    # snapshots INSERT, and then lands the tombstone and repoints the head onto
    # it AFTER conn2's real snapshot committed: head == 'tombstone' over a
    # successful fetch, the exact absorbing corruption lode-uda1/lode-elc8 exist
    # to prevent (verified by mutation: red with head 'tombstone' != 'ok').
    conn.execute(
        "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
        (external_id, SOURCE_TYPE_WEB),
    )
    conn.commit()

    # A second, independent connection to the SAME on-disk database --
    # stands in for the still-in-flight handler's own connection/transaction.
    # Created AND used entirely inside the background thread (sqlite3
    # connections are thread-affine by default) -- writes the real snapshot
    # directly (mirroring ingest_snapshot's own write shape) and holds the
    # transaction open, uncommitted, for `hold_seconds`, long enough that the
    # guarded call below can only finish by genuinely waiting on this
    # connection's write lock, not by racing past a stale read.
    lock_acquired = threading.Event()

    def hold_real_snapshot_write_open() -> None:
        conn2 = sqlite3.connect(db_path, timeout=5.0)
        conn2.execute("BEGIN")
        conn2.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, ?) "
            "ON CONFLICT (external_id) DO NOTHING",
            (external_id, SOURCE_TYPE_WEB),
        )
        conn2.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, external_id, body, status, fetched_at) "
            "VALUES (?, ?, ?, 'ok', ?)",
            (
                "real-snapshot-id",
                external_id,
                "the real, successfully-fetched body",
                _now_iso(),
            ),
        )
        conn2.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            ("real-snapshot-id", external_id),
        )
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn2.commit()
        conn2.close()

    holder = threading.Thread(target=hold_real_snapshot_write_open)
    holder.start()
    # Wait for the holder to have actually issued its writes (so it holds
    # the write lock) before starting the guarded call -- deterministic,
    # not a fixed sleep guess.
    assert lock_acquired.wait(timeout=5.0), (
        "holder thread never acquired its write lock"
    )

    started_at = time.monotonic()
    _refresh_dead_letter_hook(conn, external_id, "timeout", claimed_at, settings)
    elapsed = time.monotonic() - started_at
    holder.join()

    # The guarded call was genuinely blocked on conn2's write lock, not
    # merely lucky in a race: it could not have completed before conn2
    # released it. (Slack under hold_seconds for scheduling noise.)
    assert elapsed >= hold_seconds - 0.1, (
        f"guarded call returned after only {elapsed:.3f}s -- expected it to "
        f"block for roughly {hold_seconds}s waiting on conn2's write lock"
    )

    status, body = conn.execute(
        "SELECT s.status, s.body FROM snapshots s "
        "JOIN externals e ON e.head_snapshot_id = s.snapshot_id "
        "WHERE e.external_id = ?",
        (external_id,),
    ).fetchone()
    assert status == "ok"
    assert body == "the real, successfully-fetched body"
    # Only the one real snapshot exists -- no tombstone row was inserted.
    (snapshot_count,) = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert snapshot_count == 1


def test_reclaim_dead_letter_hook_deduped_success_is_atomic_under_genuine_concurrency(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """lode-9tj4, the concurrent half: a successful-but-DEDUPED refresh must
    survive the tombstone winning the write lock, exactly as a
    content-CHANGED one does (the test above).

    ``test_reclaim_dead_letter_hook_recognizes_a_deduped_success`` only proves
    the SEQUENTIAL order (the dedup fully commits, THEN the reclaim runs). That
    is the ticket's stated scenario, but it is not the only reachable one, and
    the dedup path is *uniquely* fragile in the other order:

    A content-CHANGED refresh SELF-HEALS when the tombstone lands first --
    ``ingest_snapshot`` sees a head it does not match, inserts its snapshot and
    drags ``externals.head_snapshot_id`` back onto it (this is precisely what
    lode-elc8 verified empirically: the real snapshot "waits the lock out, lands
    cleanly and becomes head"). A DEDUPED refresh has no such recovery: it only
    bumps ``fetched_at`` and NEVER moves the head. So if it decides "this is a
    dedup" from a head read taken BEFORE it holds the write lock, a tombstone
    committing in that gap stays head **forever** -- and reconcile's refresh
    sweep skips tombstoned heads, so nothing ever revisits it. That is lode-uda1's
    absorbing corruption, reached through the one door lode-9tj4 opened by making
    the dedup path a writer.

    The fix is that ``ingest_snapshot``'s lock-taking ``externals`` upsert now
    runs first for EVERY caller, not just the guarded one, so the dedup decision
    is made under the write lock and can never rest on a stale head.

    Mirror-image of the elc8 test above: the holder thread stands in for the
    dead-letter hook's guarded write (which legitimately did NOT fire its guard
    -- at the moment it read, the head's ``fetched_at`` genuinely predated the
    claim), while the REAL, deduping ``ingest_snapshot`` runs on the primary
    connection. Mutation-tested: revert the "upsert first for every caller"
    ordering and this fails with head == 'tombstone' over a successful fetch.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.externals import ingest_snapshot, tombstone_body

    external_id = "https://example.com/deduped-success-concurrent-race"
    body = "stable, unchanging body"
    hold_seconds = 0.3

    # An existing 'ok' head whose fetched_at PREDATES the job's claim -- the
    # production shape (a refresh job only exists for an already-drawn-down
    # external) and the precondition for the guard's blind spot: the hook,
    # reading this head, correctly sees nothing newer than its own claim.
    seeded = ingest_snapshot(conn, external_id, SOURCE_TYPE_WEB, body)
    conn.execute(
        "UPDATE snapshots SET fetched_at = ? WHERE snapshot_id = ?",
        ("2000-01-01T00:00:00.000000Z", seeded.snapshot_id),
    )
    conn.commit()

    # The dead-letter hook's tombstone write, holding its transaction OPEN --
    # i.e. it WON the write lock. Written directly (mirroring the hook's write
    # shape) for the same reason the elc8 test above does so: the property under
    # test is what the OTHER side does while this lock is held.
    lock_acquired = threading.Event()

    def hold_tombstone_write_open() -> None:
        conn2 = sqlite3.connect(db_path, timeout=5.0)
        conn2.execute("BEGIN")
        conn2.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, external_id, body, status, fetched_at) "
            "VALUES (?, ?, ?, 'tombstone', ?)",
            (
                "tombstone-snapshot-id",
                external_id,
                tombstone_body("timeout"),
                _now_iso(),
            ),
        )
        conn2.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            ("tombstone-snapshot-id", external_id),
        )
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn2.commit()
        conn2.close()

    holder = threading.Thread(target=hold_tombstone_write_open)
    holder.start()
    assert lock_acquired.wait(timeout=5.0), (
        "holder thread never acquired its write lock"
    )

    # The handler's successful refetch: identical content. Pre-fix, its head
    # read ran in autocommit (unblocked by conn2's open write txn -- WAL readers
    # do not block), saw the still-'ok' head, committed to the dedup path, and
    # then only bumped fetched_at on a row that was no longer head by the time
    # the UPDATE landed.
    started_at = time.monotonic()
    ingest_snapshot(conn, external_id, SOURCE_TYPE_WEB, body)
    elapsed = time.monotonic() - started_at
    holder.join()

    # It genuinely waited on conn2's write lock rather than racing past a stale
    # read -- the lock-taking upsert is its first statement now.
    assert elapsed >= hold_seconds - 0.1, (
        f"deduping ingest returned after only {elapsed:.3f}s -- expected it to "
        f"block for roughly {hold_seconds}s waiting on conn2's write lock"
    )

    # THE PROPERTY: the successfully re-verified content is still head. (Pre-fix
    # this is 'tombstone' -- the absorbing corruption.)
    status, head_body = conn.execute(
        "SELECT s.status, s.body FROM snapshots s "
        "JOIN externals e ON e.head_snapshot_id = s.snapshot_id "
        "WHERE e.external_id = ?",
        (external_id,),
    ).fetchone()
    assert status == "ok"
    assert head_body == body


def test_run_refresh_dead_letter_still_tombstones_over_older_content(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """The lode-uda1 guard must NOT block the pre-existing, intentional case:
    a LATER refresh job (lode-w0h.6's staleness policy) that exhausts its
    retries still tombstones even though the external already has OLDER 'ok'
    content from an earlier, successful refresh. The guard only exempts a
    head fetched AT OR AFTER *this* job's own claim -- content that predates
    the claim is exactly the case the hook's docstring already commits to
    tombstoning unconditionally, unaffected by lode-uda1.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.externals import ingest_snapshot

    external_id = "https://example.com/staleness-refresh"
    # Prior, older successful content -- fetched_at is "now", strictly BEFORE
    # the claim stamped below.
    ingest_snapshot(conn, external_id, SOURCE_TYPE_WEB, "the old, still-live body")

    job_id = _insert_job(
        conn, "refresh", external_id, attempts=settings.retry_max_attempts - 1
    )
    # Claimed strictly after the ingest above (an hour out, like the existing
    # ABA-guard tests use, to unmistakably clear it).
    _claim_one(conn, ("refresh",), _future_iso())
    ok = run_one(
        conn, job_id, db_path, settings, _always_raising_refresh_registry("timeout")
    )
    assert ok is False
    assert _job(conn, job_id)["status"] == "dead"

    status, body = conn.execute(
        "SELECT s.status, s.body FROM snapshots s "
        "JOIN externals e ON e.head_snapshot_id = s.snapshot_id "
        "WHERE e.external_id = ?",
        (external_id,),
    ).fetchone()
    assert status == "tombstone"
    assert "timeout" in body


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

    def _boom(conn, target_version, last_error, claimed_at, settings):  # noqa: ARG001
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
# Transient-failure CAS guard (lode-3jte) — the sibling of the AuthError
# resurrection guard above (lode-9yy), now applied to run_one's
# `except Exception` arm via jobs.record_job_failure's own CAS guard.
# ---------------------------------------------------------------------------


def test_run_transient_failure_does_not_resurrect_a_reclaimed_job(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A transient failure's UPDATE must not resurrect a job a concurrent
    reclaim already drove to a terminal 'dead' while the handler was in flight.

    ``cli._enrich_immediately`` reaches ``run_one`` via ``claim_and_run_one``
    with no worker lock held, so a concurrent ``lode work`` drain can hit
    ``_reclaim_stale_running`` and dead-letter this very row mid-handler.
    Unguarded, the transient arm's UPDATE would flip that dead job back to
    'failed' with a fresh backoff and double-charge attempts on top -- exactly
    the resurrection the AuthError arm was hardened against (lode-9yy), just
    never mirrored on this arm until lode-3jte. The handler below stands in
    for that concurrent reclaim: it terminalizes the row (and runs the
    dead-letter hook itself, as the real reclaim path does) before raising a
    plain (non-AuthError) exception.
    """
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())

    def _reclaimed_then_transient_error(conn_, tv, db, s):
        with conn_:
            conn_.execute(
                "UPDATE jobs SET status = 'dead', attempts = ?, "
                "last_error = 'reclaimed' WHERE id = ?",
                (settings.retry_max_attempts, job_id),
            )
        raise RuntimeError("transient blip (test)")

    ok = run_one(
        conn, job_id, db_path, settings, {"embed": _reclaimed_then_transient_error}
    )

    assert ok is False
    row = _job(conn, job_id)
    assert row["status"] == "dead"  # NOT resurrected to 'failed'
    assert row["attempts"] == settings.retry_max_attempts  # NOT double-charged
    assert row["last_error"] == "reclaimed"  # NOT overwritten by run_one's own message


def test_run_transient_failure_does_not_run_dead_letter_hook_twice(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """The dead-letter hook must fire exactly once (lode-at8) even when the
    claim was lost to a concurrent reclaim.

    Whoever wins the CAS (the reclaim, here simulated inline) already ran the
    hook; run_one's own transient arm must see claim_lost=True and skip
    running it a second time for the same job.
    """
    external_id = "https://example.com/reclaimed-mid-handler"
    job_id = _insert_job(
        conn, "refresh", external_id, attempts=settings.retry_max_attempts - 1
    )
    _claim_one(conn, ("refresh",), _now_iso())

    hook_calls: list[str] = []

    def _counting_hook(conn_, target_version, last_error, claimed_at, settings_):  # noqa: ARG001
        hook_calls.append(target_version)

    def _reclaimed_then_transient_error(conn_, tv, db, s):
        # Stand-in for a concurrent _reclaim_stale_running: terminalize the
        # row AND run its dead-letter hook, exactly as the real reclaim path
        # does, before the stalled handler finally raises.
        with conn_:
            conn_.execute(
                "UPDATE jobs SET status = 'dead', attempts = ?, "
                "last_error = 'reclaimed' WHERE id = ?",
                (settings.retry_max_attempts, job_id),
            )
        _counting_hook(conn_, external_id, "reclaimed", None, s)
        raise RuntimeError("transient blip (test)")

    with mock.patch.dict("lode.worker._DEAD_LETTER_HOOKS", {"refresh": _counting_hook}):
        ok = run_one(
            conn,
            job_id,
            db_path,
            settings,
            {"refresh": _reclaimed_then_transient_error},
        )

    assert ok is False
    assert hook_calls == [external_id]  # exactly once, not twice


# ---------------------------------------------------------------------------
# ABA guard (lode-nggm hole 2) — the CAS on `status = 'running'` alone cannot
# tell a job's OWN stale claim from a NEWER one on the same row: the row can
# cycle running -> failed -> pending -> running (a different claimed_at,
# possibly a different worker) inside a single stall that already exceeded
# stale_running_timeout_s, entirely before the original, still-stalled caller
# below finally writes. Guarding on claimed_at too (not just status) closes
# this: run_one read claimed_at once, at the top, before the handler ran.
# ---------------------------------------------------------------------------


def test_run_transient_failure_does_not_clobber_a_job_reclaimed_to_a_new_claim(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """A status-only guard would wrongly match a *different*, newer 'running'
    claim on the same row and clobber it. The claimed_at guard must not: it
    compares against the value run_one read at the top, before the handler
    (here standing in for an arbitrarily long stall) ran.
    """
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    new_claimants_attempts = 9
    # Must be unmistakably DIFFERENT from the claimed_at _claim_one just
    # stamped, or the guard matches and this test silently stops testing the
    # ABA case. Not `_future_iso(0)`: that is a raw wall-clock read at
    # millisecond precision, while _claim_one stamps jobs.now_iso() — a
    # different clock (jobs.now() deliberately runs *ahead* of the wall clock,
    # see its docstring) at the same precision, only a SELECT/UPDATE/SELECT
    # earlier. The two can collide on the same millisecond. An hour out cannot.
    new_claimants_claimed_at = _future_iso()

    def _cycled_to_a_new_claim_then_transient_error(conn_, tv, db, s):
        # Stand-in for the FULL cycle a concurrent reclaim + reset + re-claim
        # already ran through while this handler was stalled: reclaimed to
        # 'failed', reset to 'pending', re-claimed to 'running' again with a
        # FRESH claimed_at by a different worker. status is 'running' again —
        # exactly what a status-only guard would accept as still its own.
        with conn_:
            conn_.execute(
                "UPDATE jobs SET status = 'running', attempts = ?, "
                "last_error = NULL, claimed_at = ? WHERE id = ?",
                (new_claimants_attempts, new_claimants_claimed_at, job_id),
            )
        raise RuntimeError("transient blip (test)")

    ok = run_one(
        conn,
        job_id,
        db_path,
        settings,
        {"embed": _cycled_to_a_new_claim_then_transient_error},
    )

    assert ok is False
    row = _job(conn, job_id)
    # Untouched — still exactly the new claimant's row, not overwritten by
    # this call's stale attempts/claimed_at.
    assert row["status"] == "running"
    assert row["attempts"] == new_claimants_attempts
    assert row["last_error"] is None
    # It is specifically the NEW claim that survived, not merely *a* running row.
    assert row["claimed_at"] == new_claimants_claimed_at


def test_run_auth_error_reset_does_not_clobber_a_job_reclaimed_to_a_new_claim(
    conn: sqlite3.Connection, db_path: Path, settings: Settings
) -> None:
    """Same ABA guard on the AuthError reset arm."""
    job_id = _insert_job(conn)
    _claim_one(conn, ("embed",), _now_iso())
    new_claimants_attempts = 9
    # An hour out, for the same reason as the transient test above: a raw
    # `_future_iso(0)` can land on the same millisecond as _claim_one's stamp.
    new_claimants_claimed_at = _future_iso()

    def _cycled_to_a_new_claim_then_auth_error(conn_, tv, db, s):
        with conn_:
            conn_.execute(
                "UPDATE jobs SET status = 'running', attempts = ?, "
                "last_error = NULL, claimed_at = ? WHERE id = ?",
                (new_claimants_attempts, new_claimants_claimed_at, job_id),
            )
        raise AuthError("no credentials (test)")

    with pytest.raises(AuthError):
        run_one(
            conn,
            job_id,
            db_path,
            settings,
            {"embed": _cycled_to_a_new_claim_then_auth_error},
        )

    row = _job(conn, job_id)
    # Untouched — NOT reset to 'pending' over the new claimant's row.
    assert row["status"] == "running"
    assert row["attempts"] == new_claimants_attempts
    assert row["last_error"] is None
    # It is specifically the NEW claim that survived, not merely *a* running row.
    assert row["claimed_at"] == new_claimants_claimed_at


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
    # Anchor the backoff check to a baseline captured BEFORE the reclaim call,
    # not to "now" re-read after it. next_attempt_at is computed once, inside
    # _reclaim_stale_running, off the same monotonically-nondecreasing clock
    # (worker._now); comparing against a snapshot taken here beforehand means
    # the assertion below can never race however long the test takes to reach
    # it (lode-0x1 — this used to compare against a live `_now_iso()` call two
    # statements later, which raced the 1.0s backoff under load).
    before = _now_iso()
    count = _reclaim_stale_running(conn, settings)
    assert count == 1
    row = _job(conn, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "reclaimed" in row["last_error"]
    # attempts goes 0 -> 1, so the applied backoff is exactly
    # retry_backoff_base_s * 2**(1-1) == retry_backoff_base_s (see
    # worker._backoff_next_attempt_at). Assert the full delay was applied,
    # not just "some time passed" -- the intent is "a backoff was applied".
    delta = (_parse_iso(row["next_attempt_at"]) - _parse_iso(before)).total_seconds()
    assert delta >= settings.retry_backoff_base_s


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


def test_reclaim_and_record_job_failure_agree_on_the_dead_letter_gate(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
) -> None:
    """_reclaim_stale_running and jobs.record_job_failure must dead-letter at
    the SAME attempts count (lode-yb9t) -- a crash-reclaimed job is supposed to
    obey the identical max-attempts gate as a cleanly-failed one, and prior to
    this ticket nothing but a docstring note enforced that. Both now delegate
    to jobs.next_failure_state, so this test catches either path silently
    reimplementing its own (possibly drifted) gate again in the future.

    The sweep is DERIVED from settings.retry_max_attempts rather than hard-coded,
    so its last iteration always lands exactly ON the dead-letter boundary. With
    hard-coded params, retuning that setting would slide the whole sweep below
    the gate and leave this test asserting False == False -- green, and no longer
    covering the one thing it exists for.
    """
    observed_dead: list[bool] = []

    for attempts_before in range(settings.retry_max_attempts):
        stale_job = _insert_job(
            conn,
            target_version=f"stale-{attempts_before}",
            status="running",
            attempts=attempts_before,
            claimed_at=_past_iso(settings.stale_running_timeout_s + 60),
        )
        # The control row must be a FRESH claim, not a stale one: the reclaim
        # selects `claimed_at IS NULL OR claimed_at <= cutoff`, so a claimed_at of
        # NULL (the _insert_job default) would put this row in the reclaim's own
        # sweep and it would be failed by the reclaim path before
        # record_job_failure ever saw it -- leaving this test comparing the
        # reclaim path against itself.
        control_claimed_at = _now_iso()
        control_job = _insert_job(
            conn,
            target_version=f"control-{attempts_before}",
            status="running",
            attempts=attempts_before,
            claimed_at=control_claimed_at,
        )

        _reclaim_stale_running(conn, settings)
        reclaimed_row = _job(conn, stale_job)

        _, record_dead, _ = jobs.record_job_failure(
            conn, control_job, attempts_before, control_claimed_at, "boom", settings
        )
        reclaimed_dead = reclaimed_row["status"] == "dead"

        # The two paths agree with each other...
        assert reclaimed_dead == record_dead
        assert reclaimed_row["attempts"] == _job(conn, control_job)["attempts"]
        # ...and both agree with an INDEPENDENTLY stated gate. Mutual agreement
        # alone is structurally guaranteed now that both call next_failure_state,
        # so it would survive a >= -> > drift *inside* that shared function; this
        # line is what pins the gate's actual value.
        assert record_dead == (attempts_before + 1 >= settings.retry_max_attempts)
        observed_dead.append(reclaimed_dead)

    # Vacuity guard: the sweep must have exercised BOTH sides of the gate.
    assert observed_dead[-1] is True, "sweep never reached the dead-letter gate"
    assert False in observed_dead, "sweep never exercised the retry side of the gate"


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


def test_drain_logs_progress_lines_for_batch_pre_steps_and_run_loop(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """drain() logs a start/done progress line naming each of its named steps
    (lode-olmi.15): the two batch pre-steps and the main claim/run loop --
    before this, drain() logged nothing while any of these actually ran.

    An embed-only queue exercises this with no enrich jobs pending, so
    ``_batch_collect_enrich``/``_batch_submit_enrich`` return immediately
    without needing a batch client or the Anthropic SDK.
    """
    _insert_job(conn, target_version="ver-1")

    with caplog.at_level(logging.INFO):
        drain(conn, db_path, settings, _registry=_noop_registry())

    assert "drain.batch_collect: starting" in caplog.text
    assert "drain.batch_collect: done" in caplog.text
    assert "drain.batch_submit: starting" in caplog.text
    assert "drain.batch_submit: done" in caplog.text
    assert "drain.run_jobs: starting" in caplog.text
    assert "drain.run_jobs: done" in caplog.text
    assert "drain.run_jobs: running job" in caplog.text


def test_drain_progress_heartbeats_a_slow_job(
    conn: sqlite3.Connection,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A handler slower than progress_heartbeat_interval_s gets a heartbeat
    line under drain.run_jobs -- the main-loop analogue of a slow reconcile
    step or a slow batch pre-step (lode-olmi.15).
    """
    _insert_job(conn, target_version="ver-1")
    slow_settings = Settings(progress_heartbeat_interval_s=0.05)
    slow_registry: dict[str, HandlerFn] = {
        "embed": lambda conn, tv, db, s: (time.sleep(0.2), None)[1]
    }

    with caplog.at_level(logging.INFO):
        drain(conn, db_path, slow_settings, _registry=slow_registry)

    assert "drain.run_jobs: still running" in caplog.text


def test_drain_embed_only_does_not_import_the_sdk(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    forget_sdk_imports: None,
) -> None:
    """An embed-only drain must never import the **Anthropic SDK** (lode-4q97).

    The end-to-end assertion for the whole ticket: embeds come from the LOCAL
    fastembed model, so an unkeyed user draining nothing but embeds must not pay
    the ~0.32s SDK import. ``drain`` reaches Anthropic-adjacent modules two ways --
    the batch pre-steps' ``lode.enrich`` imports, and its own unconditional
    ``from lode.auth import AuthError`` -- and this asserts on the SDK itself, so
    it holds no matter which path a regression comes back through. (Asserting only
    ``"lode.enrich" not in sys.modules`` would pass while the SDK was still fully
    imported via ``lode.auth``, which is exactly the gap that shipped once.)

    ``forget_sdk_imports`` evicts the import graph first; without it this file's own
    module-level imports make the assertion vacuous.
    """
    for i in range(3):
        _insert_job(conn, target_version=f"ver-{i}")
    n = drain(conn, db_path, settings, _registry=_noop_registry())

    assert n == 3
    assert "anthropic" not in sys.modules, (
        "embed-only drain imported the Anthropic SDK despite having no enrich "
        "work to do and needing no credentials"
    )


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
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
) -> None:
    """An enrich job is never dead-lettered by a repeatedly-failing *transient*
    batch submission, no matter how many drain passes run.

    This is the TRANSIENT half of the taxonomy (docs/storage.md "Transient vs.
    permanent job failures") and is deliberately driven by an ordinary
    ``RuntimeError`` from the Batches API call -- NOT by ``AuthError``, which
    takes the permanent path and is covered by
    ``test_drain_raises_auth_error_leaving_job_pending_uncharged`` below.

    The invariant: ``_batch_submit_enrich``'s transient revert sets
    ``status='failed'`` with a backoff but does **not** increment ``attempts``,
    so no number of drain passes can walk an enrich job up to
    ``retry_max_attempts`` and dead-letter it via the batch-submit failure
    path. Dead-letter only happens inside ``collect_enrich_batch``, when the
    Batches API itself returns an error result after ``retry_max_attempts``.

    lode-9yy note: this coverage predates lode-9yy and must survive it. The
    AuthError carve-out added there must not disturb the transient accounting
    this test pins -- if it ever over-broadens to catch ordinary exceptions,
    the enrich job here would be reset to 'pending' instead of 'failed' and
    this test goes red.
    """
    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(conn)

    # Transient API failure: the batch create raises on every pass.
    client = mock.MagicMock()
    client.beta.messages.batches.create.side_effect = RuntimeError("api down")

    for _ in range(5):
        drain(
            conn,
            db_path,
            settings,
            _registry=_noop_registry(),
            _batch_client=client,
        )

    row = _job(conn, job_id)
    # Reverted to 'failed' with a backoff, never 'dead', and never charged an
    # attempt -- so there is no accumulation across passes to dead-letter.
    assert row["status"] == "failed"
    assert row["attempts"] == 0


def test_drain_raises_auth_error_leaving_job_pending_uncharged(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PERMANENT half: drain() raises AuthError straight through, and the
    enrich job is left 'pending' and uncharged (lode-9yy).

    Before lode-9yy a missing credential was folded into the transient revert
    above: drain() never raised, so `lode work` reported an ordinary failed job
    and re-submitted it on every pass forever -- build_client's actionable
    message never reached the operator, and the job could never succeed. Now
    the very first drain() surfaces it, with ``attempts`` untouched so the
    retry budget is not spent on something retrying can never fix.

    The failure is driven by a ``build_client`` that *deterministically* raises
    (lode-85q). It used to be driven by the ambient environment instead -- the
    real, un-mocked ``build_client()`` happening to fail at construction because
    CI has no ``ANTHROPIC_API_KEY`` -- which made this test prove two different
    things on two different machines: on a keyed dev box construction would
    succeed and the submit-failure path under test was never exercised at all.
    That is precisely the class of bug lode-85q exists to kill, so it is fixed
    here rather than papered over with an escape-hatch marker.
    """
    import lode.enrich as enrich_mod

    def _no_credentials() -> object:
        raise AuthError("no credentials (test)")

    monkeypatch.setattr(enrich_mod, "build_client", _no_credentials)

    enqueue_derive_jobs(conn, "ver-1")
    # A single drain() call now raises -- the permanent failure is not
    # retried, so there is no "many failing passes" to survive.
    with pytest.raises(AuthError):
        drain(conn, db_path, settings, _registry=_noop_registry())

    row = conn.execute(
        "SELECT status, attempts FROM jobs WHERE type = 'enrich'"
    ).fetchone()
    status, attempts = row
    assert status == "pending"
    assert attempts == 0  # uncharged — never dead, never even 'failed'


def test_drain_still_runs_embed_jobs_when_credentials_are_missing(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing Anthropic credential must NOT starve the local embed jobs.

    Regression test for the lode-9yy review. ``embed`` jobs are produced by the
    LOCAL fastembed model and have nothing to do with Anthropic credentials, but
    both enrich batch pre-steps run BEFORE drain's main claim/run loop. When the
    permanent-failure carve-out first raised straight out of ``_batch_submit_enrich``,
    an unkeyed user's embeds never drained again: every ``add`` enqueues a pending
    enrich job, so the pre-step aborted every single ``drain`` before the first
    embed ever ran, and the dense half of retrieval died silently.

    The contract: drain does all the credential-free work it can, THEN raises.
    """
    import lode.enrich as enrich_mod

    def _no_credentials() -> object:
        raise AuthError("no credentials (test)")

    monkeypatch.setattr(enrich_mod, "build_client", _no_credentials)

    # One note → one pending embed job + one pending enrich job.
    _insert_note_worker(conn)
    enqueue_derive_jobs(conn, "ver-1")

    embedded: list[str] = []

    def _embed(conn_, tv, db, s):
        embedded.append(tv)
        return None

    # The AuthError still surfaces to the caller...
    with pytest.raises(AuthError):
        drain(conn, db_path, settings, _registry={"embed": _embed})

    # ...but only AFTER the embed job ran to completion.
    assert embedded == ["ver-1"], "embed job was starved by the missing credential"
    states = dict(conn.execute("SELECT type, status FROM jobs").fetchall())
    assert states["embed"] == "done"
    # The enrich job is left pending + uncharged, for a later credentialed run.
    assert states["enrich"] == "pending"


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
    # embed() also duck-type-probes model_revision() (lode-g274.4) -- real
    # FastEmbedEmbedder.model_revision() calls the (unpatched) _load(), which
    # would otherwise both download the real ONNX model and hit the network
    # for the revision probe. Stub it offline like embed_passages above.
    monkeypatch.setattr(FastEmbedEmbedder, "model_revision", lambda self: None)


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


@pytest.mark.parametrize("pre_step", [_batch_submit_enrich, _batch_collect_enrich])
def test_batch_pre_step_with_no_work_does_not_import_enrich(
    conn: sqlite3.Connection,
    settings: Settings,
    forget_sdk_imports: None,
    pre_step,
) -> None:
    """Both batch pre-steps import ``lode.enrich`` only *below* their early-return
    guard (lode-4q97), so a drain with no enrich work does not import it at all.

    Hygiene rather than the load-bearing fix -- ``lode.enrich`` is cheap to import
    now either way (see ``test_importing_module_does_not_import_the_sdk``) -- but
    there is still no reason to import it to do nothing.
    """
    assert pre_step(conn, settings, _client=_fake_batch_client_worker()) == 0
    assert "lode.enrich" not in sys.modules
    assert "anthropic" not in sys.modules


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


def test_batch_submit_auth_error_resets_to_pending_and_reraises(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_batch_submit_enrich treats AuthError as permanent (lode-9yy): the
    pre-claimed job is reset to 'pending' (uncharged, not 'failed' with
    backoff) and the exception is re-raised rather than absorbed."""
    import lode.enrich as enrich_mod

    _insert_note_worker(conn)
    job_id = _insert_enrich_job_worker(conn)

    def _no_credentials() -> object:
        raise AuthError("no credentials (test)")

    # No explicit _client -- forces submit_enrich_batch's own build_client()
    # call, which is what actually raises AuthError in production.
    monkeypatch.setattr(enrich_mod, "build_client", _no_credentials)

    with pytest.raises(AuthError):
        _batch_submit_enrich(conn, settings)

    row = _job(conn, job_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert "no credentials" in row["last_error"]


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
        # Match the candidate SELECT structurally, not by its ORDER BY clause: a
        # matcher keyed on the exact sort column silently *disarms* this race (and
        # the test then passes while asserting nothing) the moment that clause is
        # edited — which is exactly what happened when it read `ORDER BY created`
        # and the sort moved to `id` (lode-t1y). `SELECT ... FROM jobs` is only
        # emitted by the candidate query here; the per-row CAS is an `UPDATE jobs`.
        if not self._raced and "FROM jobs" in sql and "status = 'pending'" in sql:
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


# ---------------------------------------------------------------------------
# jobs.now — the queue's clock (lode-t1y; moved worker -> jobs in lode-ajda so
# lode.enrich shares it instead of reading a raw, unanchored wall clock)
# ---------------------------------------------------------------------------


class _FakeClock:
    """Drives the jobs module's two clock sources independently.

    ``wall`` stands in for ``CLOCK_REALTIME`` (which the OS may step in *either*
    direction); ``mono`` for ``time.monotonic()`` (which never decreases).
    """

    def __init__(self) -> None:
        self.wall = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
        self.mono = 1000.0

    def tick(self, seconds: float) -> None:
        """Time passes, no clock event: both sources advance together."""
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def step_wall(self, seconds: float) -> None:
        """The OS steps the wall clock alone (NTP / hypervisor catch-up)."""
        self.wall += timedelta(seconds=seconds)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Put lode.jobs's clock under test control and reset its anchor."""
    fake = _FakeClock()

    class _SteppableDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return fake.wall

    # Rebind the *names* inside lode.jobs only — the real time/datetime
    # modules are untouched, so nothing else in the process sees a fake clock.
    monkeypatch.setattr(jobs, "_now_epoch", datetime.min.replace(tzinfo=UTC))
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: fake.mono))
    monkeypatch.setattr(jobs, "datetime", _SteppableDatetime)
    return fake


def test_now_does_not_go_backward_when_the_wall_clock_steps_back(
    clock: _FakeClock,
) -> None:
    """A backward wall-clock step must not rewind now() (the lode-t1y strand).

    _claim_one reads 'next_attempt_at <= now' as "nothing is ready yet" and
    drain's loop breaks on the first miss, so one backward tick would strand an
    already-eligible job for the rest of the pass.
    """
    first = jobs.now()
    clock.tick(0.010)
    clock.step_wall(-5)  # OS yanks CLOCK_REALTIME back, mid-drain

    second = jobs.now()

    assert second >= first
    # ...and it still advanced by the time that genuinely elapsed.
    assert second == first + timedelta(seconds=0.010)


def test_now_adopts_a_forward_wall_clock_step_instead_of_lagging_forever(
    clock: _FakeClock,
) -> None:
    """now() must never read *behind* the wall clock.

    Not every timestamp now() is compared against comes from now(): jobs.next_attempt_at
    defaults to SQLite's own (unanchored) strftime('now'), and other processes
    stamp rows from their own wall clocks. A clock anchored once at first use
    would lag CLOCK_REALTIME permanently after a forward step, making every
    freshly enqueued job look not-yet-due — the same stranded job, from the
    opposite direction.
    """
    jobs.now()  # establish the anchor
    clock.tick(0.010)
    clock.step_wall(5)  # NTP correction / hypervisor catch-up, forward

    assert jobs.now() >= clock.wall


def test_now_never_decreases_across_a_forward_step_then_a_correction_back(
    clock: _FakeClock,
) -> None:
    """The two guarantees must hold *together*, not one at a time.

    Simply returning max(monotonic estimate, wall clock) would satisfy each
    guarantee alone but still hand back a *decreasing* reading here: the reading
    taken during the forward excursion could not be un-seen once the wall clock
    was corrected back. Ratcheting the anchor forward is what closes that.
    """
    readings = [jobs.now()]
    clock.tick(0.001)
    clock.step_wall(30)  # big forward excursion...
    readings.append(jobs.now())
    clock.tick(0.001)
    clock.step_wall(-30)  # ...corrected straight back again
    readings.append(jobs.now())
    clock.tick(0.001)
    readings.append(jobs.now())

    assert readings == sorted(readings)


def test_claim_and_run_one_claims_a_job_enqueued_moments_before_a_backward_clock_step(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    clock: _FakeClock,
) -> None:
    """lode-0dnk: enqueue_derive_jobs's ``next_attempt_at`` and _claim_one's
    ``now`` comparison must share the SAME clock, or the CLI's immediate-enrich
    fast path (``claim_and_run_one``, called moments after the enqueue in the
    very same process) can silently fail to claim the job it just enqueued.

    Before this fix, ``next_attempt_at`` came from the schema's raw SQLite
    ``strftime('now')`` DEFAULT (``CLOCK_REALTIME``), while the claim compares
    against ``jobs.now_iso()`` (the forward-ratcheted queue clock, lode-t1y). A
    backward ``CLOCK_REALTIME`` step (NTP correction / hypervisor catch-up --
    routine under load, ``docs/storage.md``) landing between the enqueue and
    the very next ``now()`` call in that process -- which, being the FIRST
    ``now()`` call in a fresh process/worker, has no prior ratchet reading to
    protect it -- could make the freshly-enqueued job's own
    ``next_attempt_at`` read as "in the future" relative to the claim, so it
    silently stayed 'pending' instead of running immediately. This is the
    mechanism behind ``test_cli.py::test_add_claims_own_job_not_backlog_job``'s
    intermittent xdist flake (confirmed via a scripted backward-step repro --
    not reproducible by CPU load alone, since it needs a genuine
    ``CLOCK_REALTIME`` step, not mere scheduling contention).

    Anchored in the future (2030, like the sibling lode-bmg9 test) so the
    assertion below -- that the stored ``next_attempt_at`` lands strictly
    AFTER a real, unfaked ``datetime.now(UTC)`` reading -- holds regardless of
    when this test actually runs, and only holds *because* the fix routes the
    enqueue through the same fake-clock-driven ``now_iso()`` the claim reads.
    """
    clock.wall = datetime(2030, 1, 1, tzinfo=UTC)

    enqueue_derive_jobs(conn, "ver-1", types=("enrich",))

    # OS steps CLOCK_REALTIME backward right after the enqueue -- the exact
    # hazard jobs.now()'s ratchet exists to absorb (lode-t1y). This is also
    # the FIRST now() call in this (fake) process, so there is no earlier
    # ratchet reading protecting it unless the enqueue itself established one
    # -- proving the fix (stamping next_attempt_at from now_iso(), not the raw
    # DEFAULT) is what closes the gap, not an accidental earlier now() call.
    clock.tick(0.010)
    clock.step_wall(-90)

    # This really is the bug's precondition: a real (unfaked) wall-clock
    # reading right now is nowhere near 2030 -- so the schema's raw SQLite
    # DEFAULT (which the pre-fix enqueue relied on, and which our fake clock
    # cannot reach -- SQLite reads the real OS clock, not lode.jobs's
    # monkeypatched one) would have stamped this row far BEFORE where it
    # actually landed. Only because the fix routes the enqueue through the
    # SAME fake-clock-driven now_iso() does next_attempt_at land in 2030 too,
    # anchored ahead of the backward step exactly like claimed_at is in the
    # sibling lode-bmg9 test above.
    (stored_next_attempt_at,) = conn.execute(
        "SELECT next_attempt_at FROM jobs WHERE target_version = ? AND type = 'enrich'",
        ("ver-1",),
    ).fetchone()
    assert jobs.iso(datetime.now(UTC)) < stored_next_attempt_at

    enrich_registry: dict[str, HandlerFn] = {"enrich": lambda conn, tv, db, s: None}
    ran = claim_and_run_one(
        conn,
        db_path,
        settings,
        ("enrich",),
        _registry=enrich_registry,
        target_version="ver-1",
    )

    assert ran is True
    (job_id,) = conn.execute(
        "SELECT id FROM jobs WHERE target_version = ? AND type = 'enrich'", ("ver-1",)
    ).fetchone()
    assert _job(conn, job_id)["status"] == "done"


def test_reclaim_dead_letter_hook_survives_a_backward_clock_step(
    conn: sqlite3.Connection,
    db_path: Path,
    settings: Settings,
    clock: _FakeClock,
) -> None:
    """lode-bmg9: the lode-uda1 guard's two timestamps must come from the SAME
    clock, or a backward CLOCK_REALTIME step defeats it.

    Before this fix, `_refresh_dead_letter_hook` compared `snapshots.fetched_at`
    (the schema's raw SQLite `strftime('now')` DEFAULT -- CLOCK_REALTIME)
    against `jobs.claimed_at` (always stamped from the forward-ratcheted
    `jobs.now_iso()`, which by its own documented guarantee (lode-t1y) can run
    *ahead* of CLOCK_REALTIME after a backward NTP/hypervisor step, until real
    time catches up). A claim taken while the ratchet is running ahead can
    therefore out-read a real, *later* fetch's raw timestamp, so the guard's
    `>=` test fails to fire and the tombstone clobbers content that genuinely
    landed after the claim -- lode-uda1's exact corruption, reopened via a
    clock-skew precondition instead of a read/write race.

    Anchors the fake clock far in the future (2030) so the assertion below --
    that a *raw*, real-wall-clock fetched_at (what `ingest_snapshot` produced
    before this fix) would land strictly BEFORE `claimed_at` -- holds
    regardless of when this test actually runs, proving this really is the
    bug's precondition. `ingest_snapshot`'s fix (stamping `fetched_at` from
    the same ratchet `claimed_at` came from) is what keeps the guard's
    comparison single-domain and lets it fire correctly despite the
    intervening backward step.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.externals import ingest_snapshot

    external_id = "https://example.com/clock-skew"

    # Anchor the fake clock safely in the future relative to the real wall
    # clock, so the "would a raw stamp have landed before claimed_at" proof
    # below can never accidentally pass for the wrong reason.
    clock.wall = datetime(2030, 1, 1, tzinfo=UTC)

    claimed_at = jobs.now_iso()
    job_id = _insert_job(
        conn,
        job_type="refresh",
        target_version=external_id,
        status="running",
        attempts=settings.retry_max_attempts - 1,
        claimed_at=claimed_at,
    )

    # Genuine real time passes -- enough to clear the staleness timeout --
    # then the OS steps CLOCK_REALTIME backward (NTP/hypervisor correction),
    # the exact hazard jobs.now()'s ratchet exists to absorb (lode-t1y).
    clock.tick(settings.stale_running_timeout_s + 60)
    clock.step_wall(-90)

    # This really is the bug's precondition: a raw, real-wall-clock fetched_at
    # (the old SQLite DEFAULT) would land strictly BEFORE claimed_at, since
    # claimed_at is anchored in 2030 and the real clock is nowhere near that.
    assert jobs.iso(datetime.now(UTC)) < claimed_at

    # The handler's real snapshot lands now -- genuinely after the claim in
    # elapsed real terms, despite the intervening backward step.
    ingest_snapshot(
        conn, external_id, SOURCE_TYPE_WEB, "the real, successfully-fetched body"
    )

    count = _reclaim_stale_running(conn, settings)
    assert count == 1
    assert _job(conn, job_id)["status"] == "dead"

    # Head must still be the real 'ok' snapshot -- NOT overwritten by the
    # reclaim's dead-letter tombstone.
    status, body = conn.execute(
        "SELECT s.status, s.body FROM snapshots s "
        "JOIN externals e ON e.head_snapshot_id = s.snapshot_id "
        "WHERE e.external_id = ?",
        (external_id,),
    ).fetchone()
    assert status == "ok"
    assert body == "the real, successfully-fetched body"
    (snapshot_count,) = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert snapshot_count == 1
