"""Tests for lode.staleness — structural staleness + re-anchor rules (lode-npx.3).

Acceptance criteria (bd show lode-npx.3):
- When the head moves past source_version the annotation reads stale.
- On update: unchanged quote stays fresh, changed quote is stale, missing quote
  is orphaned.
- Span anchors by quoted text + version, never offsets.
- User annotations/edges (source='user') are never re-anchored.

Strategy: all tests use a real SQLite DB (init_db) to exercise actual schema
constraints. No Anthropic API calls are made — annotations and edges are
inserted directly to test the re-anchor logic in isolation.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from lode.staleness import reanchor_annotations, reanchor_edges
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


def _insert_note(
    conn: sqlite3.Connection,
    note_id: str = "note-1",
    version_id: str = "ver-1",
    body: str = "hello world",
) -> None:
    """Insert a minimal notes + versions row pair and set the head pointer."""
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


def _insert_annotation(
    conn: sqlite3.Connection,
    *,
    target: str = "note-1",
    source_version: str = "ver-1",
    kind: str = "tag",
    payload_value: str = "python",
    source: str = "ai",
    status: str = "fresh",
    quoted_text: str | None = None,
) -> int:
    """Insert one annotation row; payload is JSON-encoded from payload_value."""
    with conn:
        cur = conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status, quoted_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                target,
                source_version,
                kind,
                json.dumps(payload_value),
                source,
                status,
                quoted_text,
            ),
        )
        return cur.lastrowid  # type: ignore[return-value]


def _insert_edge(
    conn: sqlite3.Connection,
    *,
    from_id: str = "note-1",
    to_id: str = "jwt-topic",
    source: str = "ai",
    source_version: str = "ver-1",
    status: str = "fresh",
    quoted_text: str | None = None,
) -> int:
    """Insert one edge row."""
    with conn:
        cur = conn.execute(
            "INSERT INTO edges "
            "(from_id, to_id, source, reason, confidence, source_version, "
            "status, quoted_text) "
            "VALUES (?, ?, ?, 'test reason', 0.8, ?, ?, ?)",
            (from_id, to_id, source, source_version, status, quoted_text),
        )
        return cur.lastrowid  # type: ignore[return-value]


def _annotation_row(conn: sqlite3.Connection, row_id: int) -> dict:
    """Fetch a single annotation row as a dict."""
    r = conn.execute(
        "SELECT status, source_version FROM annotations WHERE id = ?", (row_id,)
    ).fetchone()
    return {"status": r[0], "source_version": r[1]}


def _edge_row(conn: sqlite3.Connection, row_id: int) -> dict:
    """Fetch a single edge row as a dict."""
    r = conn.execute(
        "SELECT status, source_version FROM edges WHERE id = ?", (row_id,)
    ).fetchone()
    return {"status": r[0], "source_version": r[1]}


# ---------------------------------------------------------------------------
# Schema: quoted_text column migration
# ---------------------------------------------------------------------------


def test_quoted_text_column_exists_in_annotations(conn: sqlite3.Connection) -> None:
    """quoted_text column is present on the annotations table after init_db."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(annotations)").fetchall()}
    assert "quoted_text" in cols, "annotations.quoted_text must exist after init_db"


def test_quoted_text_column_exists_in_edges(conn: sqlite3.Connection) -> None:
    """quoted_text column is present on the edges table after init_db."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
    assert "quoted_text" in cols, "edges.quoted_text must exist after init_db"


def test_migration_idempotent(tmp_path: Path) -> None:
    """Calling init_db twice on the same file (migration already applied) does not raise."""
    db = tmp_path / "lode.db"
    init_db(db).close()
    # Second call: ALTER TABLE will fail silently (column already present).
    conn2 = init_db(db)
    try:
        cols = {
            r[1] for r in conn2.execute("PRAGMA table_info(annotations)").fetchall()
        }
        assert "quoted_text" in cols
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# reanchor_annotations — with quoted_text set
# ---------------------------------------------------------------------------


def test_reanchor_annotations_unchanged_quote_is_fresh(
    conn: sqlite3.Connection,
) -> None:
    """Verbatim quoted_text match in new body → fresh, source_version advanced.

    Acceptance criterion: unchanged quote stays fresh on update.
    """
    _insert_note(conn, body="The quick brown fox")
    row_id = _insert_annotation(
        conn,
        source_version="ver-1",
        payload_value="quick",
        quoted_text="quick brown fox",
    )

    reanchor_annotations(conn, "note-1", "ver-2", "The quick brown fox jumped")

    row = _annotation_row(conn, row_id)
    assert row["status"] == "fresh"
    assert row["source_version"] == "ver-2", "source_version must advance when fresh"


def test_reanchor_annotations_changed_context_is_stale(
    conn: sqlite3.Connection,
) -> None:
    """quoted_text gone but payload value still in body → stale, source_version NOT advanced.

    Acceptance criterion: changed quote is stale on update.
    """
    _insert_note(conn, body="quick brown fox")
    row_id = _insert_annotation(
        conn,
        source_version="ver-1",
        payload_value="fox",
        # Original context: "quick brown fox"; new body will keep "fox" but lose the context
        quoted_text="quick brown fox",
    )

    # New body: quoted_text is gone but the payload value "fox" is still present.
    reanchor_annotations(conn, "note-1", "ver-2", "lazy fox")

    row = _annotation_row(conn, row_id)
    assert row["status"] == "stale"
    assert row["source_version"] == "ver-1", (
        "source_version must NOT advance when stale"
    )


def test_reanchor_annotations_missing_both_is_orphaned(
    conn: sqlite3.Connection,
) -> None:
    """quoted_text absent and payload value absent → orphaned.

    Acceptance criterion: missing quote is orphaned on update.
    """
    _insert_note(conn, body="Note about Python")
    row_id = _insert_annotation(
        conn,
        source_version="ver-1",
        payload_value="python",
        quoted_text="Note about Python",
    )

    # New body: both the quoted_text and "python" are gone.
    reanchor_annotations(conn, "note-1", "ver-2", "Completely different topic")

    row = _annotation_row(conn, row_id)
    assert row["status"] == "orphaned"
    assert row["source_version"] == "ver-1", (
        "source_version must NOT advance when orphaned"
    )


# ---------------------------------------------------------------------------
# reanchor_annotations — without quoted_text (fallback)
# ---------------------------------------------------------------------------


def test_reanchor_annotations_no_quoted_text_payload_present_is_fresh(
    conn: sqlite3.Connection,
) -> None:
    """No quoted_text: payload value in body → fresh, source_version advanced."""
    _insert_note(conn)
    row_id = _insert_annotation(
        conn,
        source_version="ver-1",
        payload_value="python",
        quoted_text=None,
    )

    reanchor_annotations(conn, "note-1", "ver-2", "A note about python and django")

    row = _annotation_row(conn, row_id)
    assert row["status"] == "fresh"
    assert row["source_version"] == "ver-2"


def test_reanchor_annotations_no_quoted_text_payload_absent_is_orphaned(
    conn: sqlite3.Connection,
) -> None:
    """No quoted_text: payload value absent from body → orphaned."""
    _insert_note(conn)
    row_id = _insert_annotation(
        conn,
        source_version="ver-1",
        payload_value="python",
        quoted_text=None,
    )

    reanchor_annotations(conn, "note-1", "ver-2", "A note about Rust and WASM")

    row = _annotation_row(conn, row_id)
    assert row["status"] == "orphaned"
    assert row["source_version"] == "ver-1"


# ---------------------------------------------------------------------------
# reanchor_annotations — user annotations are never touched
# ---------------------------------------------------------------------------


def test_reanchor_annotations_user_source_not_touched(
    conn: sqlite3.Connection,
) -> None:
    """User annotations (source='user') are never re-anchored, even if content changes.

    User curation is irreplaceable and attaches to the logical note, not a version.
    """
    _insert_note(conn)
    row_id = _insert_annotation(
        conn,
        source="user",
        source_version="ver-1",
        payload_value="important",
        status="fresh",
        quoted_text="important word",
    )

    # New body: both the quote and "important" are gone.
    reanchor_annotations(conn, "note-1", "ver-2", "Completely different content")

    row = _annotation_row(conn, row_id)
    # User annotation must be untouched.
    assert row["status"] == "fresh", "user annotation status must not change"
    assert row["source_version"] == "ver-1", (
        "user annotation source_version must not change"
    )


# ---------------------------------------------------------------------------
# reanchor_annotations — return value (counts)
# ---------------------------------------------------------------------------


def test_reanchor_annotations_returns_counts(conn: sqlite3.Connection) -> None:
    """reanchor_annotations returns a count dict with fresh/stale/orphaned totals."""
    _insert_note(conn)
    # fresh: quoted_text is verbatim in the new body
    _insert_annotation(conn, payload_value="python", quoted_text="python language")
    # stale: quoted_text absent but payload "python" still in new body
    _insert_annotation(conn, payload_value="python", quoted_text="python auth tutorial")
    # orphaned: quoted_text absent and payload "rust" not in new body
    _insert_annotation(conn, payload_value="rust", quoted_text="rust programming")

    counts = reanchor_annotations(conn, "note-1", "ver-2", "python language basics")

    assert counts["fresh"] == 1
    assert counts["stale"] == 1
    assert counts["orphaned"] == 1


def test_reanchor_annotations_empty_returns_zeros(conn: sqlite3.Connection) -> None:
    """No annotations on the note → all-zero count dict."""
    _insert_note(conn)
    counts = reanchor_annotations(conn, "note-1", "ver-2", "some body text")
    assert counts == {"fresh": 0, "stale": 0, "orphaned": 0}


# ---------------------------------------------------------------------------
# reanchor_edges — with quoted_text set
# ---------------------------------------------------------------------------


def test_reanchor_edges_unchanged_quote_is_fresh(conn: sqlite3.Connection) -> None:
    """Verbatim quoted_text match in new body → fresh, source_version advanced."""
    _insert_note(conn, body="This discusses JWT token authentication")
    row_id = _insert_edge(
        conn,
        to_id="jwt-topic",
        source_version="ver-1",
        quoted_text="JWT token authentication",
    )

    reanchor_edges(
        conn, "note-1", "ver-2", "This discusses JWT token authentication flows"
    )

    row = _edge_row(conn, row_id)
    assert row["status"] == "fresh"
    assert row["source_version"] == "ver-2"


def test_reanchor_edges_changed_context_is_stale(conn: sqlite3.Connection) -> None:
    """quoted_text gone but to_id still in body → stale, source_version NOT advanced."""
    _insert_note(conn, body="JWT token authentication")
    row_id = _insert_edge(
        conn,
        to_id="jwt-topic",
        source_version="ver-1",
        quoted_text="JWT token authentication",
    )

    # New body: quoted_text gone but "jwt-topic" appears literally.
    reanchor_edges(conn, "note-1", "ver-2", "See also jwt-topic for more details")

    row = _edge_row(conn, row_id)
    assert row["status"] == "stale"
    assert row["source_version"] == "ver-1"


def test_reanchor_edges_missing_both_is_orphaned(conn: sqlite3.Connection) -> None:
    """quoted_text absent and to_id absent → orphaned."""
    _insert_note(conn, body="JWT token authentication")
    row_id = _insert_edge(
        conn,
        to_id="jwt-topic",
        source_version="ver-1",
        quoted_text="JWT token authentication",
    )

    # New body: both the quoted_text and "jwt-topic" are gone.
    reanchor_edges(conn, "note-1", "ver-2", "Database indexing strategies")

    row = _edge_row(conn, row_id)
    assert row["status"] == "orphaned"
    assert row["source_version"] == "ver-1"


# ---------------------------------------------------------------------------
# reanchor_edges — without quoted_text (fallback)
# ---------------------------------------------------------------------------


def test_reanchor_edges_no_quoted_text_to_id_present_is_fresh(
    conn: sqlite3.Connection,
) -> None:
    """No quoted_text: to_id value in body → fresh, source_version advanced."""
    _insert_note(conn)
    row_id = _insert_edge(
        conn, to_id="security", source_version="ver-1", quoted_text=None
    )

    reanchor_edges(conn, "note-1", "ver-2", "This covers security and auth patterns")

    row = _edge_row(conn, row_id)
    assert row["status"] == "fresh"
    assert row["source_version"] == "ver-2"


def test_reanchor_edges_no_quoted_text_to_id_absent_is_orphaned(
    conn: sqlite3.Connection,
) -> None:
    """No quoted_text: to_id absent from body → orphaned."""
    _insert_note(conn)
    row_id = _insert_edge(
        conn, to_id="security", source_version="ver-1", quoted_text=None
    )

    reanchor_edges(conn, "note-1", "ver-2", "This covers performance tuning")

    row = _edge_row(conn, row_id)
    assert row["status"] == "orphaned"
    assert row["source_version"] == "ver-1"


# ---------------------------------------------------------------------------
# reanchor_edges — user edges are never touched
# ---------------------------------------------------------------------------


def test_reanchor_edges_user_source_not_touched(conn: sqlite3.Connection) -> None:
    """User edges (source='user') are never re-anchored."""
    _insert_note(conn)
    row_id = _insert_edge(
        conn,
        to_id="auth-notes",
        source="user",
        source_version="ver-1",
        status="fresh",
        quoted_text="authentication flows",
    )

    # New body: both quote and "auth-notes" are gone.
    reanchor_edges(conn, "note-1", "ver-2", "Completely different content")

    row = _edge_row(conn, row_id)
    assert row["status"] == "fresh", "user edge status must not change"
    assert row["source_version"] == "ver-1", "user edge source_version must not change"


# ---------------------------------------------------------------------------
# reanchor_edges — return value and empty case
# ---------------------------------------------------------------------------


def test_reanchor_edges_returns_counts(conn: sqlite3.Connection) -> None:
    """reanchor_edges returns a count dict with fresh/stale/orphaned totals."""
    _insert_note(conn)
    # fresh: quoted_text "OAuth2 flow" is verbatim in new body
    _insert_edge(conn, to_id="oauth", source_version="ver-1", quoted_text="OAuth2 flow")
    # stale: quoted_text "old OAuth2 context" absent, but to_id "security" IS in body
    _insert_edge(
        conn, to_id="security", source_version="ver-1", quoted_text="old OAuth2 context"
    )
    # orphaned: both quoted_text "gone text" and to_id "missing-concept" absent
    _insert_edge(
        conn, to_id="missing-concept", source_version="ver-1", quoted_text="gone text"
    )

    counts = reanchor_edges(
        conn, "note-1", "ver-2", "OAuth2 flow and security patterns"
    )

    assert counts["fresh"] == 1
    assert counts["stale"] == 1
    assert counts["orphaned"] == 1


def test_reanchor_edges_empty_returns_zeros(conn: sqlite3.Connection) -> None:
    """No edges from the note → all-zero count dict."""
    _insert_note(conn)
    counts = reanchor_edges(conn, "note-1", "ver-2", "some body text")
    assert counts == {"fresh": 0, "stale": 0, "orphaned": 0}


# ---------------------------------------------------------------------------
# Head-moved-past-source_version acceptance criterion
# ---------------------------------------------------------------------------


def test_head_moved_past_source_version_reads_stale(
    conn: sqlite3.Connection,
) -> None:
    """When the head moves past source_version and re-anchor runs, the annotation
    reflects the actual comparison: if the quote is gone the status is not fresh.

    This is the primary acceptance criterion: the head moving past source_version
    causes the annotation to read stale (or orphaned), not fresh.
    """
    _insert_note(conn, version_id="ver-1", body="python auth tutorial")
    # Annotation anchored at ver-1 with a specific quote.
    row_id = _insert_annotation(
        conn,
        source_version="ver-1",
        payload_value="auth",
        quoted_text="python auth tutorial",
        status="fresh",
    )

    # Head moves to ver-2; the quote is no longer verbatim (edited away).
    # "auth" still appears but the exact quote is gone → stale.
    reanchor_annotations(conn, "note-1", "ver-2", "python authentication guide")

    row = _annotation_row(conn, row_id)
    # Quote "python auth tutorial" is gone; "auth" is in "authentication" via substring.
    # The annotation should not be fresh — head moved past source_version.
    assert row["status"] in ("stale", "orphaned"), (
        "annotation must not be fresh when head moves past source_version "
        f"and the exact quote is gone; got {row['status']!r}"
    )
    assert row["source_version"] == "ver-1", (
        "source_version must not advance when annotation is not fresh"
    )
