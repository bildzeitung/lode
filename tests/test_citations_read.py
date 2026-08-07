"""Tests for lode.citations_read -- shared citation identity + as-of resolution
(lode-kuc7, relocated out of lode.tui.services.ask where lode-35nu.1 wrote them).
"""

from pathlib import Path

from lode.answer import Support
from lode.citations_read import CitationIdentity, resolve_citations
from lode.storage import init_db
from lode.versions import save


def test_resolve_citations_reads_version_created_from_store(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "n1", "hello world")
        (created,) = conn.execute(
            "SELECT created FROM versions WHERE version_id = ?", (result.version_id,)
        ).fetchone()

        as_of, _ = resolve_citations(
            conn, [Support(version_id=result.version_id, quoted_span="hello")]
        )
    finally:
        conn.close()

    assert as_of == {result.version_id: created}


def test_resolve_citations_reads_snapshot_fetched_at_from_store(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES ('e1', 'jira')"
        )
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, status, fetched_at) "
            "VALUES ('s1', 'e1', 'status: open', 'ok', '2026-06-01T00:00:00.000Z')"
        )
        conn.commit()

        as_of, _ = resolve_citations(
            conn, [Support(snapshot_id="s1", quoted_span="status: open")]
        )
    finally:
        conn.close()

    assert as_of == {"s1": "2026-06-01T00:00:00.000Z"}


def test_resolve_citations_maps_an_unresolvable_target_to_none(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        as_of, identities = resolve_citations(
            conn, [Support(version_id="nonexistent", quoted_span="x")]
        )
    finally:
        conn.close()

    assert as_of == {"nonexistent": None}
    assert identities == {}


def test_resolve_citations_resolves_head_note_version(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "n1", "First line of the note.\nmore body")

        _, identities = resolve_citations(
            conn, [Support(version_id=result.version_id, quoted_span="First line")]
        )
    finally:
        conn.close()

    assert identities[result.version_id] == CitationIdentity(
        note_id="n1", title="First line of the note.", is_head=True
    )


def test_resolve_citations_marks_a_superseded_version_not_head(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        v1 = save(conn, "n1", "Original body.")
        save(
            conn, "n1", "Updated body.", parent=v1.version_id
        )  # new head; v1 superseded

        _, identities = resolve_citations(
            conn, [Support(version_id=v1.version_id, quoted_span="Original")]
        )
    finally:
        conn.close()

    assert identities[v1.version_id] == CitationIdentity(
        note_id="n1", title="Original body.", is_head=False
    )


def test_resolve_citations_resolves_head_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, head_snapshot_id) "
            "VALUES ('e1', 'web', 's1')"
        )
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, status, fetched_at) "
            "VALUES ('s1', 'e1', ?, 'ok', '2026-06-01T00:00:00.000Z')",
            ("Ticket title\nbody",),
        )
        conn.commit()

        _, identities = resolve_citations(
            conn, [Support(snapshot_id="s1", quoted_span="body")]
        )
    finally:
        conn.close()

    assert identities["s1"] == CitationIdentity(
        external_id="e1", title="Ticket title", is_head=True
    )


def test_resolve_citations_batches_one_query_per_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        v1 = save(conn, "n1", "one")
        v2 = save(conn, "n2", "two")

        executed: list[str] = []
        conn.set_trace_callback(executed.append)

        _, identities = resolve_citations(
            conn,
            [
                Support(version_id=v1.version_id, quoted_span="one"),
                Support(version_id=v2.version_id, quoted_span="two"),
            ],
        )
        conn.set_trace_callback(None)
    finally:
        conn.close()

    assert len(executed) == 1
    assert len(identities) == 2
