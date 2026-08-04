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
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

from lode.config import Settings
from lode.curation import delete_annotation, delete_edge
from lode.enrich import (
    ENRICH_PROMPT_VER,
    MAX_TOKENS,
    EnrichmentResult,
    InferredEdge,
    collect_enrich_batch,
    enrich_version,
    format_enrich_outcome,
    submit_enrich_batch,
)
from lode.jobs import now_iso
from lode.llm_provider import AnthropicProvider, ModelTier
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


def _insert_external(
    conn: sqlite3.Connection,
    *,
    external_id: str = "ext-1",
    snapshot_id: str = "snap-1",
    body: str = "This is a test snapshot about Python authentication.",
    status: str = "ok",
    no_egress: int = 0,
) -> None:
    """Insert an externals + snapshots row pair and set the head pointer.

    Mirrors :func:`_insert_note`'s note/version fixture, but on the external
    side (lode-7qi) — insertion order: externals row first (head_snapshot_id
    NULL / deferred FK), then snapshots row, then head pointer update.
    """
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, no_egress) "
            "VALUES (?, 'web', ?)",
            (external_id, no_egress),
        )
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
            "VALUES (?, ?, ?, ?)",
            (snapshot_id, external_id, body, status),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
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
        "SELECT kind, payload, source, status, model, prompt_ver, source_version, "
        "provider "
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
            "provider": r[7],
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


# lode-b4w.3: the bounds-valid/below-zero/above-one trio all exercise the
# same Pydantic confidence-field validator, differing only in the input
# value and whether it should raise -- parametrized over (confidence,
# should_raise), 3 tests -> 1, every original value still checked.
@pytest.mark.parametrize(
    "confidence, should_raise",
    [
        pytest.param(0.0, False, id="lower_bound"),
        pytest.param(1.0, False, id="upper_bound"),
        pytest.param(0.5, False, id="mid_range"),
        pytest.param(-0.01, True, id="below_zero"),
        pytest.param(1.01, True, id="above_one"),
    ],
)
def test_inferred_edge_confidence_validation(
    confidence: float, should_raise: bool
) -> None:
    if should_raise:
        with pytest.raises(ValidationError):
            InferredEdge(to_id="x", reason="y", confidence=confidence)
    else:
        InferredEdge(to_id="x", reason="y", confidence=confidence)


# ---------------------------------------------------------------------------
# enrich_version -- happy path
# ---------------------------------------------------------------------------


def test_enrich_version_writes_tags(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Tags are written as source='ai', kind='tag' annotations."""
    _insert_note(conn)
    result = EnrichmentResult(tags=["python", "auth"], entities=[], inferred_edges=[])
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

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
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    rows = _annotations(conn)
    entity_rows = [r for r in rows if r["kind"] == "entity"]
    assert len(entity_rows) == 2
    entity_values = {r["payload"] for r in entity_rows}
    assert entity_values == {"FastAPI", "Pydantic"}


def test_enrich_version_writes_summary(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A non-empty summary is written as exactly one source='ai', kind='summary'
    whole-note annotation (lode-0wj.9)."""
    _insert_note(conn)
    result = EnrichmentResult(
        tags=[],
        entities=[],
        inferred_edges=[],
        summary="A note about Python authentication.",
    )
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    rows = _annotations(conn)
    summary_rows = [r for r in rows if r["kind"] == "summary"]
    assert len(summary_rows) == 1
    row = summary_rows[0]
    assert row["payload"] == "A note about Python authentication."
    assert row["source"] == "ai"
    assert row["status"] == "fresh"
    assert row["source_version"] == "ver-1"


def test_enrich_version_empty_summary_writes_no_row(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """An empty summary produces no annotation row, mirroring an empty tag list."""
    _insert_note(conn)
    result = EnrichmentResult(tags=[], entities=[], inferred_edges=[], summary="")
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    rows = _annotations(conn)
    assert [r for r in rows if r["kind"] == "summary"] == []


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
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

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
    returned = enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )
    assert isinstance(returned, EnrichmentResult)
    assert returned.tags == ["design"]


def test_enrich_version_passes_anthropic_call_timeout_to_create(
    conn: sqlite3.Connection,
) -> None:
    """The immediate Haiku call is bounded by Settings.llm_call_timeout_s
    (lode-olmi.15) -- this call is reachable from 'lode work's drain loop (a
    residual enrich job claimed by the main claim/run loop), so with no
    client-side timeout it could otherwise hang the drain forever.
    """
    _insert_note(conn)
    result = EnrichmentResult(tags=["design"], entities=[], inferred_edges=[])
    settings = Settings(llm_call_timeout_s=42.0)
    client = _fake_client(result)
    enrich_version(conn, "ver-1", settings, provider=AnthropicProvider(client))

    create_kwargs = client.messages.create.call_args.kwargs
    assert create_kwargs["timeout"] == 42.0


def test_enrich_version_uses_the_raised_max_tokens(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """The immediate Haiku call sends enrich.MAX_TOKENS (lode-jgus), not a
    stale inline 1024 -- headroom for a thinking-capable enrichment_llm
    override to share the budget with the forced tool-call JSON.
    """
    _insert_note(conn)
    result = EnrichmentResult(tags=["design"], entities=[], inferred_edges=[])
    client = _fake_client(result)
    enrich_version(conn, "ver-1", settings, provider=AnthropicProvider(client))

    create_kwargs = client.messages.create.call_args.kwargs
    assert create_kwargs["max_tokens"] == MAX_TOKENS
    assert MAX_TOKENS > 1024  # the raise this ticket exists to make


def test_enrich_version_max_tokens_override_reaches_the_call(
    conn: sqlite3.Connection,
) -> None:
    """lode-d70n: a Kind.RUNTIME override of enrichment_llm.max_tokens must
    actually change the budget sent on the wire, not just the model/effort.
    """
    _insert_note(conn)
    settings = Settings(
        enrichment_llm=ModelTier(model="claude-haiku-4-5", max_tokens=777)
    )
    result = EnrichmentResult(tags=["design"], entities=[], inferred_edges=[])
    client = _fake_client(result)
    enrich_version(conn, "ver-1", settings, provider=AnthropicProvider(client))

    create_kwargs = client.messages.create.call_args.kwargs
    assert create_kwargs["max_tokens"] == 777


def test_enrich_version_with_thinking_capable_override_omits_thinking(
    conn: sqlite3.Connection,
) -> None:
    """A Kind.RUNTIME override of enrichment_llm to a thinking-capable model
    (lode-jgus) still never sends `thinking` on the forced tool-use branch --
    that rule is a property of the branch, not of the model (see the
    AnthropicProvider class docstring).
    """
    _insert_note(conn)
    settings = Settings(enrichment_llm=ModelTier(model="claude-opus-5"))
    result = EnrichmentResult(tags=["design"], entities=[], inferred_edges=[])
    client = _fake_client(result)
    enrich_version(conn, "ver-1", settings, provider=AnthropicProvider(client))

    create_kwargs = client.messages.create.call_args.kwargs
    assert create_kwargs["model"] == "claude-opus-5"
    assert "thinking" not in create_kwargs


def test_enrich_version_full_provenance(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Every annotation row carries model, prompt_ver, and source_version provenance."""
    _insert_note(conn)
    result = EnrichmentResult(tags=["x"], entities=["Y"], inferred_edges=[])
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    rows = _annotations(conn)
    assert rows, "annotations must exist after enrichment"
    for row in rows:
        assert row["model"] == settings.enrichment_llm.model
        assert row["prompt_ver"] == ENRICH_PROMPT_VER
        assert row["source_version"] == "ver-1"
        # lode-568v.4: NULL means "anthropic" by convention -- settings.llm_provider
        # is "anthropic" here (the only value accepted today).
        assert row["provider"] is None


def test_enrich_version_summary_full_provenance(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """The summary annotation carries the same full provenance as tag/entity."""
    _insert_note(conn)
    result = EnrichmentResult(
        tags=[], entities=[], inferred_edges=[], summary="A summary sentence."
    )
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    rows = [r for r in _annotations(conn) if r["kind"] == "summary"]
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == settings.enrichment_llm.model
    assert row["prompt_ver"] == ENRICH_PROMPT_VER
    assert row["source_version"] == "ver-1"


def test_enrich_version_writes_egress_log(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """One egress_log row with purpose='enrich' is written per enrichment call."""
    _insert_note(conn)
    result = EnrichmentResult(tags=["a"], entities=[], inferred_edges=[])
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    log_rows = conn.execute(
        "SELECT purpose, model, provider FROM egress_log"
    ).fetchall()
    assert len(log_rows) == 1
    assert log_rows[0][0] == "enrich"
    assert log_rows[0][1] == settings.enrichment_llm.model
    # lode-568v.4: NULL means "anthropic" by convention.
    assert log_rows[0][2] is None


def test_enrich_version_empty_result_is_valid(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A note that yields no tags/entities/edges still succeeds and logs egress."""
    _insert_note(conn)
    result = EnrichmentResult(tags=[], entities=[], inferred_edges=[])
    returned = enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )
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

    result = enrich_version(conn, "ver-1", settings, provider=AnthropicProvider(client))
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

    result = enrich_version(conn, "ver-1", settings, provider=AnthropicProvider(client))
    assert result is None
    client.messages.create.assert_not_called()


def test_enrich_version_skips_purged(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Purged versions (purged_at IS NOT NULL) are skipped."""
    _insert_note(conn, purged_at="2026-01-01T00:00:00.000Z")
    client = _fake_client(EnrichmentResult())

    result = enrich_version(conn, "ver-1", settings, provider=AnthropicProvider(client))
    assert result is None
    client.messages.create.assert_not_called()


def test_enrich_version_skips_missing_version(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Missing version_id returns None without raising."""
    client = _fake_client(EnrichmentResult())
    result = enrich_version(
        conn, "nonexistent-ver", settings, provider=AnthropicProvider(client)
    )
    assert result is None
    client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# enrich_version -- external snapshot targets (lode-7qi)
#
# The gap this ticket closes: gate_reenrich (lode.externals) enqueues an
# 'enrich' job keyed on a snapshot_id. Before lode-7qi, enrich_version's
# note-only lookup returned None for that target -- silently no-op, no Haiku
# call ever made. These tests exercise the snapshot resolution path directly.
# ---------------------------------------------------------------------------


def test_enrich_version_snapshot_runs_haiku_and_writes_against_external_id(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A snapshot_id target runs Haiku extraction and writes against external_id.

    This is lode-7qi's core acceptance criterion: an enrich job whose
    target_version is a snapshot_id must actually call Haiku and persist
    annotations/edges keyed to the external, not silently no-op.
    """
    _insert_external(conn, external_id="ext-1", snapshot_id="snap-1")
    result = EnrichmentResult(
        tags=["python", "auth"],
        entities=["FastAPI"],
        inferred_edges=[
            InferredEdge(to_id="jwt-topic", reason="mentions JWT", confidence=0.8)
        ],
        summary="A snapshot about Python auth.",
    )
    client = _fake_client(result)
    returned = enrich_version(
        conn, "snap-1", settings, provider=AnthropicProvider(client)
    )

    assert returned == result
    client.messages.create.assert_called_once()

    ann_rows = _annotations(conn, version_id="snap-1")
    assert {r["kind"] for r in ann_rows} == {"tag", "entity", "summary"}

    # annotations.target and edges.from_id both resolve to the external_id,
    # not a note_id -- the polymorphic owner (schema.sql).
    targets = {
        r[0]
        for r in conn.execute(
            "SELECT target FROM annotations WHERE source_version = 'snap-1'"
        ).fetchall()
    }
    assert targets == {"ext-1"}

    edge_rows = _edges(conn, version_id="snap-1")
    assert len(edge_rows) == 1
    assert edge_rows[0]["from_id"] == "ext-1"
    assert edge_rows[0]["to_id"] == "jwt-topic"


def test_enrich_version_snapshot_writes_egress_log(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A snapshot enrichment audits egress the same as a note enrichment."""
    _insert_external(conn)
    result = EnrichmentResult(tags=["a"], entities=[], inferred_edges=[])
    enrich_version(
        conn, "snap-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    log_rows = conn.execute("SELECT purpose, model FROM egress_log").fetchall()
    assert len(log_rows) == 1
    assert log_rows[0][0] == "enrich"


def test_enrich_version_skips_no_egress_external(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A no_egress external's snapshot is never sent to Haiku."""
    _insert_external(conn, no_egress=1)
    client = _fake_client(EnrichmentResult())

    result = enrich_version(
        conn, "snap-1", settings, provider=AnthropicProvider(client)
    )
    assert result is None
    client.messages.create.assert_not_called()
    assert conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0


def test_enrich_version_skips_tombstone_snapshot(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A link-rot tombstone snapshot (status='tombstone') is skipped."""
    _insert_external(conn, status="tombstone")
    client = _fake_client(EnrichmentResult())

    result = enrich_version(
        conn, "snap-1", settings, provider=AnthropicProvider(client)
    )
    assert result is None
    client.messages.create.assert_not_called()


def test_enrich_version_snapshot_idempotent_replace(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Re-enriching the same snapshot replaces existing source='ai' rows."""
    _insert_external(conn)

    first = EnrichmentResult(tags=["old-tag"], entities=[], inferred_edges=[])
    enrich_version(
        conn, "snap-1", settings, provider=AnthropicProvider(_fake_client(first))
    )

    second = EnrichmentResult(tags=["new-tag"], entities=[], inferred_edges=[])
    enrich_version(
        conn, "snap-1", settings, provider=AnthropicProvider(_fake_client(second))
    )

    payloads = {r["payload"] for r in _annotations(conn, version_id="snap-1")}
    assert "old-tag" not in payloads
    assert "new-tag" in payloads


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
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(first))
    )

    second = EnrichmentResult(
        tags=["new-tag"],
        entities=["NewEntity"],
        inferred_edges=[
            InferredEdge(to_id="new-concept", reason="new", confidence=0.8)
        ],
    )
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(second))
    )

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


def test_enrich_version_summary_idempotent_replace(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Re-enriching the same version replaces the old summary with the new one.

    Exactly one kind='summary' row must exist after re-enrichment -- the old
    summary text is gone, the new one is present (source_version-keyed replace,
    same as tag/entity).
    """
    _insert_note(conn)

    first = EnrichmentResult(
        tags=[], entities=[], inferred_edges=[], summary="Old summary."
    )
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(first))
    )

    second = EnrichmentResult(
        tags=[], entities=[], inferred_edges=[], summary="New summary."
    )
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(second))
    )

    summary_rows = [r for r in _annotations(conn) if r["kind"] == "summary"]
    assert len(summary_rows) == 1
    assert summary_rows[0]["payload"] == "New summary."


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
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

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
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    # User deletes the "python" tag.
    (row_id,) = conn.execute(
        "SELECT id FROM annotations WHERE kind = 'tag' AND payload = '\"python\"'"
    ).fetchone()
    delete_annotation(conn, row_id)

    # Note is edited and re-enriched; Haiku proposes the same tags again.
    _update_note(
        conn, version_id="ver-2", parent_version_id="ver-1", body="updated body"
    )
    enrich_version(
        conn, "ver-2", settings, provider=AnthropicProvider(_fake_client(result))
    )

    tag_payloads = {
        json.loads(p)
        for (p,) in conn.execute(
            "SELECT payload FROM annotations WHERE kind = 'tag' AND source = 'ai'"
        ).fetchall()
    }
    assert "python" not in tag_payloads
    assert "auth" in tag_payloads


def test_deleted_summary_is_not_re_added_on_re_enrichment(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A summary the user deletes (pins away) stays suppressed across re-enrichment.

    Same mechanism as tag/entity (lode-npx.4): delete_annotation converts the
    row to a source='user' tombstone; _write_enrichment must see that
    tombstone and skip re-inserting an AI summary with the exact same text.
    """
    _insert_note(conn)
    result = EnrichmentResult(
        tags=[], entities=[], inferred_edges=[], summary="A note about auth."
    )
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    # User deletes (pins away) the AI summary.
    (row_id,) = conn.execute(
        "SELECT id FROM annotations WHERE kind = 'summary'"
    ).fetchone()
    delete_annotation(conn, row_id)

    # Note is edited and re-enriched; Haiku proposes the exact same summary text.
    _update_note(
        conn, version_id="ver-2", parent_version_id="ver-1", body="updated body"
    )
    enrich_version(
        conn, "ver-2", settings, provider=AnthropicProvider(_fake_client(result))
    )

    ai_summaries = conn.execute(
        "SELECT payload FROM annotations WHERE kind = 'summary' AND source = 'ai'"
    ).fetchall()
    assert ai_summaries == []
    # The tombstone itself is still there, untouched.
    user_summaries = conn.execute(
        "SELECT status, source_version FROM annotations "
        "WHERE kind = 'summary' AND source = 'user'"
    ).fetchall()
    assert user_summaries == [("orphaned", None)]


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
    enrich_version(
        conn, "ver-1", settings, provider=AnthropicProvider(_fake_client(result))
    )

    (row_id,) = conn.execute(
        "SELECT id FROM edges WHERE to_id = 'jwt-topic'"
    ).fetchone()
    delete_edge(conn, row_id)

    _update_note(
        conn, version_id="ver-2", parent_version_id="ver-1", body="updated body"
    )
    enrich_version(
        conn, "ver-2", settings, provider=AnthropicProvider(_fake_client(result))
    )

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


# lode-b4w.3: tombstone/purged/no_egress each seed one static disqualifying
# note attribute with an identical "insert, assert 0 gaps" shape -- verified
# by reading _enrich_gap_step's WHERE clause, not just name matching.
# Parametrized over the insert kwarg, 3 tests -> 1. test_enrich_gap_skips_live_job
# below is NOT folded in: it inserts a *job* row (a materially different
# setup, not a note attribute), per lode-b4w.1's explicit caution to verify
# before merging it into this table.
@pytest.mark.parametrize(
    "insert_kwargs",
    [
        pytest.param({"op": "delete"}, id="tombstone"),
        pytest.param({"purged_at": "2026-01-01T00:00:00.000Z"}, id="purged"),
        pytest.param({"no_egress": 1}, id="no_egress"),
    ],
)
def test_enrich_gap_skips_disqualifying_note(
    conn: sqlite3.Connection, insert_kwargs: dict
) -> None:
    """Soft-deleted, purged, or no_egress head versions are not enqueued."""
    _insert_note(conn, **insert_kwargs)
    assert _enrich_gap_step(conn) == 0


def test_enrich_gap_skips_live_job(conn: sqlite3.Connection) -> None:
    """A version with a live (pending/running/done/failed) enrich job is not re-enqueued."""
    _insert_note(conn)
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, next_attempt_at) "
            "VALUES ('enrich', 'ver-1', ?)",
            (now_iso(),),
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
            "INSERT INTO jobs (type, target_version, status, batch_handle, next_attempt_at) "
            "VALUES ('enrich', 'ver-1', 'running', 'batch-abc123', ?)",
            (now_iso(),),
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
            "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
            "VALUES ('enrich', 'ver-1', 'dead', ?)",
            (now_iso(),),
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
# Enrich-gap: prompt/model-change re-enqueue via the job's own prompt_ver
# (lode-0wj.9, re-keyed off the job row itself in lode-q47)
# ---------------------------------------------------------------------------


def _insert_done_enrich_job(
    conn: sqlite3.Connection,
    *,
    target_version: str = "ver-1",
    prompt_ver: str | None = None,
) -> None:
    """Insert a ``status='done'`` enrich job row with the given ``prompt_ver``.

    ``prompt_ver=None`` (the default) reproduces a job that completed before
    lode-q47 stamped it, or one the schema left NULL at enqueue time.
    """
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status, prompt_ver, next_attempt_at) "
            "VALUES ('enrich', ?, 'done', ?, ?)",
            (target_version, prompt_ver, now_iso()),
        )


def test_enrich_gap_done_job_missing_prompt_ver_is_gap(
    conn: sqlite3.Connection,
) -> None:
    """A 'done' enrich job whose own prompt_ver is NULL is still a gap.

    Covers a job that completed before lode-q47 ever stamped prompt_ver: the
    job is 'done' but its prompt_ver column proves nothing about which prompt
    version produced it, so reconcile must re-enqueue.
    """
    _insert_note(conn)
    _insert_done_enrich_job(conn, prompt_ver=None)
    count = _enrich_gap_step(conn)
    assert count == 1
    statuses = conn.execute(
        "SELECT status FROM jobs WHERE type = 'enrich' AND target_version = 'ver-1'"
        " ORDER BY id"
    ).fetchall()
    assert ("done",) in statuses
    assert ("pending",) in statuses


def test_enrich_gap_done_job_with_current_prompt_ver_is_not_a_gap(
    conn: sqlite3.Connection,
) -> None:
    """A 'done' job whose own prompt_ver already matches the current
    ENRICH_PROMPT_VER is NOT a gap -- reconcile must not re-enqueue
    enrichment that is already current."""
    _insert_note(conn)
    _insert_done_enrich_job(conn, prompt_ver=ENRICH_PROMPT_VER)
    assert _enrich_gap_step(conn) == 0
    # No new job was enqueued.
    rows = conn.execute(
        "SELECT status FROM jobs WHERE type = 'enrich' AND target_version = 'ver-1'"
    ).fetchall()
    assert rows == [("done",)]


def test_enrich_gap_reenqueues_on_stale_prompt_ver(
    conn: sqlite3.Connection,
) -> None:
    """A 'done' job stamped with an OLD prompt_ver is a gap.

    This is the prompt/model-change signal: bumping ENRICH_PROMPT_VER makes
    every note whose job predates the bump look like a gap again, so the
    reconcile scan drives corpus-wide re-enrichment.
    """
    _insert_note(conn)
    _insert_done_enrich_job(conn, prompt_ver="some-older-prompt-ver")
    count = _enrich_gap_step(conn)
    assert count == 1
    statuses = conn.execute(
        "SELECT status FROM jobs WHERE type = 'enrich' AND target_version = 'ver-1'"
        " ORDER BY id"
    ).fetchall()
    assert ("done",) in statuses
    assert ("pending",) in statuses


def test_enrich_gap_empty_summary_with_current_prompt_ver_is_not_a_gap(
    conn: sqlite3.Connection,
) -> None:
    """Regression test for the lode-q47 thrash bug.

    A 'done' job stamped with the CURRENT prompt_ver is not a gap even when
    the head has NO summary annotation at all -- e.g. Haiku legitimately
    returned an empty summary for a content-free note, so _write_enrichment
    wrote no 'summary' row (mirrors an empty tag/entity list). Before
    lode-q47 the gap signal read the annotations table instead of the job's
    own prompt_ver, so this exact case re-enqueued a fresh Haiku call on
    every reconcile tick, forever.
    """
    _insert_note(conn)
    _insert_done_enrich_job(conn, prompt_ver=ENRICH_PROMPT_VER)
    # Deliberately no summary annotation inserted at all.
    assert _enrich_gap_step(conn) == 0
    rows = conn.execute(
        "SELECT status FROM jobs WHERE type = 'enrich' AND target_version = 'ver-1'"
    ).fetchall()
    assert rows == [("done",)]


def test_enrich_gap_pending_job_not_reenqueued_regardless_of_prompt_ver(
    conn: sqlite3.Connection,
) -> None:
    """An in-flight (pending) job is left alone regardless of its prompt_ver --
    it will land soon and stamp its own prompt_ver then."""
    _insert_note(conn)
    with conn:
        conn.execute(
            "INSERT INTO jobs (type, target_version, status, prompt_ver, next_attempt_at) "
            "VALUES ('enrich', 'ver-1', 'pending', 'some-older-prompt-ver', ?)",
            (now_iso(),),
        )
    assert _enrich_gap_step(conn) == 0


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
            "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
            "VALUES ('enrich', ?, ?, ?)",
            (version_id, status, now_iso()),
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
    result_id = submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )
    assert result_id == "batch-xyz"


def test_submit_enrich_batch_passes_anthropic_call_timeout_to_create(
    conn: sqlite3.Connection,
) -> None:
    """create() is bounded by Settings.llm_call_timeout_s (lode-olmi.15) --
    the network call that commits the spend must not be able to hang forever
    with no client-side timeout at all.
    """
    _insert_note(conn)
    job_id = _insert_enrich_job(conn)

    settings = Settings(llm_call_timeout_s=42.0)
    client = _fake_batch_client(batch_id="batch-xyz")
    submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )

    create_kwargs = client.beta.messages.batches.create.call_args.kwargs
    assert create_kwargs["timeout"] == 42.0


def test_submit_enrich_batch_uses_the_raised_max_tokens(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """The batch request's per-item params send the same enrich.MAX_TOKENS the
    immediate path does (lode-568v.2's byte-for-byte bar). The raise itself is
    pinned by test_enrich_version_uses_the_raised_max_tokens.
    """
    _insert_note(conn)
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client(batch_id="batch-xyz")
    submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )

    create_kwargs = client.beta.messages.batches.create.call_args.kwargs
    (request,) = create_kwargs["requests"]
    assert request["params"]["max_tokens"] == MAX_TOKENS


def test_submit_enrich_batch_max_tokens_override_reaches_the_call(
    conn: sqlite3.Connection,
) -> None:
    """lode-d70n: a Kind.RUNTIME override of enrichment_llm.max_tokens must
    reach the batch route too -- the realistic-failure-mode route named in
    enrich.MAX_TOKENS's own docstring, and the one with no other escape hatch.
    """
    _insert_note(conn)
    job_id = _insert_enrich_job(conn)

    settings = Settings(
        enrichment_llm=ModelTier(model="claude-haiku-4-5", max_tokens=555)
    )
    client = _fake_batch_client(batch_id="batch-xyz")
    submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )

    create_kwargs = client.beta.messages.batches.create.call_args.kwargs
    (request,) = create_kwargs["requests"]
    assert request["params"]["max_tokens"] == 555


def test_submit_enrich_batch_stores_batch_handle(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Submitted jobs are marked running with batch_handle set."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client(batch_id="batch-handle-test")
    submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )

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
    result_id = submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )

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
    result_id = submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )

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
    result_id = submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )

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
    result_id = submit_enrich_batch(
        conn, [], settings, provider=AnthropicProvider(client)
    )

    assert result_id is None
    client.beta.messages.batches.create.assert_not_called()


def test_submit_enrich_batch_writes_egress_log(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A single egress_log row is written for the batch submission."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn)

    client = _fake_batch_client(batch_id="batch-egress")
    submit_enrich_batch(
        conn, [(job_id, "ver-1")], settings, provider=AnthropicProvider(client)
    )

    rows = conn.execute(
        "SELECT purpose, model, provider FROM egress_log WHERE purpose = 'enrich'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == settings.enrichment_llm.model
    # lode-568v.4: NULL means "anthropic" by convention.
    assert rows[0][2] is None


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
        conn,
        [(job1, "ver-1"), (job2, "ver-2")],
        settings,
        provider=AnthropicProvider(client),
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
# submit_enrich_batch -- external snapshot targets (lode-7qi)
#
# The Batches API pre-step is the route a gate_reenrich-enqueued 'enrich' job
# actually takes in production (it runs ahead of the immediate-handler
# fallback), so the same polymorphic gate must hold here too.
# ---------------------------------------------------------------------------


def test_submit_enrich_batch_includes_snapshot_target(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A snapshot_id job is resolved, redacted, and included in the batch."""
    _insert_external(conn, external_id="ext-1", snapshot_id="snap-1")
    job_id = _insert_enrich_job(conn, "snap-1")

    client = _fake_batch_client(batch_id="batch-snap")
    result_id = submit_enrich_batch(
        conn, [(job_id, "snap-1")], settings, provider=AnthropicProvider(client)
    )

    assert result_id == "batch-snap"
    call_kwargs = client.beta.messages.batches.create.call_args
    requests = call_kwargs.kwargs.get("requests") or call_kwargs.args[0]
    assert {r["custom_id"] for r in requests} == {"snap-1"}

    row = conn.execute(
        "SELECT status, batch_handle FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row == ("running", "batch-snap")


def test_submit_enrich_batch_skips_no_egress_external(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A no_egress external's snapshot job is marked done without an API call."""
    _insert_external(conn, no_egress=1)
    job_id = _insert_enrich_job(conn, "snap-1")

    client = _fake_batch_client()
    result_id = submit_enrich_batch(
        conn, [(job_id, "snap-1")], settings, provider=AnthropicProvider(client)
    )

    assert result_id is None
    client.beta.messages.batches.create.assert_not_called()
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "done"


def test_submit_enrich_batch_skips_tombstone_snapshot(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A tombstone snapshot job is marked done without an API call."""
    _insert_external(conn, status="tombstone")
    job_id = _insert_enrich_job(conn, "snap-1")

    client = _fake_batch_client()
    result_id = submit_enrich_batch(
        conn, [(job_id, "snap-1")], settings, provider=AnthropicProvider(client)
    )

    assert result_id is None
    client.beta.messages.batches.create.assert_not_called()
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "done"


def test_submit_enrich_batch_mixed_note_and_snapshot_targets(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A single batch can include both a note version and an external snapshot."""
    _insert_note(conn, note_id="note-1", version_id="ver-1")
    _insert_external(conn, external_id="ext-1", snapshot_id="snap-1")
    job1 = _insert_enrich_job(conn, "ver-1")
    job2 = _insert_enrich_job(conn, "snap-1")

    client = _fake_batch_client(batch_id="batch-mixed")
    submit_enrich_batch(
        conn,
        [(job1, "ver-1"), (job2, "snap-1")],
        settings,
        provider=AnthropicProvider(client),
    )

    call_kwargs = client.beta.messages.batches.create.call_args
    requests = call_kwargs.kwargs.get("requests") or call_kwargs.args[0]
    assert {r["custom_id"] for r in requests} == {"ver-1", "snap-1"}


# ---------------------------------------------------------------------------
# Batch API helpers — collect_enrich_batch (lode-npx.2)
# ---------------------------------------------------------------------------


def test_collect_enrich_batch_returns_false_when_in_progress(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """collect_enrich_batch returns False when the batch is still in progress."""
    client = _fake_batch_client(processing_status="in_progress")
    ended = collect_enrich_batch(
        conn, "batch-in-flight", settings, provider=AnthropicProvider(client)
    )
    assert ended is False


def test_collect_enrich_batch_passes_anthropic_call_timeout_to_retrieve_and_results(
    conn: sqlite3.Connection,
) -> None:
    """retrieve()/results() are bounded by Settings.llm_call_timeout_s
    (lode-olmi.15) -- with no client-side timeout either call could otherwise
    hang forever with no signal back to 'lode work'.
    """
    _insert_note(conn)
    job_id = _insert_enrich_job(conn, "ver-1", status="running")
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_handle = 'batch-done' WHERE id = ?", (job_id,)
        )
    settings = Settings(llm_call_timeout_s=42.0)
    client = _fake_batch_client(results=[])
    collect_enrich_batch(
        conn, "batch-done", settings, provider=AnthropicProvider(client)
    )

    retrieve_kwargs = client.beta.messages.batches.retrieve.call_args.kwargs
    assert retrieve_kwargs["timeout"] == 42.0
    results_kwargs = client.beta.messages.batches.results.call_args.kwargs
    assert results_kwargs["timeout"] == 42.0


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

    ended = collect_enrich_batch(
        conn, "batch-done", settings, provider=AnthropicProvider(client)
    )
    assert ended is True

    # Job marked done and stamped with the current prompt_ver (lode-q47) --
    # the signal lode.reconcile's enrich-gap step reads to decide freshness.
    (status, prompt_ver) = conn.execute(
        "SELECT status, prompt_ver FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "done"
    assert prompt_ver == ENRICH_PROMPT_VER

    # Enrichment written to DB.
    ann_rows = conn.execute(
        "SELECT kind, payload, provider FROM annotations WHERE source_version = 'ver-1'"
    ).fetchall()
    kinds = {r[0] for r in ann_rows}
    assert "tag" in kinds
    assert "entity" in kinds
    # lode-568v.4: NULL means "anthropic" by convention, on the batch route too.
    assert all(r[2] is None for r in ann_rows)

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

    ended = collect_enrich_batch(
        conn, "batch-err", settings, provider=AnthropicProvider(client)
    )
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

    collect_enrich_batch(
        conn, "batch-dead", settings_low, provider=AnthropicProvider(client)
    )

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
    ended = collect_enrich_batch(
        conn, "batch-xyz", settings, provider=AnthropicProvider(client)
    )
    # Batch is ended (processing_status='ended' is the default) so True.
    assert ended is True


# ---------------------------------------------------------------------------
# collect_enrich_batch -- external snapshot targets (lode-7qi)
# ---------------------------------------------------------------------------


def test_collect_enrich_batch_writes_enrichment_against_external_id(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A succeeded result for a snapshot_id job writes annotations/edges
    against the external_id, not a note_id -- the same polymorphic owner
    resolution :func:`enrich_version` uses (lode-7qi)."""
    _insert_external(conn, external_id="ext-1", snapshot_id="snap-1")
    job_id = _insert_enrich_job(conn, "snap-1", status="running")
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_handle = 'batch-snap-done' WHERE id = ?",
            (job_id,),
        )

    enrichment = EnrichmentResult(
        tags=["python"],
        entities=["FastAPI"],
        inferred_edges=[
            InferredEdge(
                to_id="web-frameworks", reason="mentions FastAPI", confidence=0.9
            )
        ],
    )
    br = _make_batch_result("snap-1", enrichment)
    client = _fake_batch_client(batch_id="batch-snap-done", results=[br])

    ended = collect_enrich_batch(
        conn, "batch-snap-done", settings, provider=AnthropicProvider(client)
    )
    assert ended is True

    (status, prompt_ver) = conn.execute(
        "SELECT status, prompt_ver FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "done"
    assert prompt_ver == ENRICH_PROMPT_VER

    targets = {
        r[0]
        for r in conn.execute(
            "SELECT target FROM annotations WHERE source_version = 'snap-1'"
        ).fetchall()
    }
    assert targets == {"ext-1"}

    edge_rows = conn.execute(
        "SELECT from_id, to_id FROM edges WHERE source_version = 'snap-1'"
    ).fetchall()
    assert edge_rows == [("ext-1", "web-frameworks")]


# ---------------------------------------------------------------------------
# CLI outcome formatting (lode-1gr.4)
# ---------------------------------------------------------------------------


def test_format_enrich_outcome_wording() -> None:
    """format_enrich_outcome renders the exact 'lode work' echo wording."""
    result = EnrichmentResult(
        tags=["python", "api", "web", "backend"],
        entities=["FastAPI", "Pydantic"],
        inferred_edges=[
            InferredEdge(to_id="a", reason="r1", confidence=0.5),
            InferredEdge(to_id="b", reason="r2", confidence=0.6),
            InferredEdge(to_id="c", reason="r3", confidence=0.7),
        ],
        summary="A note about FastAPI.",
    )
    line = format_enrich_outcome("abcdef0123456789", result)
    assert line == ("enriched abcdef012345: 4 tags, 2 entities, 3 edges, summary set")


def test_format_enrich_outcome_empty_summary() -> None:
    """An empty summary renders 'summary empty', not 'summary set'."""
    result = EnrichmentResult(tags=["x"], summary="")
    line = format_enrich_outcome("ver-1", result)
    assert "summary empty" in line


def test_collect_enrich_batch_appends_outcome_line_on_success(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A succeeded batch result appends a format_enrich_outcome line (lode-1gr.4).

    This is the channel a *later* drain pass that collects a completed enrich
    batch uses to surface a per-note outcome to 'lode work' -- the batch
    pre-step runs ahead of lode.worker.drain's main claim/run loop, so this is
    the only place those outcomes are observable.
    """
    _insert_note(conn)
    job_id = _insert_enrich_job(conn, "ver-1", status="running")
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_handle = 'batch-outcome' WHERE id = ?", (job_id,)
        )

    enrichment = EnrichmentResult(tags=["python", "api"], entities=["FastAPI"])
    br = _make_batch_result("ver-1", enrichment)
    client = _fake_batch_client(batch_id="batch-outcome", results=[br])

    outcomes: list[str] = []
    ended = collect_enrich_batch(
        conn,
        "batch-outcome",
        settings,
        provider=AnthropicProvider(client),
        outcomes=outcomes,
    )
    assert ended is True
    assert outcomes == [format_enrich_outcome("ver-1", enrichment)]


def test_collect_enrich_batch_no_outcome_on_errored_result(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """An errored batch result appends no outcome line -- only successes do."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn, "ver-1", status="running")
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_handle = 'batch-err-outcome' WHERE id = ?",
            (job_id,),
        )

    br = _make_batch_result("ver-1", EnrichmentResult(), result_type="errored")
    client = _fake_batch_client(batch_id="batch-err-outcome", results=[br])

    outcomes: list[str] = []
    collect_enrich_batch(
        conn,
        "batch-err-outcome",
        settings,
        provider=AnthropicProvider(client),
        outcomes=outcomes,
    )
    assert outcomes == []


def test_collect_enrich_batch_outcomes_default_none_is_a_no_op(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Omitting outcomes (the default) does not error -- purely additive param."""
    _insert_note(conn)
    job_id = _insert_enrich_job(conn, "ver-1", status="running")
    with conn:
        conn.execute(
            "UPDATE jobs SET batch_handle = 'batch-no-sink' WHERE id = ?", (job_id,)
        )

    enrichment = EnrichmentResult(tags=["x"])
    br = _make_batch_result("ver-1", enrichment)
    client = _fake_batch_client(batch_id="batch-no-sink", results=[br])

    ended = collect_enrich_batch(
        conn, "batch-no-sink", settings, provider=AnthropicProvider(client)
    )
    assert ended is True
