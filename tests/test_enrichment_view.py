"""Tests for lode.enrichment_view -- the shared TUI+CLI enrichment seam (lode-ay5.1).

Acceptance criteria (bd show lode-ay5.1):
- A documented reader returns the assembled enrichment view for a note_id, reusing
  lode.display verbatim (no second copy of classify_*), with a THREE-valued
  enrichment_state {pending, ready, failed} keyed on the head.
- Covered by unit tests over a seeded db including: enriched (ready) note,
  un-enriched (pending) note, dead-lettered (failed) note, a re-enriching note that
  reports state=pending WITH stale content shown, a note with stale items, and a
  tombstoned edge case.

Strategy: a seeded SQLite db (init_db) with hand-inserted notes/versions/annotations/
edges/jobs rows, mirroring the helper conventions in tests/test_enrich.py.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from lode.enrichment_view import EnrichmentEdge, enrichment_view
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
# Helpers -- mirrors tests/test_enrich.py's seeding conventions
# ---------------------------------------------------------------------------


def _insert_note(
    conn: sqlite3.Connection,
    *,
    note_id: str = "note-1",
    version_id: str = "ver-1",
    body: str = "A test note.",
) -> None:
    with conn:
        conn.execute("INSERT INTO notes (note_id) VALUES (?)", (note_id,))
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES (?, ?, ?, 'create')",
            (version_id, note_id, body),
        )
        conn.execute(
            "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
            (version_id, note_id),
        )


def _update_note(
    conn: sqlite3.Connection,
    *,
    note_id: str = "note-1",
    version_id: str,
    parent_version_id: str,
    body: str = "An updated test note.",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO versions (version_id, note_id, parent_version_id, body, op) "
            "VALUES (?, ?, ?, ?, 'update')",
            (version_id, note_id, parent_version_id, body),
        )
        conn.execute(
            "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
            (version_id, note_id),
        )


def _insert_annotation(
    conn: sqlite3.Connection,
    *,
    target: str = "note-1",
    source_version: str | None = "ver-1",
    kind: str = "tag",
    payload_value: str = "python",
    source: str = "ai",
    status: str = "fresh",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (target, source_version, kind, json.dumps(payload_value), source, status),
        )


def _insert_edge(
    conn: sqlite3.Connection,
    *,
    from_id: str = "note-1",
    to_id: str = "topic-1",
    source_version: str | None = "ver-1",
    reason: str = "mentions jwt auth",
    confidence: float = 0.82,
    source: str = "ai",
    status: str = "fresh",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO edges "
            "(from_id, to_id, source, reason, confidence, source_version, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (from_id, to_id, source, reason, confidence, source_version, status),
        )


def _insert_enrich_job(
    conn: sqlite3.Connection,
    *,
    target_version: str = "ver-1",
    status: str = "pending",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) VALUES ('enrich', ?, ?)",
            (target_version, status),
        )


def _insert_passage(
    conn: sqlite3.Connection,
    *,
    passage_id: str = "pass-1",
    target_version: str = "ver-1",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO passages (passage_id, target_version, ord, text) "
            "VALUES (?, ?, 0, 'chunk text')",
            (passage_id, target_version),
        )


# ---------------------------------------------------------------------------
# Missing note
# ---------------------------------------------------------------------------


def test_missing_note_returns_none(conn: sqlite3.Connection) -> None:
    assert (
        enrichment_view(
            Path(conn.execute("PRAGMA database_list").fetchone()[2]), "nope"
        )
        is None
    )


# ---------------------------------------------------------------------------
# enrichment_state predicate
# ---------------------------------------------------------------------------


def test_ready_when_ai_output_exists_and_no_job(conn: sqlite3.Connection) -> None:
    """Enriched note, no enrich job left behind at all -> ready."""
    _insert_note(conn)
    _insert_annotation(conn, kind="summary", payload_value="a summary")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.enrichment_state == "ready"
    assert view.summary == "a summary"


def test_pending_when_live_enrich_job_and_no_ai_rows(conn: sqlite3.Connection) -> None:
    """Freshly captured, un-enriched note: a live enrich job, no AI rows yet."""
    _insert_note(conn)
    _insert_enrich_job(conn, status="pending")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.enrichment_state == "pending"
    assert view.summary is None
    assert view.tags == []
    assert view.entities == []
    assert view.edges == []


def test_pending_with_running_job(conn: sqlite3.Connection) -> None:
    _insert_note(conn)
    _insert_enrich_job(conn, status="running")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.enrichment_state == "pending"


def test_failed_when_dead_job_and_zero_ai_rows(conn: sqlite3.Connection) -> None:
    """Dead-lettered enrich job with no AI output -> failed, not enriched-empty."""
    _insert_note(conn)
    _insert_enrich_job(conn, status="dead")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.enrichment_state == "failed"


def test_failed_when_transient_failed_status_and_zero_ai_rows(
    conn: sqlite3.Connection,
) -> None:
    """A transient 'failed' status (no live job, no AI output yet) also reads failed."""
    _insert_note(conn)
    _insert_enrich_job(conn, status="failed")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.enrichment_state == "failed"


def test_ready_not_failed_when_dead_job_but_ai_rows_exist(
    conn: sqlite3.Connection,
) -> None:
    """A dead job alongside real AI output (e.g. a retry succeeded) is NOT 'failed'."""
    _insert_note(conn)
    _insert_enrich_job(conn, status="dead")
    _insert_annotation(conn, kind="tag", payload_value="python")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.enrichment_state == "ready"


def test_ready_when_dead_job_but_only_edge_output_exists(
    conn: sqlite3.Connection,
) -> None:
    """AI output can land as an edge alone (no annotation) and still count as ready."""
    _insert_note(conn)
    _insert_enrich_job(conn, status="dead")
    _insert_edge(conn)

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.enrichment_state == "ready"


def test_reenriching_note_reports_pending_with_stale_content_shown(
    conn: sqlite3.Connection,
) -> None:
    """Edit an enriched note: old-head AI rows go stale, new head gets a live job.

    Pinned predicate consequence (bd lode-ay5.1 notes): pending + stale content
    COEXIST in one render -- content is note_id-scoped and never suppressed by
    state.
    """
    _insert_note(conn, version_id="ver-1")
    _insert_annotation(
        conn,
        source_version="ver-1",
        kind="tag",
        payload_value="old-tag",
        status="stale",
    )
    _insert_edge(conn, source_version="ver-1", to_id="old-topic", status="stale")
    _update_note(conn, version_id="ver-2", parent_version_id="ver-1")
    _insert_enrich_job(conn, target_version="ver-2", status="pending")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.enrichment_state == "pending"
    assert view.tags == ["old-tag [stale]"]
    assert view.edges == [
        EnrichmentEdge(
            to_id="old-topic",
            reason="mentions jwt auth",
            confidence=0.82,
            stale=True,
        )
    ]


# ---------------------------------------------------------------------------
# Content: stale items, tombstones, edges carry reason+confidence
# ---------------------------------------------------------------------------


def test_stale_tag_shown_flagged_not_hidden(conn: sqlite3.Connection) -> None:
    _insert_note(conn)
    _insert_annotation(conn, kind="tag", payload_value="fresh-tag", status="fresh")
    _insert_annotation(conn, kind="tag", payload_value="stale-tag", status="stale")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert set(view.tags) == {"fresh-tag", "stale-tag [stale]"}


def test_edges_carry_reason_and_confidence(conn: sqlite3.Connection) -> None:
    _insert_note(conn)
    _insert_edge(conn, to_id="topic-a", reason="discusses topic a", confidence=0.91)

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.edges == [
        EnrichmentEdge(
            to_id="topic-a", reason="discusses topic a", confidence=0.91, stale=False
        )
    ]


def test_tombstoned_edge_is_dropped(conn: sqlite3.Connection) -> None:
    """A user-curation tombstone (source='user', status='orphaned') never shows."""
    _insert_note(conn)
    _insert_edge(conn, to_id="deleted-topic", source="user", status="orphaned")
    _insert_edge(conn, to_id="kept-topic", source="ai", status="fresh")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    to_ids = {e.to_id for e in view.edges}
    assert "deleted-topic" not in to_ids
    assert "kept-topic" in to_ids


def test_tombstoned_annotation_is_dropped(conn: sqlite3.Connection) -> None:
    _insert_note(conn)
    _insert_annotation(
        conn, kind="tag", payload_value="deleted-tag", source="user", status="orphaned"
    )
    _insert_annotation(conn, kind="tag", payload_value="kept-tag")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.tags == ["kept-tag"]


# ---------------------------------------------------------------------------
# Embed status
# ---------------------------------------------------------------------------


def test_embedded_true_with_passage_count(conn: sqlite3.Connection) -> None:
    _insert_note(conn)
    _insert_passage(conn)
    _insert_passage(conn, passage_id="pass-2")

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.embedded is True
    assert view.passage_count == 2


def test_embedded_false_with_no_passages(conn: sqlite3.Connection) -> None:
    _insert_note(conn)

    view = enrichment_view(_db_path(conn), "note-1")

    assert view is not None
    assert view.embedded is False
    assert view.passage_count == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _db_path(conn: sqlite3.Connection) -> Path:
    """Recover the on-disk path backing an already-open ``conn`` fixture.

    :func:`~lode.enrichment_view.enrichment_view` opens its own connection
    (the module's public convention, matching ``lode.notes_read``), so tests
    that seed rows via the fixture's ``conn`` need the same file path to call
    it against -- ``PRAGMA database_list`` reports it without threading a
    second fixture through every test.
    """
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])
