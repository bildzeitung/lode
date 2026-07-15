"""Tests for lode.reconcile — reconciliation scan (lode-i05.4).

Acceptance criteria (bd show lode-i05.4):

- A live head version with no fresh embed (no passages row for head_version_id)
  is re-enqueued by the scan.
- Running the scan repeatedly enqueues no duplicate jobs (idempotent against
  i05.6's live-job partial unique index).
- A soft-deleted head (op='delete') is NOT enqueued.
- A purged head (purged_at IS NOT NULL) is NOT enqueued.
- enqueue reuses the single i05.1 enqueue path (lode.jobs.enqueue_derive_jobs).

lode-621 extends the embed-gap acceptance to external snapshots (mirroring
live_head_versions' notes-UNION-externals shape):

- An external's current head_snapshot_id whose embed job reached 'dead' (or is
  altogether missing) is re-enqueued, exactly as a note's version would be.
- A tombstone snapshot (no body to embed) is NOT enqueued.
- A superseded (non-head) snapshot is NOT enqueued — only the current head.

Strategy: all tests use a real SQLite DB (via init_db) to exercise the actual
partial unique index and the ON CONFLICT DO NOTHING deduplication path.
The module-level _STEPS registry is not touched; tests inject custom step
lists into reconcile() where needed.
"""

import sqlite3
from pathlib import Path

import pytest

from lode.config import Settings
from lode.reconcile import _embed_gap_step, _refresh_stale_step, reconcile
from lode.storage import init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_note_with_version(
    conn: sqlite3.Connection,
    note_id: str = "note-1",
    version_id: str = "ver-1",
    op: str = "create",
    purged_at: str | None = None,
) -> None:
    """Insert a notes + versions row pair (minimal — no hashing needed).

    Insertion order matters: ``versions.note_id → notes`` is an immediate FK
    (not DEFERRABLE), so the notes row must exist before the versions row.
    ``notes.head_version_id → versions`` IS DEFERRABLE INITIALLY DEFERRED, so
    setting the head pointer after inserting the version row is fine within the
    same transaction.
    """
    with conn:
        # notes row first (head_version_id NULL initially — deferred FK).
        conn.execute("INSERT INTO notes (note_id) VALUES (?)", (note_id,))
        # versions row — FK to notes is satisfied now.
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op, purged_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (version_id, note_id, "body text", op, purged_at),
        )
        # Update head pointer — deferred FK checked at COMMIT.
        conn.execute(
            "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
            (version_id, note_id),
        )


def _insert_passage(
    conn: sqlite3.Connection,
    target_version: str = "ver-1",
    passage_id: str = "p-1",
) -> None:
    """Insert a minimal passages row.

    Note (lode-xyb): passages are now written synchronously on save by
    :class:`~lode.lexical.LexicalCacheBackend`, so their presence does NOT
    imply embedding is complete.  The embed-gap signal is the embed job status.
    This helper is kept for tests that set up passage state independently.
    """
    with conn:
        conn.execute(
            "INSERT INTO passages (passage_id, target_version, ord, text) "
            "VALUES (?, ?, ?, ?)",
            (passage_id, target_version, 0, "chunk text"),
        )


def _insert_embed_job(
    conn: sqlite3.Connection,
    target_version: str = "ver-1",
    status: str = "done",
) -> None:
    """Insert a minimal embed job row with the given status."""
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) VALUES ('embed', ?, ?)",
            (target_version, status),
        )


def _pending_embed_jobs(conn: sqlite3.Connection, version_id: str) -> list[str]:
    """Return statuses of embed jobs for version_id."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT status FROM jobs WHERE type = 'embed' AND target_version = ?",
            (version_id,),
        ).fetchall()
    ]


def _all_jobs_for_version(conn: sqlite3.Connection, version_id: str) -> list[tuple]:
    """Return all job rows (type, status) for version_id."""
    return conn.execute(
        "SELECT type, status FROM jobs WHERE target_version = ? ORDER BY type",
        (version_id,),
    ).fetchall()


def _insert_external_snapshot(
    conn: sqlite3.Connection,
    external_id: str = "ext-1",
    snapshot_id: str = "snap-1",
    *,
    status: str = "ok",
    is_head: bool = True,
    fetched_at: str | None = None,
) -> None:
    """Insert an external + one snapshot; point head at it iff ``is_head``.

    Externals/snapshots are UNUSED until connectors (schema), so tests seed the
    rows directly — mirrors ``tests/test_retrieval.py``'s
    ``_insert_external_snapshot`` helper.

    ``fetched_at`` (lode-w0h.6), if given, overrides the schema's ``now()``
    default — used by the ``refresh_stale`` step's tests to seed a snapshot
    that is already past (or well within) the TTL window.
    """
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
            (external_id,),
        )
        if fetched_at is not None:
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (snapshot_id, external_id, "body text", status, fetched_at),
            )
        else:
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
                "VALUES (?, ?, ?, ?)",
                (snapshot_id, external_id, "body text", status),
            )
        if is_head:
            conn.execute(
                "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
                (snapshot_id, external_id),
            )


def _insert_refresh_job(
    conn: sqlite3.Connection,
    target_version: str = "ext-1",
    status: str = "done",
) -> None:
    """Insert a minimal refresh job row with the given status (lode-w0h.6)."""
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) VALUES ('refresh', ?, ?)",
            (target_version, status),
        )


def _pending_refresh_jobs(conn: sqlite3.Connection, external_id: str) -> list[str]:
    """Return statuses of refresh jobs for external_id (lode-w0h.6)."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT status FROM jobs WHERE type = 'refresh' AND target_version = ?",
            (external_id,),
        ).fetchall()
    ]


def _old_timestamp(seconds_ago: int) -> str:
    """An ISO-8601 timestamp ``seconds_ago`` seconds in the past (lode-w0h.6)."""
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# _embed_gap_step — core gap detection and enqueue
# ---------------------------------------------------------------------------


def test_embed_gap_enqueues_embed_for_missing_job(
    conn: sqlite3.Connection,
) -> None:
    """A live head version with no embed job at all gets an embed job enqueued.

    The embed-gap signal (lode-xyb): a missing job (or all-dead jobs) means
    the vector leg has not run.  Passages may or may not exist — their presence
    is no longer the signal since they are written synchronously on save.
    """
    _insert_note_with_version(conn, "note-1", "ver-1")
    count = _embed_gap_step(conn)
    assert count == 1
    statuses = _pending_embed_jobs(conn, "ver-1")
    assert statuses == ["pending"]


def test_embed_gap_returns_zero_when_no_notes(conn: sqlite3.Connection) -> None:
    """An empty notes table produces a gap count of 0."""
    count = _embed_gap_step(conn)
    assert count == 0


def test_embed_gap_returns_zero_when_embed_job_done(conn: sqlite3.Connection) -> None:
    """A head version with a done embed job is not in the gap.

    Signal (lode-xyb): a non-dead embed job means the vector leg ran (or will run).
    A done job covers the version regardless of whether passages exist.
    """
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_embed_job(conn, "ver-1", status="done")
    count = _embed_gap_step(conn)
    assert count == 0
    # No additional embed job should be enqueued (done job covers the version).
    all_jobs = conn.execute(
        "SELECT status FROM jobs WHERE type = 'embed' AND target_version = 'ver-1'"
    ).fetchall()
    assert all_jobs == [("done",)]


def test_embed_gap_passages_alone_do_not_cover(conn: sqlite3.Connection) -> None:
    """Having passages but no embed job IS still a gap (lode-xyb).

    Before lode-xyb, passages were only written by the embed worker, so their
    presence signalled "embed ran."  After lode-xyb, passages are written
    synchronously on save — so passages exist before any embedding — and the
    gap signal is the embed job status, not passages.
    """
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_passage(conn, "ver-1", "p-1")
    # Passages exist, but no embed job → the vector leg has not run.
    count = _embed_gap_step(conn)
    assert count == 1
    assert _pending_embed_jobs(conn, "ver-1") == ["pending"]


def test_embed_gap_excludes_soft_deleted_head(conn: sqlite3.Connection) -> None:
    """A soft-deleted head (op='delete') must NOT be enqueued by the scan."""
    _insert_note_with_version(conn, "note-1", "ver-1", op="delete")
    count = _embed_gap_step(conn)
    assert count == 0
    assert _pending_embed_jobs(conn, "ver-1") == []


def test_embed_gap_excludes_purged_head(conn: sqlite3.Connection) -> None:
    """A purged head (purged_at IS NOT NULL) must NOT be enqueued."""
    _insert_note_with_version(
        conn, "note-1", "ver-1", purged_at="2026-01-01T00:00:00.000Z"
    )
    count = _embed_gap_step(conn)
    assert count == 0
    assert _pending_embed_jobs(conn, "ver-1") == []


def test_embed_gap_enqueues_only_embed_not_enrich(conn: sqlite3.Connection) -> None:
    """The embed-gap step enqueues embed only, not enrich (types=('embed',))."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _embed_gap_step(conn)
    jobs = _all_jobs_for_version(conn, "ver-1")
    # Only an embed job; no enrich job from this step.
    assert jobs == [("embed", "pending")]


def test_embed_gap_multiple_gap_versions(conn: sqlite3.Connection) -> None:
    """Multiple notes with no embed jobs all get an embed job enqueued."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_note_with_version(conn, "note-2", "ver-2")
    _insert_note_with_version(conn, "note-3", "ver-3")
    count = _embed_gap_step(conn)
    assert count == 3
    for ver in ("ver-1", "ver-2", "ver-3"):
        assert _pending_embed_jobs(conn, ver) == ["pending"]


def test_embed_gap_mixed_gap_and_covered(conn: sqlite3.Connection) -> None:
    """Only the version with no non-dead embed job appears in the gap.

    Signal (lode-xyb): "covered" means a non-dead embed job exists, not that
    passages exist.
    """
    _insert_note_with_version(conn, "note-1", "ver-1")  # gap: no embed job
    _insert_note_with_version(conn, "note-2", "ver-2")  # covered: done embed job
    _insert_embed_job(conn, "ver-2", status="done")
    count = _embed_gap_step(conn)
    assert count == 1
    assert _pending_embed_jobs(conn, "ver-1") == ["pending"]
    # ver-2's done job is not touched (no new pending job created).
    assert _pending_embed_jobs(conn, "ver-2") == ["done"]


# ---------------------------------------------------------------------------
# _embed_gap_step — snapshot arm (lode-621)
# ---------------------------------------------------------------------------


def test_embed_gap_enqueues_embed_for_dead_snapshot_job(
    conn: sqlite3.Connection,
) -> None:
    """A snapshot whose embed job reached 'dead' is re-enqueued (lode-621 AC).

    Mirrors the notes-arm gap signal: a dead (max-retries-exhausted) embed job
    counts the same as no job at all — the vector leg never completed.
    """
    _insert_external_snapshot(conn, "ext-1", "snap-1")
    _insert_embed_job(conn, "snap-1", status="dead")
    count = _embed_gap_step(conn)
    assert count == 1
    statuses = _pending_embed_jobs(conn, "snap-1")
    assert sorted(statuses) == ["dead", "pending"]


def test_embed_gap_enqueues_embed_for_snapshot_missing_job(
    conn: sqlite3.Connection,
) -> None:
    """A live external head snapshot with no embed job at all is a gap too."""
    _insert_external_snapshot(conn, "ext-1", "snap-1")
    count = _embed_gap_step(conn)
    assert count == 1
    assert _pending_embed_jobs(conn, "snap-1") == ["pending"]


def test_embed_gap_returns_zero_when_snapshot_embed_job_done(
    conn: sqlite3.Connection,
) -> None:
    """A head snapshot with a done embed job is not in the gap."""
    _insert_external_snapshot(conn, "ext-1", "snap-1")
    _insert_embed_job(conn, "snap-1", status="done")
    count = _embed_gap_step(conn)
    assert count == 0
    assert _pending_embed_jobs(conn, "snap-1") == ["done"]


def test_embed_gap_excludes_tombstone_snapshot(conn: sqlite3.Connection) -> None:
    """A tombstone snapshot (no body to embed) must NOT be swept.

    A tombstone has no real content — sweeping it would enqueue an embed job
    that can only fail, converting a silent gap into a retry loop (design
    constraint recorded on lode-621, inherited from lode-w0h.2's review note).
    """
    _insert_external_snapshot(conn, "ext-1", "snap-1", status="tombstone")
    count = _embed_gap_step(conn)
    assert count == 0
    assert _pending_embed_jobs(conn, "snap-1") == []


def test_embed_gap_excludes_superseded_snapshot(conn: sqlite3.Connection) -> None:
    """A superseded (non-head) snapshot must NOT be swept — only the current head.

    Matches what live_head_versions itself admits: only externals.head_snapshot_id
    is read, so a prior, now-stale snapshot is excluded by construction, the same
    way a note's non-head version is.
    """
    _insert_external_snapshot(conn, "ext-1", "snap-old", is_head=False)
    count = _embed_gap_step(conn)
    assert count == 0
    assert _pending_embed_jobs(conn, "snap-old") == []


def test_embed_gap_mixed_notes_and_snapshots(conn: sqlite3.Connection) -> None:
    """Both a note-version gap and a snapshot gap are found in one scan."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_external_snapshot(conn, "ext-1", "snap-1")
    count = _embed_gap_step(conn)
    assert count == 2
    assert _pending_embed_jobs(conn, "ver-1") == ["pending"]
    assert _pending_embed_jobs(conn, "snap-1") == ["pending"]


def test_embed_gap_snapshot_idempotent_repeated_calls(
    conn: sqlite3.Connection,
) -> None:
    """Running _embed_gap_step repeatedly must not duplicate snapshot jobs."""
    _insert_external_snapshot(conn, "ext-1", "snap-1")
    _embed_gap_step(conn)
    _embed_gap_step(conn)
    statuses = _pending_embed_jobs(conn, "snap-1")
    assert statuses == ["pending"]


def test_embed_gap_full_reconcile_heals_dead_snapshot_embed_job(
    conn: sqlite3.Connection,
) -> None:
    """End-to-end: reconcile() re-enqueues a dead snapshot embed job.

    Exercises the public reconcile() entrypoint (module-level _STEPS registry),
    not just the private step function — the shape of lode-621's acceptance
    criterion ("runs reconcile, and asserts a fresh embed job exists").
    """
    _insert_external_snapshot(conn, "ext-1", "snap-1")
    _insert_embed_job(conn, "snap-1", status="dead")
    total = reconcile(conn)
    assert total >= 1
    statuses = _pending_embed_jobs(conn, "snap-1")
    assert "pending" in statuses
    assert "dead" in statuses


# ---------------------------------------------------------------------------
# Idempotency — repeated scans produce no duplicates
# ---------------------------------------------------------------------------


def test_embed_gap_idempotent_repeated_calls(conn: sqlite3.Connection) -> None:
    """Running _embed_gap_step repeatedly must not create duplicate jobs."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _embed_gap_step(conn)
    _embed_gap_step(conn)  # second call: ON CONFLICT DO NOTHING
    statuses = _pending_embed_jobs(conn, "ver-1")
    assert statuses == ["pending"]  # still one row, not two


def test_embed_gap_no_gap_when_job_pending(conn: sqlite3.Connection) -> None:
    """A pending embed job (in-flight or just enqueued) means no gap."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_embed_job(conn, "ver-1", status="pending")
    count = _embed_gap_step(conn)
    assert count == 0  # pending job → not a gap
    # ON CONFLICT DO NOTHING would have prevented a duplicate anyway, but the
    # scan should not even detect a gap.
    all_embed = conn.execute(
        "SELECT status FROM jobs WHERE type = 'embed' AND target_version = 'ver-1'"
    ).fetchall()
    assert all_embed == [("pending",)]


def test_embed_gap_no_gap_when_job_running(conn: sqlite3.Connection) -> None:
    """A running embed job (in-flight) means no gap — do not duplicate."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_embed_job(conn, "ver-1", status="running")
    count = _embed_gap_step(conn)
    assert count == 0  # running job → not a gap
    all_embed = conn.execute(
        "SELECT status FROM jobs WHERE type = 'embed' AND target_version = 'ver-1'"
    ).fetchall()
    assert all_embed == [("running",)]


def test_embed_gap_no_gap_when_job_failed(conn: sqlite3.Connection) -> None:
    """A failed (but not dead) embed job means no gap — _reset_retryable will retry it."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_embed_job(conn, "ver-1", status="failed")
    count = _embed_gap_step(conn)
    assert count == 0  # failed job will be reset to pending by _reset_retryable


def test_embed_gap_reenqueues_when_all_jobs_dead(conn: sqlite3.Connection) -> None:
    """A dead-lettered embed job (max retries exhausted) is treated as a gap.

    Signal (lode-xyb): only 'dead' jobs mean the vector leg is stuck and needs
    a fresh re-enqueue.  A dead job is terminal — the worker won't retry it —
    so the reconcile scan must detect it as a gap and kick off a new job.
    """
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_embed_job(conn, "ver-1", status="dead")
    count = _embed_gap_step(conn)
    assert count == 1
    statuses = conn.execute(
        "SELECT status FROM jobs WHERE type = 'embed' AND target_version = 'ver-1'"
        " ORDER BY id"
    ).fetchall()
    # The dead job stays; a new pending job was added.
    assert ("dead",) in statuses
    assert ("pending",) in statuses


# ---------------------------------------------------------------------------
# _refresh_stale_step — TTL staleness detection + scheduling (lode-w0h.6)
# ---------------------------------------------------------------------------


def test_refresh_stale_enqueues_for_external_past_ttl(conn: sqlite3.Connection) -> None:
    """An external whose head snapshot is older than the TTL is re-enqueued."""
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200))
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 1
    assert _pending_refresh_jobs(conn, "ext-1") == ["pending"]


def test_refresh_stale_skips_external_within_ttl(conn: sqlite3.Connection) -> None:
    """A recently-fetched external (within the TTL) is not swept."""
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(10))
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 0
    assert _pending_refresh_jobs(conn, "ext-1") == []


def test_refresh_stale_returns_zero_when_no_externals(conn: sqlite3.Connection) -> None:
    """An empty externals table produces a gap count of 0."""
    assert _refresh_stale_step(conn) == 0


def test_refresh_stale_excludes_tombstone_head(conn: sqlite3.Connection) -> None:
    """A tombstoned head snapshot is NOT swept, even if well past the TTL.

    A tombstone means the source already failed permanently (or exhausted
    every retry and dead-lettered) — mirrors _embed_gap_step's own tombstone
    exclusion; blindly re-fetching it is not this step's job.
    """
    _insert_external_snapshot(
        conn,
        "ext-1",
        "snap-1",
        status="tombstone",
        fetched_at=_old_timestamp(7200),
    )
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 0
    assert _pending_refresh_jobs(conn, "ext-1") == []


def test_refresh_stale_excludes_superseded_snapshot(conn: sqlite3.Connection) -> None:
    """Only the current head's age matters — a superseded snapshot is not swept."""
    _insert_external_snapshot(
        conn,
        "ext-1",
        "snap-old",
        is_head=False,
        fetched_at=_old_timestamp(7200),
    )
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 0
    assert _pending_refresh_jobs(conn, "snap-old") == []


def test_refresh_stale_no_gap_when_refresh_job_pending(
    conn: sqlite3.Connection,
) -> None:
    """A pending refresh job (in-flight or just enqueued) means no gap."""
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200))
    _insert_refresh_job(conn, "ext-1", status="pending")
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 0
    assert _pending_refresh_jobs(conn, "ext-1") == ["pending"]


def test_refresh_stale_no_gap_when_refresh_job_running(
    conn: sqlite3.Connection,
) -> None:
    """A running refresh job means no gap — do not duplicate."""
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200))
    _insert_refresh_job(conn, "ext-1", status="running")
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 0
    assert _pending_refresh_jobs(conn, "ext-1") == ["running"]


def test_refresh_stale_reenqueues_when_prior_job_done(
    conn: sqlite3.Connection,
) -> None:
    """A prior 'done' refresh job does not block a fresh TTL-driven re-enqueue.

    Unlike embed/enrich, a refresh job carries no notion of "still current" —
    the external's own age (fetched_at) is the only staleness signal, so a
    done job from the last revalidation cycle must not permanently cover it.
    """
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200))
    _insert_refresh_job(conn, "ext-1", status="done")
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 1
    statuses = sorted(_pending_refresh_jobs(conn, "ext-1"))
    assert statuses == ["done", "pending"]


def test_refresh_stale_reenqueues_when_prior_job_dead(
    conn: sqlite3.Connection,
) -> None:
    """A dead-lettered refresh job from a past cycle does not block re-enqueue."""
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200))
    _insert_refresh_job(conn, "ext-1", status="dead")
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 1
    statuses = sorted(_pending_refresh_jobs(conn, "ext-1"))
    assert statuses == ["dead", "pending"]


def test_refresh_stale_multiple_stale_externals(conn: sqlite3.Connection) -> None:
    """Multiple stale externals all get a refresh job enqueued."""
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200))
    _insert_external_snapshot(conn, "ext-2", "snap-2", fetched_at=_old_timestamp(7200))
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 2
    assert _pending_refresh_jobs(conn, "ext-1") == ["pending"]
    assert _pending_refresh_jobs(conn, "ext-2") == ["pending"]


def test_refresh_stale_mixed_stale_and_fresh(conn: sqlite3.Connection) -> None:
    """Only the past-TTL external is in the gap; the fresh one is left alone."""
    _insert_external_snapshot(
        conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200)
    )  # stale
    _insert_external_snapshot(
        conn, "ext-2", "snap-2", fetched_at=_old_timestamp(10)
    )  # fresh
    settings = Settings(refresh_ttl_s=3600)
    count = _refresh_stale_step(conn, settings)
    assert count == 1
    assert _pending_refresh_jobs(conn, "ext-1") == ["pending"]
    assert _pending_refresh_jobs(conn, "ext-2") == []


def test_refresh_stale_idempotent_repeated_calls(conn: sqlite3.Connection) -> None:
    """Running _refresh_stale_step repeatedly must not duplicate jobs."""
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200))
    settings = Settings(refresh_ttl_s=3600)
    _refresh_stale_step(conn, settings)
    _refresh_stale_step(conn, settings)
    assert _pending_refresh_jobs(conn, "ext-1") == ["pending"]


def test_refresh_stale_uses_default_settings_when_none_given(
    conn: sqlite3.Connection,
) -> None:
    """With no settings arg, the step falls back to Settings()'s default TTL."""
    default_ttl = Settings().refresh_ttl_s
    _insert_external_snapshot(
        conn, "ext-1", "snap-1", fetched_at=_old_timestamp(default_ttl + 60)
    )
    count = _refresh_stale_step(conn)
    assert count == 1
    assert _pending_refresh_jobs(conn, "ext-1") == ["pending"]


def test_refresh_stale_registered_in_module_steps(conn: sqlite3.Connection) -> None:
    """refresh_stale is registered in the module-level _STEPS registry."""
    from lode.reconcile import _STEPS

    names = [name for name, _ in _STEPS]
    assert "refresh_stale" in names


def test_refresh_stale_full_reconcile_enqueues_stale_external(
    conn: sqlite3.Connection,
) -> None:
    """End-to-end: reconcile() with the refresh_stale step enqueues a stale refresh.

    Passes ``settings`` through ``reconcile()`` itself (rather than baking the
    override into the injected step, as a pre-lode-09n version of this test
    would have had to) — this is the actual threading path production code
    now uses too.
    """
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(7200))
    total = reconcile(
        conn,
        settings=Settings(refresh_ttl_s=3600),
        steps=[("refresh_stale", _refresh_stale_step)],
    )
    assert total == 1
    assert _pending_refresh_jobs(conn, "ext-1") == ["pending"]


def test_reconcile_threads_settings_to_refresh_stale_step(
    conn: sqlite3.Connection,
) -> None:
    """reconcile(conn, settings) threads a caller's refresh_ttl_s override into
    the module-registered refresh_stale step (lode-09n regression test).

    Before lode-09n, ``reconcile()`` invoked every step as ``step_fn(conn)``
    with no settings argument at all, so ``_refresh_stale_step`` always fell
    back to its own default ``Settings()`` — a caller-supplied override (e.g.
    from ``load_settings()``) never reached ``refresh_ttl_s``. This exercises
    the *default* ``_STEPS`` registry (not an injected step list) so it
    covers the exact path ``lode work`` (``cli.py``) drives.
    """
    # 120s old — well within Settings()'s (much larger) default TTL, but past
    # a short caller-supplied override, so this only shows up as a refresh gap
    # if the override actually reaches the step.
    _insert_external_snapshot(conn, "ext-1", "snap-1", fetched_at=_old_timestamp(120))
    assert Settings().refresh_ttl_s > 120  # sanity: default TTL would NOT flag this
    reconcile(conn, settings=Settings(refresh_ttl_s=60))
    # Assert on the refresh-job outcome specifically (not reconcile()'s total),
    # since the default registry's embed_gap step independently also flags
    # this same snapshot's missing embed job — an unrelated, expected gap.
    assert _pending_refresh_jobs(conn, "ext-1") == ["pending"]


# ---------------------------------------------------------------------------
# reconcile() — step orchestration
# ---------------------------------------------------------------------------


def test_reconcile_calls_all_steps(conn: sqlite3.Connection) -> None:
    """reconcile() invokes each step in the injected list, threading settings."""
    call_log: list[str] = []

    def _step_a(c: sqlite3.Connection, settings: Settings) -> int:
        call_log.append("a")
        return 2

    def _step_b(c: sqlite3.Connection, settings: Settings) -> int:
        call_log.append("b")
        return 3

    total = reconcile(conn, steps=[("a", _step_a), ("b", _step_b)])
    assert total == 5
    assert call_log == ["a", "b"]


def test_reconcile_passes_settings_instance_through_to_each_step(
    conn: sqlite3.Connection,
) -> None:
    """reconcile() threads the SAME caller-supplied Settings instance to every
    step positionally (lode-09n) — not a step-local default construction.

    Asserts with ``is`` (not ``==``): ``Settings`` is a pydantic model with
    *value* equality, so ``==`` would still pass if reconcile() rebuilt a copy
    per step. Two steps, so "every step" is actually exercised.
    """
    received: list[Settings] = []

    def _step(c: sqlite3.Connection, settings: Settings) -> int:
        received.append(settings)
        return 0

    custom = Settings(refresh_ttl_s=42)
    reconcile(conn, settings=custom, steps=[("a", _step), ("b", _step)])
    assert [s is custom for s in received] == [True, True]


def test_reconcile_defaults_settings_when_none_given(
    conn: sqlite3.Connection,
) -> None:
    """reconcile() with no settings arg falls back to a fresh Settings() default,
    same as :func:`lode.worker.drain`'s ``settings = settings or Settings()``.
    """
    received: list[Settings] = []

    def _step(c: sqlite3.Connection, settings: Settings) -> int:
        received.append(settings)
        return 0

    reconcile(conn, steps=[("step", _step)])
    assert received == [Settings()]


def test_reconcile_returns_zero_for_no_steps(conn: sqlite3.Connection) -> None:
    total = reconcile(conn, steps=[])
    assert total == 0


def test_reconcile_uses_module_level_steps_by_default(conn: sqlite3.Connection) -> None:
    """reconcile() with no ``steps`` arg uses _STEPS (embed_gap + enrich_gap registered)."""
    from lode.reconcile import _STEPS

    # Verify both steps are registered at module load.
    names = [name for name, _ in _STEPS]
    assert "embed_gap" in names
    assert "enrich_gap" in names  # added in lode-npx.1


def test_reconcile_embed_gap_end_to_end(conn: sqlite3.Connection) -> None:
    """End-to-end: reconcile() with the embed_gap step finds and enqueues the gap.

    Uses an injected step list so the assertion stays focused on the embed_gap
    step count regardless of how many steps the module-level _STEPS contains.
    """
    from lode.reconcile import _embed_gap_step

    _insert_note_with_version(conn, "note-1", "ver-1")
    total = reconcile(conn, steps=[("embed_gap", _embed_gap_step)])
    assert total == 1
    assert _pending_embed_jobs(conn, "ver-1") == ["pending"]


def test_reconcile_embed_gap_idempotent_via_reconcile(conn: sqlite3.Connection) -> None:
    """Calling reconcile() twice produces no duplicate jobs."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    reconcile(conn)
    reconcile(conn)
    statuses = _pending_embed_jobs(conn, "ver-1")
    assert statuses == ["pending"]  # still exactly one row


# ---------------------------------------------------------------------------
# Progress instrumentation (lode-olmi.15)
# ---------------------------------------------------------------------------


def test_reconcile_logs_progress_line_per_step(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Each step call is wrapped in op_progress -- a 'starting' line names it.

    Before lode-olmi.15, reconcile() logged nothing while a step was running
    (only a gap-count summary afterward, and only if it found a gap) -- a
    plain 'lode work' had no sign a step was even in progress.
    """

    def _step_a(c: sqlite3.Connection, settings: Settings) -> int:
        return 0

    def _step_b(c: sqlite3.Connection, settings: Settings) -> int:
        return 0

    with caplog.at_level("INFO"):
        reconcile(conn, steps=[("step_a", _step_a), ("step_b", _step_b)])

    assert "reconcile.step_a: starting" in caplog.text
    assert "reconcile.step_a: done" in caplog.text
    assert "reconcile.step_b: starting" in caplog.text
    assert "reconcile.step_b: done" in caplog.text


def test_reconcile_progress_heartbeats_a_slow_step(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A step slower than progress_heartbeat_interval_s gets a heartbeat line.

    Uses a tiny heartbeat interval + a short sleep in the step so this stays
    fast without any fake-clock plumbing through the threading-based
    op_progress heartbeat.
    """
    import time

    def _slow_step(c: sqlite3.Connection, settings: Settings) -> int:
        time.sleep(0.2)
        return 0

    settings = Settings(progress_heartbeat_interval_s=0.05)
    with caplog.at_level("INFO"):
        reconcile(conn, settings=settings, steps=[("slow", _slow_step)])

    assert "reconcile.slow: starting" in caplog.text
    assert "reconcile.slow: still running" in caplog.text
    assert "reconcile.slow: done" in caplog.text


def test_reconcile_progress_logs_failed_and_reraises(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising step logs a 'failed' progress line and the exception propagates."""

    def _boom(c: sqlite3.Connection, settings: Settings) -> int:
        raise RuntimeError("boom")

    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="boom"):
        reconcile(conn, steps=[("boom", _boom)])

    assert "reconcile.boom: starting" in caplog.text
    assert "reconcile.boom: failed" in caplog.text
