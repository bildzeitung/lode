"""Tests for ``lode stats`` (lode-tyhy) -- the read-only corpus-inspection CLI.

Covers the acceptance criteria directly: reason breakdown including the
``[tombstone: X]`` parse (and its "other" fallback for an unrecognized
body), the empty-DB path (renders cleanly, zeros, no crash -- also
exercised smoke-style via ``lode.cli.stats.stats`` in
``tests/test_cli.py``'s subcommand-surface sweep), and the
raw_payload-retained count.
"""

from pathlib import Path

from typer.testing import CliRunner

from lode.cli import app
from lode.externals import ingest_snapshot
from lode.stats_read import (
    empty_extract_raw_payload_retained_count,
    parse_tombstone_reason,
    snapshot_status_counts,
    tombstone_reason_counts,
)
from lode.storage import init_db
from lode.versions import save

runner = CliRunner()


def test_stats_empty_db_renders_cleanly(tmp_path: Path) -> None:
    """Empty DB: zeros throughout, no crash (acceptance criteria)."""
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["stats", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "empty_extract tombstones with raw_payload retained: 0" in result.stdout
    for label in (
        "Notes (total)",
        "Notes (live)",
        "Notes (deleted)",
        "Versions (total)",
        "Egress log (total entries)",
    ):
        row = next(ln for ln in result.stdout.splitlines() if label in ln)
        assert "0" in row


def test_stats_not_registered_on_status(tmp_path: Path) -> None:
    """Deliberately NOT part of `lode status` (ticket's non-goal)."""
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "Snapshots by status" not in result.stdout
    assert "Tombstones by reason" not in result.stdout


def test_parse_tombstone_reason_recognized() -> None:
    assert parse_tombstone_reason("[tombstone: empty_extract]") == "empty_extract"
    assert parse_tombstone_reason("[tombstone: http_403]") == "http_403"
    assert parse_tombstone_reason("[tombstone: too_many_redirects]") == (
        "too_many_redirects"
    )


def test_parse_tombstone_reason_unrecognized_bucket_is_other() -> None:
    """Tolerates a body that isn't the stable `[tombstone: X]` shape (never raises)."""
    assert parse_tombstone_reason("not a tombstone body at all") == "other"
    assert parse_tombstone_reason("") == "other"
    assert parse_tombstone_reason("[tombstone: ]") == "other"


def test_tombstone_reason_counts_breakdown(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        ingest_snapshot(
            conn,
            "https://a.example/1",
            "web",
            "[tombstone: empty_extract]",
            status="tombstone",
        )
        ingest_snapshot(
            conn,
            "https://a.example/2",
            "web",
            "[tombstone: empty_extract]",
            status="tombstone",
        )
        ingest_snapshot(
            conn,
            "https://a.example/3",
            "web",
            "[tombstone: http_403]",
            status="tombstone",
        )
        ingest_snapshot(conn, "https://a.example/4", "web", "live content")
        counts = dict(tombstone_reason_counts(conn))
        assert counts == {"empty_extract": 2, "http_403": 1}

        status_counts = dict(snapshot_status_counts(conn))
        assert status_counts == {"ok": 1, "tombstone": 3}
    finally:
        conn.close()


def test_stats_cli_renders_tombstone_breakdown_with_oni_annotation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        ingest_snapshot(
            conn,
            "https://a.example/1",
            "web",
            "[tombstone: empty_extract]",
            status="tombstone",
        )
        ingest_snapshot(
            conn,
            "https://a.example/2",
            "web",
            "[tombstone: http_403]",
            status="tombstone",
        )
    finally:
        conn.close()

    result = runner.invoke(app, ["stats", "--db", str(db_path)])
    assert result.exit_code == 0
    empty_extract_row = next(
        ln for ln in result.stdout.splitlines() if "empty_extract" in ln
    )
    assert "lode-oni candidate" in empty_extract_row
    assert "1" in empty_extract_row
    http_row = next(ln for ln in result.stdout.splitlines() if "http_403" in ln)
    assert "lode-oni candidate" not in http_row


def test_empty_extract_raw_payload_retained_count(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        # Retained: empty_extract with raw_payload.
        ingest_snapshot(
            conn,
            "https://a.example/1",
            "web",
            "[tombstone: empty_extract]",
            status="tombstone",
            raw_payload="<html>js scaffold</html>",
        )
        # Not retained: empty_extract with no raw_payload.
        ingest_snapshot(
            conn,
            "https://a.example/2",
            "web",
            "[tombstone: empty_extract]",
            status="tombstone",
        )
        # A different reason with raw_payload -- must not be counted.
        ingest_snapshot(
            conn,
            "https://a.example/3",
            "web",
            "[tombstone: http_403]",
            status="tombstone",
            raw_payload="<html>forbidden</html>",
        )
        assert empty_extract_raw_payload_retained_count(conn) == 1
    finally:
        conn.close()

    result = runner.invoke(app, ["stats", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "empty_extract tombstones with raw_payload retained: 1" in result.stdout


def test_stats_notes_and_edges_overview(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-live", "a live note")
        head = save(conn, "note-dead", "will be deleted")
        from lode.versions import delete

        delete(conn, "note-dead", parent=head.version_id)

        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, status) VALUES (?, ?, ?, ?)",
            ("note-live", "note-dead", "user", "fresh"),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["stats", "--db", str(db_path)])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    notes_total = next(ln for ln in lines if "Notes (total)" in ln)
    notes_live = next(ln for ln in lines if "Notes (live)" in ln)
    notes_deleted = next(ln for ln in lines if "Notes (deleted)" in ln)
    assert "2" in notes_total
    assert "1" in notes_live
    assert "1" in notes_deleted

    edge_status_section = "\n".join(lines[lines.index("Edges by status") :])
    assert "fresh" in edge_status_section
    edge_source_section = "\n".join(lines[lines.index("Edges by source") :])
    assert "user" in edge_source_section
