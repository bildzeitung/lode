"""Tests for lode.jobs — the derive-job enqueue seam (lode-y42.1).

Covers: a capture enqueues exactly the embed + enrich derive jobs as pending
rows targeting the version (schema defaults applied, prompt_ver left NULL), and
the enqueue is its own transaction (committed when it returns).
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
