"""Tests for lode.enrich -- Haiku enrichment, immediate + Batches API (lode-npx.1/2).

Acceptance criteria (bd show lode-npx.1):
- Haiku returns validated tags/entities/inferred-edges with full provenance.
- Inferred edges are stored as source='ai' suggestions with confidence, never asserted
  facts.

Acceptance criteria (bd show lode-npx.2):
- A fresh note enriches via one immediate Haiku call (enrich_version, called from CLI).
- Bulk/backfill submits a Batch (submit_enrich_batch → Batches API, 50% off).
- collect_enrich_batch processes results when the batch ends.
- Embedding lands regardless of enrichment latency (embed job still enqueued from save).

Strategy: all tests mock the Anthropic client to run offline + keyless. Pydantic model
validation is tested directly; DB writes are verified against a real SQLite DB (init_db)
to exercise the actual schema constraints and CHECK clauses.
"""

import json
import sqlite3
import unittest.mock as mock
from pathlib import Path

import pytest

from lode.config import Settings
from lode.curation import delete_annotation, delete_edge
from lode.enrich import (
    ENRICH_PROMPT_VER,
    EnrichmentResult,
    InferredEdge,
    collect_enrich_batch,
    enrich_version,
    submit_enrich_batch,
)
from lode.reconcile import _enrich_gap_step
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


@pytest.fixture()
def settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_note(
    conn: sqlite3.Connection,
    *,
    note_id: str = "note-1",
    version_id: str = "ver-1",
    body: str = "This is a test note about Python authentication.",
    op: str = "create",
    purged_at: str | None = None,
    no_egress: int = 0,
) -> None:
    """Insert a notes + versions row pair and set the head pointer.

    Insertion order: notes row first (head_version_id NULL / deferred FK),
    then versions row (FK to notes is immediate), then update head pointer
    (deferred FK checked at COMMIT).
    """
    with conn:
        conn.execute(
            "INSERT INTO notes (note_id, no_egress) VALUES (?, ?)",
            (note_id, no_egress),
        )
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op, purged_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (version_id, note_id, body, op, purged_at),
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
    body: str,
) -> None:
    """Append a new head version to an existing note (an 'update', not a create)."""
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


def _fake_client(result: EnrichmentResult) -> mock.MagicMock:
    """Mock Anthropic client that returns the given EnrichmentResult as a tool call."""
    tool_block = mock.MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = result.model_dump()

    response = mock.MagicMock()
    response.content = [tool_block]

    client = mock.MagicMock()
    client.messages.create.return_value = response
    return client


def _annotations(conn: sqlite3.Connection, version_id: str = "ver-1") -> list[dict]:
    """Return all annotation rows for a version as dicts."""
    rows = conn.execute(
        "SELECT kind, payload, source, status, model, prompt_ver, source_version "
        "FROM annotations WHERE source_version = ? ORDER BY kind, payload",
        (version_id,),
    ).fetchall()
    return [
        {
            "kind": r[0],
            "payload": json.loads(r[1]),
            "source": r[2],
            "status": r[3],
            "model": r[4],
            "prompt_ver": r[5],
            "source_version": r[6],
        }
        for r in rows
    ]


def _edges(conn: sqlite3.Connection, version_id: str = "ver-1") -> list[dict]:
    """Return all edge rows for a source_version as dicts."""
    rows = conn.execute(
        "SELECT from_id, to_id, source, reason, confidence, source_version, status "
        "FROM edges WHERE source_version = ? ORDER BY to_id",
        (version_id,),
    ).fetchall()
    return [
        {
            "from_id": r[0],
            "to_id": r[1],
            "source": r[2],
            "reason": r[3],
            "confidence": r[4],
            "source_version": r[5],
            "status": r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Pydantic model validation
# ---------------------------------------------------------------------------


def test_enrichment_result_defaults_to_empty() -> None:
    """EnrichmentResult with no args defaults all lists to empty."""
    r = EnrichmentResult()
    assert r.tags == []
    assert r.entities == []
    assert r.inferred_edges == []


def test_enrichment_result_roundtrip() -> None:
    """EnrichmentResult serializes and deserializes correctly via model_dump."""
    original = EnrichmentResult(
        tags=["python", "auth"],
        entities=["OAuth2"],
        inferred_edges=[
            InferredEdge(to_id="oauth-notes", reason="mentions OAuth2", confidence=0.85)
        ],
    )
    rehydrated = EnrichmentResult.model_validate(original.model_dump())
    assert rehydrated.tags == ["python", "auth"]
    assert rehydrated.entities == ["OAuth2"]
    assert rehydrated.inferred_edges[0].confidence == pytest.approx(0.85)


def test_inferred_edge_confidence_bounds_valid() -> None:
    """Boundary values 0.0 and 1.0 are accepted."""
    InferredEdge(to_id="x", reason="y", confidence=0.0)
    InferredEdge(to_id="x", reason="y", confidence=1.0)
    InferredEdge(to_id="x", reason="y", confidence=0.5)


def test_inferred_edge_confidence_below_zero_rejected() -> None:
    """Confidence below 0.0 is rejected by Pydantic validation."""
    with pytest.raises(Exception):
        InferredEdge(to_id="x", reason="y", confidence=-0.01)


def test_inferred_edge_confidence_above_one_rejected() -> None:
    """Confidence above 1.0 is rejected by Pydantic validation."""
    with pytest.raises(Exception):
        InferredEdge(to_id="x", reason="y", confidence=1.01)


# ---------------------------------------------------------------------------
# enrich_version -- happy path
# ---------------------------------------------------------------------------


def test_enrich_version_writes_tags(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Tags are written as source='ai', kind='tag' annotations."""
    _insert_note(conn)
    result = EnrichmentResult(tags=["python", "auth"], entities=[], inferred_edges=[])
    enrich_version(conn, "ver-1", settings, client=_fake_client(result))

    rows = _annotations(conn)
    tag_rows = [r for r in rows if r["kind"] == "tag"]
    assert len(tag_rows) == 2
    tag_values = {r["payload"] for r in tag_rows}
    assert tag_values == {"python", "auth"}
    for r in tag_rows:
        assert r["source"] == "ai"
        assert r["status"] == "fresh"


def test_enrich_version_writes_entities(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Entities are written as source='ai', kind='entity' annotations."""
    _insert_note(conn)
    result = EnrichmentResult(
        tags=[], entities=["FastAPI", "Pydantic"], inferred_edges=[]
    )
    enrich_version(conn, "ver-1", settings, client=_fake_client(result))

    rows = _annotations(conn)
    entity_rows = [r for r in rows if r["kind"] == "entity"]
    assert len(entity_rows) == 2
    entity_values = {r["payload"] for r in entity_rows}
    assert entity_values == {"FastAPI", "Pydantic"}


def test_enrich_version_inferred_edges_are_source_ai(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Inferred edges are stored as source='ai' suggestions, never asserted facts.

    This is the core acceptance criterion for lode-npx.1: the edges table must
    record inferred relationships as suggestions with confidence, never as facts
    that the user or the knowledge graph treats as ground truth.
    """
    _insert_note(conn)
    result = EnrichmentResult(
        tags=[],
        entities=[],
        inferred_edges=[
            InferredEdge(
                to_id="jwt-topic", reason="discusses JWT tokens", confidence=0.9
            ),
            InferredEdge(
                to_id="security", reason="authentication topic", confidence=0.7
            ),
        ],
    )
    enrich_version(conn, "ver-1", settings, client=_fake_client(result))

    rows = _edges(conn)
    assert len(rows) == 2

    for row in rows:
        # source='ai' is the invariant -- never 'user'.
        assert row["source"] == "ai", (
            "inferred edges must be source='ai', never asserted facts"
        )
        assert row["status"] == "fresh"
        assert row["from_id"] == "note-1"
        assert row["source_version"] == "ver-1"
        assert 0.0 <= row["confidence"] <= 1.0

    # Verify per-edge confidences and reasons.
    edge_map = {r["to_id"]: r for r in rows}
    assert edge_map["jwt-topic"]["confidence"] == pytest.approx(0.9)
    assert "JWT" in edge_map["jwt-topic"]["reason"]
    assert edge_map["security"]["confidence"] == pytest.approx(0.7)


def test_enrich_version_returns_result(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """enrich_version returns the validated EnrichmentResult on success."""
    _insert_note(conn)
    result = EnrichmentResult(tags=["design"], entities=[], inferred_edges=[])
    returned = enrich_version(conn, "ver-1", settings, client=_fake_client(result))
    assert isinstance(returned, EnrichmentResult)
    assert returned.tags == ["design"]


def test_enrich_version_full_provenance(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Every annotation row carries model, prompt_ver, and source_version provenance."""
    _insert_note(conn)
    result = EnrichmentResult(tags=["x"], entities=["Y"], inferred_edges=[])
    enrich_version(conn, "ver-1", settings, client=_fake_client(result))

    rows = _annotations(conn)
    assert rows, "annotations must exist after enrichment"
    for row in rows:
        assert row["model"] == settings.enrichment_llm
        assert row["prompt_ver"] == ENRICH_PROMPT_VER
        assert row["source_version"] == "ver-1"


def test_enrich_version_writes_egress_log(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """One egress_log row with purpose='enrich' is written per enrichment call."""
    _insert_note(conn)
    result = EnrichmentResult(tags=["a"], entities=[], inferred_edges=[])
    enrich_version(conn, "ver-1", settings, client=_fake_client(result))

    log_rows = conn.execute("SELECT purpose, model FROM egress_log").fetchall()
    assert len(log_rows) == 1
    assert log_rows[0][0] == "enrich"
    assert log_rows[0][1] == settings.enrichment_llm


def test_enrich_version_empty_result_is_valid(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A note that yields no tags/entities/edges still succeeds and logs egress."""
    _insert_note(conn)
    result = EnrichmentResult(tags=[], entities=[], inferred_edges=[])
    returned = enrich_version(conn, "ver-1", settings, client=_fake_client(result))
    assert returned is not None
    # No annotations or edges.
    assert conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    # But egress_log was still written.
    assert conn.execute("SELECT COUNT(*) FROM egress_log").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Skipping behavior
# ---------------------------------------------------------------------------


def test_enrich_version_skips_no_egress(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """no_egress notes are never sent to Haiku; enrich_version returns None."""
    _insert_note(conn, no_egress=1)
    client = _fake_client(EnrichmentResult())

    result = enrich_version(conn, "ver-1", settings, client=client)
    assert result is None
    # The API must not have been called.
    client.messages.create.assert_not_called()
    # No side effects: no annotations, no egress_log.
    assert conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM egress_log").fetchone()[0] == 0


def test_enrich_version_skips_tombstone(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Soft-deleted versions (op='delete') are skipped."""
    _insert_note(conn, op="delete")
    client = _fake_client(EnrichmentResult())

    result = enrich_version(conn, "ver-1", settings, client=client)
    assert result is None
    client.messages.create.assert_not_called()


def test_enrich_version_skips_purged(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Purged versions (purged_at IS NOT NULL) are skipped."""
    _insert_note(conn, purged_at="2026-01-01T00:00:00.000Z")
    client = _fake_client(EnrichmentResult())

    result = enrich_version(conn, "ver-1", settings, client=client)
    assert result is None
    client.messages.create.assert_not_called()


def test_enrich_version_skips_missing_version(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Missing version_id returns None without raising."""
    client = _fake_client(EnrichmentResult())
    result = enrich_version(conn, "nonexistent-ver", settings, client=client)
    assert result is None
    client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_enrich_version_idempotent_replaces_old_results(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Re-enriching the same version replaces existing source='ai' rows.

    The handler deletes old source='ai' annotations and edges for the version
    before writing new ones, so re-running converges to the latest result.
    """
    _insert_note(conn)

    first = EnrichmentResult(
        tags=["old-tag"],
        entities=[],
        inferred_edges=[
            InferredEdge(to_id="old-concept", reason="old", confidence=0.5)
        ],
    )
    enrich_version(conn, "ver-1", settings, client=_fake_client(first))

    second = EnrichmentResult(
        tags=["new-tag"],
        entities=["NewEntity"],
        inferred_edges=[
            InferredEdge(to_id="new-concept", reason="new", confidence=0.8)
        ],
    )
    enrich_version(conn, "ver-1", settings, client=_fake_client(second))

    # Only the second result remains.
    ann_rows = _annotations(conn)
    payloads = {r["payload"] for r in ann_rows}
    assert "old-tag" not in payloads
    assert "new-tag" in payloads
    assert "NewEntity" in payloads

    edge_rows = _edges(conn)
    edge_to_ids = {r["to_id"] for r in edge_rows}
    assert "old-concept" not in edge_to_ids
    assert "new-concept" in edge_to_ids


def test_enrich_version_user_annotations_preserved(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """User annotations (source='user') are NOT deleted on re-enrichment.

    The idempotency delete is scoped to source='ai' only, so user curation
    (irreplaceable, pinned to note_id) survives a re-enrichment.
    """
    _insert_note(conn)
    # Plant a user annotation.
    with conn:
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES ('note-1', 'ver-1', 'tag', '\"pinned\"', 'user', 'fresh')"
        )

    result = EnrichmentResult(tags=["ai-tag"], entities=[], inferred_edges=[])
    enrich_version(conn, "ver-1", settings, client=_fake_client(result))

    # User annotation still present.
    user_rows = conn.execute(
        "SELECT payload FROM annotations WHERE source = 'user'"
    ).fetchall()
    assert len(user_rows) == 1
    assert json.loads(user_rows[0][0]) == "pinned"


# ---------------------------------------------------------------------------
# User pinning: a deleted tag/link is not re-added (lode-npx.4)
# ---------------------------------------------------------------------------


def test_deleted_tag_is_not_re_added_on_re_enrichment(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A tag the user deletes stays deleted across re-enrichment.

    lode.curation.delete_annotation converts the row to a source='user'
    tombstone; _write_enrichment must see that tombstone and skip
    re-inserting a matching AI suggestion, even though Haiku keeps proposing
    the same tag.
    """
    _insert_note(conn)
    result = EnrichmentResult(tags=["python", "auth"], entities=[], inferred_edges=[])
    enrich_version(conn, "ver-1", settings, client=_fake_client(result))

    # User deletes the "python" tag.
    (row_id,) = conn.execute(
        "SELECT id FROM annotations WHERE kind = 'tag' AND payload = '\"python\"'"
    ).fetchone()
    delete_annotation(conn, row_id)

    # Note is edited and re-enriched; Haiku proposes the same tags again.
    _update_note(
        conn, version_id="ver-2", parent_version_id="ver-1", body="updated body"
    )
    enrich_version(conn, "ver-2", settings, client=_fake_client(result))

    tag_payloads = {
        json.loads(p)
        for (p,) in conn.execute(
            "SELECT payload FROM annotations WHERE kind = 'tag' AND source = 'ai'"
        ).fetchall()
    }
    assert "python" not in tag_payloads
    assert "auth" in tag_payloads


def test_deleted_edge_is_not_re_added_on_re_enrichment(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """An inferred edge (link) the user deletes stays deleted across re-enrichment."""
    _insert_note(conn)
    result = EnrichmentResult(
        tags=[],
        entities=[],
        inferred_edges=[
            InferredEdge(to_id="jwt-topic", reason="mentions JWT", confidence=0.7)
        ],
    )
    enrich_version(conn, "ver-1", settings, client=_fake_client(result))

    (row_id,) = conn.execute(
        "SELECT id FROM edges WHERE to_id = 'jwt-topic'"
    ).fetchone()
    delete_edge(conn, row_id)

    _update_note(
        conn, version_id="ver-2", parent_version_id="ver-1", body="updated body"
    )
    enrich_version(conn, "ver-2", settings, client=_fake_client(result))

    ai_edge_to_ids = {
        r[0]
        for r in conn.execute("SELECT to_id FROM edges WHERE source = 'ai'").fetchall()
    }
    assert "jwt-topic" not in ai_edge_to_ids


# ---------------------------------------------------------------------------
# Enrich-gap reconcile step
# ---------------------------------------------------------------------------


def test_enrich_gap_enqueues_for_missing_job(conn: sqlite3.Connection) -> None:
    """A head version with no enrich job is re-enqueued by the enrich-gap step."""
    _insert_note(conn)
    count = _enrich_gap_step(conn)
    assert count == 1

    rows = conn.execute(
        "SELECT type, target_version, status FROM jobs WHERE type = 'enrich'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "enrich"
    assert rows[0][1] == "ver-1"
    assert rows[0][2] == "pending"


def test_enrich_gap_returns_zero_when_no_notes(conn: sqlite3.Connection) -> None:
    """Empty notes table produces a gap count of 0."""
    assert _enrich_gap_step(conn) == 0


def test_enrich_gap_idempotent(conn: sqlite3.Connection) -> None:
    """Running the enrich-gap step twice enqueues no duplicate jobs."""
    _insert_note(conn)
    _enrich_gap_step(conn)
    _enrich_gap_step(conn)  # ON CONFLICT DO NOTHING keeps exactly one row
    rows = conn.execute("SELECT COUNT(*) FROM jobs WHERE type = 'enrich'").fetchone()
    assert rows[0] == 1


def test_enrich_gap_skips_tombstone(conn: sqlite3.Connection) -> None:
    """Soft-deleted head versions (op='delete') are not enqueued."""
    _insert_note(conn, op="delete")
    assert _enrich_gap_step(conn) == 0


def test_enrich_gap_skips_purged(conn: sqlite3.Connection) -> None:
    """Purged head versions (purged_at IS NOT NULL) are not enqueued."""
    _insert_note(conn, purged_at="2026-01-01T00:00:00.000Z")
    assert _enrich_gap_step(conn) == 0


def test_enrich_gap_skips_no_egress(conn: sqlite3.Connection) -> None:
    """no_egress notes are excluded from enrichment gap detection."""
    _insert_note(conn, no_egress=1)
    assert _enrich_gap_step(conn) == 0


def test_enrich_gap_skips_live_job(conn: sqlite3.Connection) -> None:
    """A version with a live (pending/running/done/failed) enrich job is not re-enqueued."""
    _insert_note(conn)
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version) VALUES ('enrich', 'ver-1')"
        )
    assert _enrich_gap_step(conn) == 0


def test_enrich_gap_skips_in_flight_batch_job(conn: sqlite3.Connection) -> None:
    """A running enrich job WITH a batch_handle is not a gap (lode-i05.5).

    This is the double-spend guard the design calls out explicitly: once a
    Batch is submitted the member job is 'running' with batch_handle set (not
    'pending'), so the enrich-gap scan's "not yet enriched" query must treat
    it as live and skip it -- re-enqueueing here would mean a second Haiku
    call for a note already covered by money-in-flight.
    """
    _insert_note(conn)
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status, batch_handle) "
            "VALUES ('enrich', 'ver-1', 'running', 'batch-abc123')"
        )
    assert _enrich_gap_step(conn) == 0
    # Still exactly the one in-flight job -- no duplicate pending row.
    rows = conn.execute(
        "SELECT status, batch_handle FROM jobs WHERE type = 'enrich'"
    ).fetchall()
    assert rows == [("running", "batch-abc123")]


def test_enrich_gap_reenqueues_dead_job(conn: sqlite3.Connection) -> None:
    """A dead-lettered enrich job is treated as a gap and re-enqueued."""
    _insert_note(conn)
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) VALUES ('enrich', 'ver-1', 'dead')"
        )
    count = _enrich_gap_step(conn)
    assert count == 1
    statuses = conn.execute(
        "SELECT status FROM jobs WHERE type = 'enrich' AND target_version = 'ver-1'"
        " ORDER BY id"
    ).fetchall()
    assert ("dead",) in statuses
    assert ("pending",) in statuses


def test_enrich_gap_multiple_versions(conn: sqlite3.Connection) -> None:
    """Multiple notes without enrich jobs all get enqueued."""
    _insert_note(conn, note_id="note-1", version_id="ver-1")
    _insert_note(conn, note_id="note-2", version_id="ver-2")
    count = _enrich_gap_step(conn)
    assert count == 2
    for ver in ("ver-1", "ver-2"):
        rows = conn.execute(
            "SELECT status FROM jobs WHERE type = 'enrich' AND target_version = ?",
            (ver,),
        ).fetchall()
        assert rows == [("pending",)]


# ---------------------------------------------------------------------------
# Batch API helpers — submit_enrich_batch (lode-npx.2)
# ---------------------------------------------------------------------------


def _insert_enrich_job(
    conn: sqlite3.Connection,
    version_id: str = "ver-1",
    status: str = "pending",
) -> int:
    """Insert a pending enrich job row; return the job id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (type, target_version, status) VALUES ('enrich', ?, ?)",
            (version_id, status),
        )
    return cur.lastrowid


def _fake_batch_client(
    batch_id: str = "batch-abc",
    results: list | None = None,
    processing_status: str = "ended",
) -> mock.MagicMock:
    """Mock Anthropic client with a Batches API stub.

    ``results`` is a list of mock result objects; each needs:
    - ``.custom_id`` (version_id)
    - ``.result.type`` ('succeeded' | 'errored')
    - ``.result.message.content`` (list of blocks) when type='succeeded'
    """
    client = mock.MagicMock()

    # Batch creation
    batch = mock.MagicMock()
    batch.id = batch_id
    client.beta.messages.batches.create.return_value = batch

    # Batch retrieve (status)
    status_obj = mock.MagicMock()
    status_obj.processing_status = processing_status
    client.beta.messages.batches.retrieve.return_value = status_obj

    # Batch results
    client.beta.messages.batches.results.return_value = iter(results or [])

    return client


def _make_batch_result(
    version_id: str,
    enrichment: EnrichmentResult,
    result_type: str = "succeeded",
) -> mock.MagicMock:
    """Build a mock batch result object (succeeded or errored)."""
    r = mock.MagicMock()
    r.custom_id = version_id
    r.result.type = result_type

    if result_type == "succeeded":
        tool_block = mock.MagicMock()
        tool_block.type = "tool_use"
        tool_block.input = enrichment.model_dump()
        r.result.message.content = [tool_block]

    return r


def test_submit_enrich_batch_returns_batch_id(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """submit_enrich_batch returns the batch ID from the Batches API."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client(batch_id="batch-xyz")
    result_id = submit_enrich_batch(conn, [(job_id, "ver-1")], settings, client=client)
    assert result_id == "batch-xyz"


def test_submit_enrich_batch_stores_batch_handle(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Submitted jobs are marked running with batch_handle set."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client(batch_id="batch-handle-test")
    submit_enrich_batch(conn, [(job_id, "ver-1")], settings, client=client)

    row = conn.execute(
        "SELECT status, batch_handle FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row[0] == "running"
    assert row[1] == "batch-handle-test"


def test_submit_enrich_batch_skips_no_egress(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """no_egress versions are marked done without being sent to the Batches API."""
    _insert_note(conn, no_egress=1)
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client()
    result_id = submit_enrich_batch(conn, [(job_id, "ver-1")], settings, client=client)

    # Nothing submitted to the batch (all gated out).
    assert result_id is None
    client.beta.messages.batches.create.assert_not_called()

    # Job marked done immediately.
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "done"


def test_submit_enrich_batch_skips_tombstone(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Tombstone (op='delete') versions are marked done without batch submission."""
    _insert_note(conn, op="delete")
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client()
    result_id = submit_enrich_batch(conn, [(job_id, "ver-1")], settings, client=client)

    assert result_id is None
    client.beta.messages.batches.create.assert_not_called()
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "done"


def test_submit_enrich_batch_skips_purged(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Purged versions are marked done without batch submission."""
    _insert_note(conn, purged_at="2026-01-01T00:00:00.000Z")
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client()
    result_id = submit_enrich_batch(conn, [(job_id, "ver-1")], settings, client=client)

    assert result_id is None
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "done"


def test_submit_enrich_batch_returns_none_for_empty_input(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Empty job_rows returns None without calling the API."""
    client = _fake_batch_client()
    result_id = submit_enrich_batch(conn, [], settings, client=client)

    assert result_id is None
    client.beta.messages.batches.create.assert_not_called()


def test_submit_enrich_batch_writes_egress_log(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A single egress_log row is written for the batch submission."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client(batch_id="batch-egress")
    submit_enrich_batch(conn, [(job_id, "ver-1")], settings, client=client)

    rows = conn.execute(
        "SELECT purpose, model FROM egress_log WHERE purpose = 'enrich'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == settings.enrichment_llm


def test_submit_enrich_batch_multiple_versions(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Multiple valid versions are all included in the same batch request."""
    _insert_note(conn, note_id="note-1", version_id="ver-1")
    _insert_note(conn, note_id="note-2", version_id="ver-2")
    job1 = _insert_enrich_job(conn, "ver-1")
    job2 = _insert_enrich_job(conn, "ver-2")

    client = _fake_batch_client(batch_id="batch-multi")
    submit_enrich_batch(
        conn, [(job1, "ver-1"), (job2, "ver-2")], settings, client=client
    )

    # API called exactly once with both requests.
    assert client.beta.messages.batches.create.call_count == 1
    call_kwargs = client.beta.messages.batches.create.call_args
    # 'requests' passed as keyword arg.
    requests = call_kwargs.kwargs.get("requests") or call_kwargs.args[0]
    custom_ids = {r["custom_id"] for r in requests}
    assert custom_ids == {"ver-1", "ver-2"}

    # Both jobs now running with the handle.
    for jid in (job1, job2):
        row = conn.execute(
            "SELECT status, batch_handle FROM jobs WHERE id = ?", (jid,)
        ).fetchone()
        assert row[0] == "running"
        assert row[1] == "batch-multi"


# ---------------------------------------------------------------------------
# Batch API helpers — collect_enrich_batch (lode-npx.2)
# ---------------------------------------------------------------------------


def test_collect_enrich_batch_returns_false_when_in_progress(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """collect_enrich_batch returns False when the batch is still in progress."""
    client = _fake_batch_client(processing_status="in_progress")
    ended = collect_enrich_batch(conn, "batch-in-flight", settings, client=client)
    assert ended is False


def test_collect_enrich_batch_returns_true_and_writes_enrichment(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """collect_enrich_batch processes succeeded results and marks jobs done."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn, "ver-1", status="running")
    # Simulate: job has a batch_handle.
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_handle = 'batch-done' WHERE id = ?", (job_id,)
        )

    enrichment = EnrichmentResult(
        tags=["python", "api"],
        entities=["FastAPI"],
        inferred_edges=[
            InferredEdge(
                to_id="web-frameworks", reason="mentions FastAPI", confidence=0.9
            )
        ],
    )
    br = _make_batch_result("ver-1", enrichment)
    client = _fake_batch_client(batch_id="batch-done", results=[br])

    ended = collect_enrich_batch(conn, "batch-done", settings, client=client)
    assert ended is True

    # Job marked done.
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "done"

    # Enrichment written to DB.
    ann_rows = conn.execute(
        "SELECT kind, payload FROM annotations WHERE source_version = 'ver-1'"
    ).fetchall()
    kinds = {r[0] for r in ann_rows}
    assert "tag" in kinds
    assert "entity" in kinds

    edge_rows = conn.execute(
        "SELECT to_id FROM edges WHERE source_version = 'ver-1'"
    ).fetchall()
    assert edge_rows == [("web-frameworks",)]


def test_collect_enrich_batch_marks_failed_on_errored_result(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """An errored batch result marks the job failed with backoff."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn, "ver-1", status="running")
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_handle = 'batch-err' WHERE id = ?", (job_id,)
        )

    br = _make_batch_result("ver-1", EnrichmentResult(), result_type="errored")
    client = _fake_batch_client(batch_id="batch-err", results=[br])

    ended = collect_enrich_batch(conn, "batch-err", settings, client=client)
    assert ended is True

    row = conn.execute(
        "SELECT status, last_error, attempts FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row[0] == "failed"
    assert row[2] == 1  # attempts incremented
    assert row[1] is not None


def test_collect_enrich_batch_dead_letters_at_max_attempts(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """An errored result at max_attempts dead-letters the job."""
    settings_low = Settings(retry_max_attempts=2)
    _insert_note(conn)
    job_id = _insert_enrich_job(conn, "ver-1", status="running")
    # Pre-set attempts = 1 so the next failure triggers dead-letter.
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_handle = 'batch-dead', attempts = 1 WHERE id = ?",
            (job_id,),
        )

    br = _make_batch_result("ver-1", EnrichmentResult(), result_type="errored")
    client = _fake_batch_client(batch_id="batch-dead", results=[br])

    collect_enrich_batch(conn, "batch-dead", settings_low, client=client)

    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "dead"


def test_collect_enrich_batch_idempotent_on_no_running_jobs(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """collect_enrich_batch with no running jobs for the handle returns True."""
    # No running jobs with batch_handle='batch-xyz'.
    client = _fake_batch_client(batch_id="batch-xyz")
    ended = collect_enrich_batch(conn, "batch-xyz", settings, client=client)
    # Batch is ended (processing_status='ended' is the default) so True.
    assert ended is True
