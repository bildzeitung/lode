"""Tests for lode.jobs — the derive-job enqueue seam (lode-y42.1, lode-i05.1, lode-i05.6).

Covers: a capture enqueues exactly the embed + enrich derive jobs as pending
rows targeting the version (schema defaults applied, prompt_ver left NULL); the
enqueue runs on the caller's connection inside the caller's transaction (lode-i05.1
— no longer its own transaction); and idempotency constraints from lode-i05.6 —
duplicate enqueue of a live (pending/running) job is a no-op (ON CONFLICT DO
NOTHING against idx_jobs_live), while re-enqueue after done/dead IS allowed.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from lode.config import Settings
from lode.jobs import (
    DERIVE_JOB_TYPES,
    cas_update_running,
    enqueue_derive_jobs,
    iso,
    next_failure_state,
    now,
    now_iso,
    record_job_failure,
)
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


def test_enqueue_runs_within_callers_txn(tmp_path: Path) -> None:
    """enqueue_derive_jobs is a plain INSERT — the CALLER commits (lode-i05.1).

    The function runs on the caller's open connection without opening or
    committing its own transaction. The rows are visible to a separate reader
    only after the caller issues the commit (via ``with conn:``).
    """
    db_path = tmp_path / "lode.db"
    writer = init_db(db_path)
    try:
        with writer:
            enqueue_derive_jobs(writer, "ver-1")
    finally:
        writer.close()
    # A separate connection sees the rows -> the caller's txn committed.
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


@pytest.mark.parametrize("terminal_status", ["done", "dead"])
def test_reenqueue_after_terminal_status_is_allowed(conn, terminal_status: str) -> None:
    """After a job reaches a terminal status, the same (type, version) can be
    re-enqueued.

    The partial index is scoped to pending/running only; 'done' and 'dead' rows
    fall outside the WHERE clause and do not block a new enqueue. 'dead' is the
    poison terminal at max-attempts (lode-i05.6).
    """
    enqueue_derive_jobs(conn, "ver-1")
    # Simulate reaching the terminal status: move both jobs there.
    with conn:
        conn.execute(
            "UPDATE jobs SET status = ? WHERE target_version = 'ver-1'",
            (terminal_status,),
        )
    # Re-enqueue should succeed (not be silently ignored).
    enqueue_derive_jobs(conn, "ver-1")
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE target_version = 'ver-1' AND status = 'pending'"
    ).fetchone()
    assert n == len(DERIVE_JOB_TYPES)  # fresh pending rows exist


def test_dead_status_accepted_by_schema(conn) -> None:
    """The status CHECK must accept 'dead' (added in lode-i05.6)."""
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
            "VALUES (?, ?, ?, ?)",
            ("embed", "ver-dead", "dead", now_iso()),
        )
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE target_version = 'ver-dead'"
    ).fetchone()
    assert status == "dead"


# --- record_job_failure (lode-ajda) ------------------------------------------
#
# The shared attempts/backoff/dead-letter transition used by both
# lode.worker.run_one (a transient handler failure) and
# lode.enrich._mark_job_failed (an errored/expired/canceled Batches API
# result) — previously two independent, drifting copies of this same logic.


def _insert_running_job(conn, *, claimed_at: str | None = None) -> int:
    # next_attempt_at is irrelevant to every caller here -- record_job_failure
    # always overwrites it as part of the failure transition -- so any valid
    # value satisfies the NOT NULL column (lode-uk1i dropped the schema DEFAULT
    # that used to supply one).
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (type, target_version, status, claimed_at, next_attempt_at) "
            "VALUES ('embed', 'ver-1', 'running', ?, ?)",
            (claimed_at, now_iso()),
        )
    return cur.lastrowid


def test_record_job_failure_applies_backoff_below_max_attempts(conn) -> None:
    settings = Settings(retry_max_attempts=5)
    # Anchor BEFORE the insert: _insert_running_job stamps next_attempt_at with
    # an arbitrary valid value (~this instant) that record_job_failure's failure
    # transition always overwrites. Asserting merely "not NULL", or "> before",
    # would therefore pass even if record_job_failure stamped nothing new at
    # all. The backoff is only proven by requiring the value to sit a FULL
    # retry_backoff_base_s ahead of this anchor, which the row's initial value
    # cannot satisfy on its own.
    before = now()
    job_id = _insert_running_job(conn)

    new_attempts, dead, claim_lost = record_job_failure(
        conn, job_id, 0, None, "boom", settings
    )

    assert (new_attempts, dead, claim_lost) == (1, False, False)
    row = conn.execute(
        "SELECT status, attempts, last_error, next_attempt_at FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] == 1
    assert row[2] == "boom"
    # First failure -> min(base * 2**0, cap) == base seconds out. now() never
    # decreases, so the stamp is >= before + base exactly; a >= compare is tight
    # and cannot flake on a slow machine (elapsed time only pushes it further out).
    earliest = iso(before + timedelta(seconds=settings.retry_backoff_base_s))
    assert row[3] >= earliest


def test_record_job_failure_dead_letters_at_max_attempts(conn) -> None:
    job_id = _insert_running_job(conn)
    settings = Settings(retry_max_attempts=2)

    new_attempts, dead, claim_lost = record_job_failure(
        conn, job_id, 1, None, "boom", settings
    )

    assert (new_attempts, dead, claim_lost) == (2, True, False)
    row = conn.execute(
        "SELECT status, attempts, last_error FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row[0] == "dead"
    assert row[1] == 2
    assert row[2] == "boom"


# --- next_failure_state (lode-yb9t) ------------------------------------------
#
# The pure policy decision factored out of record_job_failure so that
# worker._reclaim_stale_running (which cannot call record_job_failure itself —
# see its docstring) shares this decision instead of duplicating it. No conn,
# no SQL, no txn — record_job_failure's persistence is exercised by the tests
# above; these test the decision in isolation.


def test_next_failure_state_below_max_attempts_applies_backoff() -> None:
    settings = Settings(retry_max_attempts=5)
    before = now()

    new_attempts, dead, next_at = next_failure_state(0, settings)

    assert (new_attempts, dead) == (1, False)
    assert next_at is not None
    # Same tight-backoff assertion style as
    # test_record_job_failure_applies_backoff_below_max_attempts: >= a full
    # retry_backoff_base_s ahead of a pre-call anchor.
    earliest = iso(before + timedelta(seconds=settings.retry_backoff_base_s))
    assert next_at >= earliest


def test_next_failure_state_dead_letters_on_the_last_attempt_not_one_past_it() -> None:
    """Pin the exact gate boundary with LITERAL expectations: the failure that
    brings the count TO retry_max_attempts dead-letters (and schedules no further
    attempt); the one before it does not. Stated as literals on purpose -- an
    expectation computed by calling next_failure_state would move with the policy
    and pin nothing.
    """
    settings = Settings(retry_max_attempts=3)

    # One below the gate: retry, with a backoff stamped.
    new_attempts, dead, next_at = next_failure_state(1, settings)
    assert (new_attempts, dead) == (2, False)  # 2 < 3 -> retry
    assert next_at is not None

    # At the gate: dead, and no next attempt is scheduled.
    assert next_failure_state(2, settings) == (3, True, None)  # 3 >= 3 -> dead


# --- record_job_failure CAS guard (lode-3jte) --------------------------------
#
# Neither UPDATE may take effect if the row is no longer 'running' by the time
# this runs -- a concurrent _reclaim_stale_running may have already reclaimed
# it (e.g. to a terminal 'dead') while the caller's handler was still in
# flight. Mirrors the same guard run_one's `except AuthError` arm already has
# (lode-9yy) for exactly this race.


def test_record_job_failure_is_a_noop_when_claim_already_lost_below_max(conn) -> None:
    """If the row moved off 'running' before this call, the 'failed' UPDATE
    must not apply -- reports claim_lost=True and leaves the row untouched."""
    job_id = _insert_running_job(conn)
    settings = Settings(retry_max_attempts=5)
    # Simulate a concurrent reclaim that already terminalized the row.
    with conn:
        conn.execute(
            "UPDATE jobs SET status = 'dead', attempts = 9, last_error = 'reclaimed' "
            "WHERE id = ?",
            (job_id,),
        )

    new_attempts, dead, claim_lost = record_job_failure(
        conn, job_id, 0, None, "boom", settings
    )

    assert claim_lost is True
    assert new_attempts == 1  # what WOULD have been applied -- not what's on the row
    assert dead is False
    row = conn.execute(
        "SELECT status, attempts, last_error FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    # Untouched by this call -- still exactly what the simulated reclaim left.
    assert row == ("dead", 9, "reclaimed")


def test_record_job_failure_is_a_noop_when_claim_already_lost_at_max(conn) -> None:
    """Same guard on the dead-lettering branch (new_attempts >= max)."""
    job_id = _insert_running_job(conn)
    settings = Settings(retry_max_attempts=2)
    with conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', attempts = 9, last_error = 'reclaimed' "
            "WHERE id = ?",
            (job_id,),
        )

    new_attempts, dead, claim_lost = record_job_failure(
        conn, job_id, 1, None, "boom", settings
    )

    assert claim_lost is True
    assert new_attempts == 2
    assert dead is True  # what WOULD have been applied
    row = conn.execute(
        "SELECT status, attempts, last_error FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row == ("failed", 9, "reclaimed")


# --- record_job_failure ABA guard (lode-nggm hole 2) -------------------------
#
# A status-only CAS guard cannot tell a STALE claim from a NEWER one: the row
# can cycle running -> failed -> pending -> running (a different claimed_at,
# possibly a different worker) inside one stall that already exceeded
# stale_running_timeout_s, entirely before the ORIGINAL stalled caller's write
# finally lands. Guarding on claimed_at too closes this -- the guard must see
# the row is not just 'running', but *this exact* running claim.


def test_record_job_failure_is_a_noop_when_the_row_cycled_to_a_new_claim(conn) -> None:
    """status='running' matches, but claimed_at does not -- the row already
    moved on to a DIFFERENT claim (failed -> pending -> re-claimed) since this
    caller first read its own claimed_at. A status-only guard would wrongly
    match this and clobber the new claimant; the claimed_at guard must not."""
    stale_claimed_at = "2026-07-13T10:00:00.000Z"
    job_id = _insert_running_job(conn, claimed_at=stale_claimed_at)
    settings = Settings(retry_max_attempts=5)
    # Simulate the full cycle a concurrent reclaim + re-claim already ran
    # through while this caller's handler was still stalled: reclaimed to
    # 'failed', reset to 'pending', then re-claimed to 'running' again with a
    # FRESH claimed_at -- status is 'running' again, exactly what a
    # status-only guard would accept.
    with conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = 9, "
            "last_error = NULL, claimed_at = '2026-07-13T11:00:00.000Z' "
            "WHERE id = ?",
            (job_id,),
        )

    _, _, claim_lost = record_job_failure(
        conn, job_id, 0, stale_claimed_at, "boom", settings
    )

    assert claim_lost is True
    row = conn.execute(
        "SELECT status, attempts, claimed_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    # Untouched -- still the NEW claimant's row, not clobbered by the stale write.
    assert row == ("running", 9, "2026-07-13T11:00:00.000Z")


def test_record_job_failure_applies_when_claimed_at_still_matches(conn) -> None:
    """The claim identity check is additive, not stricter than necessary --
    a caller whose claimed_at still matches the live row succeeds exactly as
    before."""
    claimed_at = "2026-07-13T10:00:00.000Z"
    job_id = _insert_running_job(conn, claimed_at=claimed_at)
    settings = Settings(retry_max_attempts=5)

    new_attempts, dead, claim_lost = record_job_failure(
        conn, job_id, 0, claimed_at, "boom", settings
    )

    assert claim_lost is False
    assert (new_attempts, dead) == (1, False)
    row = conn.execute(
        "SELECT status, attempts FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row == ("failed", 1)


# --- cas_update_running (lode-nggm hole 3) -----------------------------------
#
# The shared primitive itself, in isolation from record_job_failure's
# attempts/backoff policy -- exercises the NULL-safe claimed_at comparison
# directly (a 'running' row that predates the column, or one a migration never
# stamped, reads back NULL, and SQL's plain `= NULL` never matches).


def test_cas_update_running_matches_a_null_claimed_at(conn) -> None:
    job_id = _insert_running_job(conn, claimed_at=None)

    matched = cas_update_running(
        conn, job_id, None, "status = 'failed', last_error = ?", ("boom",)
    )

    assert matched is True
    row = conn.execute(
        "SELECT status, last_error FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row == ("failed", "boom")


def test_cas_update_running_rejects_a_mismatched_claimed_at(conn) -> None:
    job_id = _insert_running_job(conn, claimed_at="2026-07-13T10:00:00.000Z")

    matched = cas_update_running(
        conn,
        job_id,
        "2026-07-13T09:00:00.000Z",
        "status = 'failed', last_error = ?",
        ("boom",),
    )

    assert matched is False
    row = conn.execute(
        "SELECT status, last_error FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row == ("running", None)  # untouched


def test_cas_update_running_rejects_when_status_is_not_running(conn) -> None:
    job_id = _insert_running_job(conn, claimed_at="2026-07-13T10:00:00.000Z")
    with conn:
        conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))

    matched = cas_update_running(
        conn,
        job_id,
        "2026-07-13T10:00:00.000Z",
        "status = 'failed', last_error = ?",
        ("boom",),
    )

    assert matched is False
    row = conn.execute(
        "SELECT status, last_error FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    # Untouched — claimed_at matches, so it is the status half of the guard
    # that held here. Without this, the test would pass on the return value
    # alone even if the UPDATE had written the row.
    assert row == ("done", None)
