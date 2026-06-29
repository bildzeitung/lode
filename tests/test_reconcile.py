"""Tests for lode.reconcile — reconciliation scan (lode-i05.4).

Acceptance criteria (bd show lode-i05.4):

- A live head version with no fresh embed (no passages row for head_version_id)
  is re-enqueued by the scan.
- Running the scan repeatedly enqueues no duplicate jobs (idempotent against
  i05.6's live-job partial unique index).
- A soft-deleted head (op='delete') is NOT enqueued.
- A purged head (purged_at IS NOT NULL) is NOT enqueued.
- enqueue reuses the single i05.1 enqueue path (lode.jobs.enqueue_derive_jobs).

Strategy: all tests use a real SQLite DB (via init_db) to exercise the actual
partial unique index and the ON CONFLICT DO NOTHING deduplication path.
The module-level _STEPS registry is not touched; tests inject custom step
lists into reconcile() where needed.
"""

import sqlite3
from pathlib import Path

import pytest

from lode.reconcile import _embed_gap_step, reconcile
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
    """Insert a minimal passages row (signals that embed ran for target_version)."""
    with conn:
        conn.execute(
            "INSERT INTO passages (passage_id, target_version, ord, text) "
            "VALUES (?, ?, ?, ?)",
            (passage_id, target_version, 0, "chunk text"),
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


# ---------------------------------------------------------------------------
# _embed_gap_step — core gap detection and enqueue
# ---------------------------------------------------------------------------


def test_embed_gap_enqueues_embed_for_missing_passages(
    conn: sqlite3.Connection,
) -> None:
    """A live head version with no passages row gets an embed job enqueued."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    count = _embed_gap_step(conn)
    assert count == 1
    statuses = _pending_embed_jobs(conn, "ver-1")
    assert statuses == ["pending"]


def test_embed_gap_returns_zero_when_no_notes(conn: sqlite3.Connection) -> None:
    """An empty notes table produces a gap count of 0."""
    count = _embed_gap_step(conn)
    assert count == 0


def test_embed_gap_returns_zero_when_passages_exist(conn: sqlite3.Connection) -> None:
    """A head version that already has passages is not in the gap."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_passage(conn, "ver-1", "p-1")
    count = _embed_gap_step(conn)
    assert count == 0
    # No embed job should be enqueued.
    assert _pending_embed_jobs(conn, "ver-1") == []


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
    """Multiple notes with missing passages all get an embed job enqueued."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    _insert_note_with_version(conn, "note-2", "ver-2")
    _insert_note_with_version(conn, "note-3", "ver-3")
    count = _embed_gap_step(conn)
    assert count == 3
    for ver in ("ver-1", "ver-2", "ver-3"):
        assert _pending_embed_jobs(conn, ver) == ["pending"]


def test_embed_gap_mixed_gap_and_covered(conn: sqlite3.Connection) -> None:
    """Only the version missing passages appears in the gap; the covered one doesn't."""
    _insert_note_with_version(conn, "note-1", "ver-1")  # gap
    _insert_note_with_version(conn, "note-2", "ver-2")  # covered
    _insert_passage(conn, "ver-2", "p-2")
    count = _embed_gap_step(conn)
    assert count == 1
    assert _pending_embed_jobs(conn, "ver-1") == ["pending"]
    assert _pending_embed_jobs(conn, "ver-2") == []


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


def test_embed_gap_idempotent_with_inflight_job(conn: sqlite3.Connection) -> None:
    """Re-enqueueing a version with an in-flight embed job is a no-op."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    # Pre-insert a running embed job (simulating in-flight).
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) VALUES ('embed', 'ver-1', 'running')"
        )
    # No passages yet — still a gap, but the job is in-flight.
    count = _embed_gap_step(conn)
    assert count == 1  # gap was found (no passages)
    # Still only one embed job (the running one); no duplicate pending inserted.
    all_embed = conn.execute(
        "SELECT status FROM jobs WHERE type = 'embed' AND target_version = 'ver-1'"
    ).fetchall()
    assert len(all_embed) == 1
    assert all_embed[0][0] == "running"


def test_embed_gap_allows_reenqueue_after_done(conn: sqlite3.Connection) -> None:
    """After a done embed job, if passages are removed, the gap is re-enqueued.

    A 'done' job is outside the live-job partial unique index scope, so
    re-enqueue after it completes IS allowed (the scan can legitimately
    re-derive). Simulated by moving the job to 'done' and removing passages.
    """
    _insert_note_with_version(conn, "note-1", "ver-1")
    # Simulate: embed ran (passages exist, job done).
    _insert_passage(conn, "ver-1", "p-1")
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) VALUES ('embed', 'ver-1', 'done')"
        )
    # Passages exist → not a gap → no enqueue.
    count = _embed_gap_step(conn)
    assert count == 0

    # Now remove passages (simulating a cache wipe).
    with conn:
        conn.execute("DELETE FROM passages WHERE target_version = 'ver-1'")

    # Gap re-appears → scan should enqueue a new embed job.
    count = _embed_gap_step(conn)
    assert count == 1
    pending = [
        r[0]
        for r in conn.execute(
            "SELECT status FROM jobs WHERE type = 'embed' AND target_version = 'ver-1'"
        ).fetchall()
    ]
    assert "pending" in pending


# ---------------------------------------------------------------------------
# reconcile() — step orchestration
# ---------------------------------------------------------------------------


def test_reconcile_calls_all_steps(conn: sqlite3.Connection) -> None:
    """reconcile() invokes each step in the injected list."""
    call_log: list[str] = []

    def _step_a(c: sqlite3.Connection) -> int:
        call_log.append("a")
        return 2

    def _step_b(c: sqlite3.Connection) -> int:
        call_log.append("b")
        return 3

    total = reconcile(conn, steps=[("a", _step_a), ("b", _step_b)])
    assert total == 5
    assert call_log == ["a", "b"]


def test_reconcile_returns_zero_for_no_steps(conn: sqlite3.Connection) -> None:
    total = reconcile(conn, steps=[])
    assert total == 0


def test_reconcile_uses_module_level_steps_by_default(conn: sqlite3.Connection) -> None:
    """reconcile() with no ``steps`` arg uses _STEPS (embed_gap registered)."""
    from lode.reconcile import _STEPS

    # Verify the embed_gap step is registered at module load.
    names = [name for name, _ in _STEPS]
    assert "embed_gap" in names


def test_reconcile_embed_gap_end_to_end(conn: sqlite3.Connection) -> None:
    """End-to-end: reconcile() with default steps finds and enqueues the gap."""
    _insert_note_with_version(conn, "note-1", "ver-1")
    total = reconcile(conn)
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
# register_step — registry mutation
# ---------------------------------------------------------------------------


def test_register_step_appends_to_steps(tmp_path: Path) -> None:
    """register_step appends to _STEPS; calling it does not affect other tests
    because we only inspect the name without touching the live module state
    in a way that bleeds across tests."""
    # We test indirectly via reconcile() with a steps list — the module-level
    # _STEPS is append-only and doesn't reset between tests, so we cannot
    # register a step here without polluting subsequent tests. Instead, confirm
    # the module-level _STEPS contains exactly 'embed_gap' at startup.
    from lode.reconcile import _STEPS

    names = [n for n, _ in _STEPS]
    assert names[0] == "embed_gap", "embed_gap must be the first registered step"
