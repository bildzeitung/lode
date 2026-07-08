"""Tests for lode.externals — the mirrored-snapshot write path (lode-w0h.2).

Covers the acceptance criteria: a fetched page becomes one ``externals`` row +
one ``snapshots`` row; an identical refetch adds no row; a changed body adds a
new snapshot and moves ``head_snapshot_id``; a fetch failure writes a
tombstone snapshot (not scaffolding); and an ``embed`` job (never ``enrich``)
is enqueued on every non-deduped ingest.
"""

from pathlib import Path

import pytest

from lode.hashing import content_snapshot_id
from lode.externals import (
    IngestResult,
    ingest_fetch_result,
    ingest_snapshot,
    tombstone_body,
)
from lode.storage import init_db
from lode.webfetch import FetchResult, FetchStatus

_EXTERNAL_ID = "https://example.com/article"


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


def _external_row(conn, external_id: str):
    return conn.execute(
        "SELECT source_type, head_snapshot_id FROM externals WHERE external_id = ?",
        (external_id,),
    ).fetchone()


def _count_snapshots(conn, external_id: str) -> int:
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    return n


def _jobs_for(conn, target_version: str):
    return conn.execute(
        "SELECT type, status FROM jobs WHERE target_version = ? ORDER BY type",
        (target_version,),
    ).fetchall()


# --- fresh ingest -------------------------------------------------------------


def test_fresh_ingest_writes_one_external_and_one_snapshot(conn) -> None:
    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    assert not result.deduped
    assert result.status == "ok"
    assert result.snapshot_id == content_snapshot_id(_EXTERNAL_ID, "hello world")
    assert _external_row(conn, _EXTERNAL_ID) == ("web", result.snapshot_id)
    assert _count_snapshots(conn, _EXTERNAL_ID) == 1
    (body, raw_payload, status) = conn.execute(
        "SELECT body, raw_payload, status FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert (body, raw_payload, status) == ("hello world", None, "ok")


def test_fresh_ingest_enqueues_embed_only_not_enrich(conn) -> None:
    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    assert _jobs_for(conn, result.snapshot_id) == [("embed", "pending")]


def test_raw_payload_is_stored_alongside_body(conn) -> None:
    result = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", "hello world", raw_payload="<html>hello world</html>"
    )

    (raw_payload,) = conn.execute(
        "SELECT raw_payload FROM snapshots WHERE snapshot_id = ?", (result.snapshot_id,)
    ).fetchone()
    assert raw_payload == "<html>hello world</html>"


# --- dedup on identical refetch -----------------------------------------------


def test_identical_refetch_adds_no_row_and_no_new_job(conn) -> None:
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")
    second = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    assert second.deduped
    assert second.snapshot_id == first.snapshot_id
    assert _count_snapshots(conn, _EXTERNAL_ID) == 1
    # No second embed job was enqueued for the (unchanged) snapshot id.
    assert _jobs_for(conn, first.snapshot_id) == [("embed", "pending")]


def test_dedup_does_not_touch_existing_externals_row(conn) -> None:
    ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")
    (n_before,) = conn.execute("SELECT COUNT(*) FROM externals").fetchone()

    ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    (n_after,) = conn.execute("SELECT COUNT(*) FROM externals").fetchone()
    assert n_before == n_after == 1


# --- changed body moves the head ----------------------------------------------


def test_changed_body_adds_a_new_snapshot_and_moves_head(conn) -> None:
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version one")
    second = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version two")

    assert not second.deduped
    assert second.snapshot_id != first.snapshot_id
    assert _count_snapshots(conn, _EXTERNAL_ID) == 2
    assert _external_row(conn, _EXTERNAL_ID) == ("web", second.snapshot_id)
    # Both snapshots enqueued their own embed job.
    assert _jobs_for(conn, first.snapshot_id) == [("embed", "pending")]
    assert _jobs_for(conn, second.snapshot_id) == [("embed", "pending")]


def test_revert_to_a_prior_body_reproduces_the_prior_snapshot_id(conn) -> None:
    """Content addressing, not history: A -> B -> A re-heads onto A's original id."""
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version A")
    ingest_snapshot(conn, _EXTERNAL_ID, "web", "version B")
    third = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version A")

    assert third.snapshot_id == first.snapshot_id
    assert not third.deduped  # head had moved to B, so this IS a head-move
    assert _count_snapshots(conn, _EXTERNAL_ID) == 2  # no 3rd row for a repeat body
    assert _external_row(conn, _EXTERNAL_ID) == ("web", first.snapshot_id)


# --- tombstone on fetch failure ------------------------------------------------


def test_fetch_failure_writes_a_tombstone_snapshot_not_garbage(conn) -> None:
    result = ingest_snapshot(
        conn,
        _EXTERNAL_ID,
        "web",
        tombstone_body("http_403"),
        status="tombstone",
    )

    assert result.status == "tombstone"
    (body, status) = conn.execute(
        "SELECT body, status FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert body == "[tombstone: http_403]"
    assert status == "tombstone"


def test_repeated_identical_tombstone_reason_dedups(conn) -> None:
    """A persistently-dead source doesn't spam a new row on every retry."""
    first = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_404"), status="tombstone"
    )
    second = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_404"), status="tombstone"
    )

    assert second.deduped
    assert second.snapshot_id == first.snapshot_id
    assert _count_snapshots(conn, _EXTERNAL_ID) == 1


def test_tombstone_after_ok_moves_head_and_enqueues_embed(conn) -> None:
    ok_result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "real content")
    tomb_result = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_410"), status="tombstone"
    )

    assert tomb_result.snapshot_id != ok_result.snapshot_id
    assert _external_row(conn, _EXTERNAL_ID) == ("web", tomb_result.snapshot_id)
    assert _jobs_for(conn, tomb_result.snapshot_id) == [("embed", "pending")]


# --- ingest_fetch_result adapter (webfetch.FetchResult -> ingest_snapshot) ---


def test_ingest_fetch_result_ok_uses_clean_text_and_raw_html(conn) -> None:
    fetch = FetchResult(
        status=FetchStatus.OK,
        final_url=_EXTERNAL_ID,
        clean_text="A Real Article body.",
        raw_html="<html>A Real Article body.</html>",
        http_status=200,
        tombstone_reason=None,
    )

    result = ingest_fetch_result(conn, _EXTERNAL_ID, "web", fetch)

    assert result.status == "ok"
    (body, raw_payload) = conn.execute(
        "SELECT body, raw_payload FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert body == "A Real Article body."
    assert raw_payload == "<html>A Real Article body.</html>"


def test_ingest_fetch_result_tombstone_uses_tombstone_reason(conn) -> None:
    fetch = FetchResult(
        status=FetchStatus.TOMBSTONE,
        final_url=_EXTERNAL_ID,
        clean_text=None,
        raw_html="<html>nope</html>",
        http_status=403,
        tombstone_reason="http_403",
    )

    result = ingest_fetch_result(conn, _EXTERNAL_ID, "web", fetch)

    assert result.status == "tombstone"
    (body, raw_payload) = conn.execute(
        "SELECT body, raw_payload FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert body == "[tombstone: http_403]"
    assert raw_payload == "<html>nope</html>"


def test_ingest_fetch_result_redirect_cap_tombstone_has_no_raw_payload(conn) -> None:
    """A too-many-redirects tombstone carries neither clean_text nor raw_html."""
    fetch = FetchResult(
        status=FetchStatus.TOMBSTONE,
        final_url=_EXTERNAL_ID,
        clean_text=None,
        raw_html=None,
        http_status=None,
        tombstone_reason="too_many_redirects",
    )

    result = ingest_fetch_result(conn, _EXTERNAL_ID, "web", fetch)

    (body, raw_payload) = conn.execute(
        "SELECT body, raw_payload FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert body == "[tombstone: too_many_redirects]"
    assert raw_payload is None


# --- IngestResult shape --------------------------------------------------------


def test_ingest_result_is_frozen_dataclass_shape() -> None:
    result = IngestResult("ext-1", "snap-1", "ok", deduped=True)
    assert result.external_id == "ext-1"
    assert result.snapshot_id == "snap-1"
    assert result.status == "ok"
    assert result.deduped is True
