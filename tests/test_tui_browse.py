"""Tests for lode.tui.browse -- the browse screen's read side (lode-0wj.5).

Pins the ticket's acceptance criterion at the module level, mirroring
``tests/test_tui_ask.py``'s direct unit style: live notes only (a soft-deleted
note is excluded), newest-first ordering, the edit-count/chain-length column,
and the summary-annotation-or-first-line fallback for the Summary column.
"""

import json
from pathlib import Path

from lode.storage import init_db
from lode.tui.browse import list_notes, list_versions, note_body, version_body
from lode.versions import delete, save


def _write_summary(db_path: Path, note_id: str, version_id: str, text: str) -> None:
    """Insert a fresh ``kind='summary'`` AI annotation directly (lode-0wj.9's shape)."""
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'summary', ?, 'ai', 'fresh')",
            (note_id, version_id, json.dumps(text)),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_note_with_created(
    db_path: Path, note_id: str, version_id: str, body: str, created: str
) -> None:
    """Seed a single-version note with an explicit ``notes.created`` timestamp.

    Bypasses :func:`lode.versions.save` (which stamps ``created`` as "now")
    since ordering needs distinct, controlled timestamps.
    """
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO notes (note_id, created) VALUES (?, ?)", (note_id, created)
        )
        conn.execute(
            "INSERT INTO versions (version_id, note_id, parent_version_id, body, op) "
            "VALUES (?, ?, NULL, ?, 'create')",
            (version_id, note_id, body),
        )
        conn.execute(
            "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
            (version_id, note_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_list_notes_orders_newest_first(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_note_with_created(
        db_path, "n1", "v1", "first note", "2026-01-01T00:00:00.000Z"
    )
    _seed_note_with_created(
        db_path, "n2", "v2", "second note", "2026-02-01T00:00:00.000Z"
    )

    rows = list_notes(db_path)

    assert [row.note_id for row in rows] == ["n2", "n1"]


def test_list_notes_excludes_a_deleted_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "live-note", "still here")
        gone_head = save(conn, "gone-note", "will be deleted").version_id
        delete(conn, "gone-note", parent=gone_head)
    finally:
        conn.close()

    rows = list_notes(db_path)

    assert [row.note_id for row in rows] == ["live-note"]


def test_list_notes_reports_chain_length_as_edit_count(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "v1 body").version_id
        head = save(conn, "note-1", "v2 body", parent=head).version_id
        save(conn, "note-1", "v3 body", parent=head)
    finally:
        conn.close()

    rows = list_notes(db_path)

    assert len(rows) == 1
    assert rows[0].version == 3


def test_list_notes_falls_back_to_first_line_when_unenriched(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-1", "the actual first line\nmore text below")
    finally:
        conn.close()

    rows = list_notes(db_path)

    assert rows[0].summary == "the actual first line"


def test_list_notes_falls_back_to_first_non_blank_line(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-1", "\n   \nfirst real content\nmore")
    finally:
        conn.close()

    rows = list_notes(db_path)

    assert rows[0].summary == "first real content"


def test_list_notes_uses_the_head_summary_annotation_when_present(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "some long note body about staging").version_id
    finally:
        conn.close()
    _write_summary(db_path, "note-1", head, "AI one-line summary")

    rows = list_notes(db_path)

    assert rows[0].summary == "AI one-line summary"


def test_list_notes_ignores_a_stale_summary_from_a_prior_head(tmp_path: Path) -> None:
    """A summary anchored to a superseded version is not the head's summary."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head1 = save(conn, "note-1", "v1 body").version_id
    finally:
        conn.close()
    _write_summary(db_path, "note-1", head1, "stale summary of v1")
    conn = init_db(db_path)
    try:
        save(conn, "note-1", "v2 body", parent=head1)
    finally:
        conn.close()

    rows = list_notes(db_path)

    # No fresh summary anchored to the new head -> falls back to first line.
    assert rows[0].summary == "v2 body"


def test_note_body_returns_the_live_head_body(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "v1 body").version_id
        save(conn, "note-1", "v2 body", parent=head)
    finally:
        conn.close()

    assert note_body(db_path, "note-1") == "v2 body"


def test_note_body_returns_none_for_a_deleted_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "v1 body").version_id
        delete(conn, "note-1", parent=head)
    finally:
        conn.close()

    assert note_body(db_path, "note-1") is None


def test_note_body_returns_none_for_an_absent_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    assert note_body(db_path, "nonexistent") is None


def test_list_versions_orders_newest_first_with_seq_matching_chain_length(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "v1 body").version_id
        head = save(conn, "note-1", "v2 body", parent=head).version_id
        save(conn, "note-1", "v3 body", parent=head)
    finally:
        conn.close()

    rows = list_versions(db_path, "note-1")

    assert [row.seq for row in rows] == [3, 2, 1]
    assert [row.op for row in rows] == ["update", "update", "create"]
    # The head's seq matches list_notes' chain-length count for the same note.
    assert rows[0].seq == list_notes(db_path)[0].version


def test_list_versions_follows_parent_links_not_created_order(
    tmp_path: Path,
) -> None:
    """Chain order comes from ``parent_version_id``, not the ``created`` column."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO notes (note_id, head_version_id) VALUES ('note-1', 'v2')"
        )
        # v2's created timestamp is earlier than v1's, but v2 is still v1's child.
        conn.execute(
            "INSERT INTO versions "
            "(version_id, note_id, parent_version_id, body, op, created) "
            "VALUES ('v1', 'note-1', NULL, 'root', 'create', "
            "'2026-01-01T00:00:01.000Z')"
        )
        conn.execute(
            "INSERT INTO versions "
            "(version_id, note_id, parent_version_id, body, op, created) "
            "VALUES ('v2', 'note-1', 'v1', 'child', 'update', "
            "'2026-01-01T00:00:00.000Z')"
        )
        conn.commit()
    finally:
        conn.close()

    rows = list_versions(db_path, "note-1")

    assert [row.version_id for row in rows] == ["v2", "v1"]
    assert [row.seq for row in rows] == [2, 1]


def test_list_versions_returns_empty_for_an_absent_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    assert list_versions(db_path, "nonexistent") == []


def test_version_body_returns_a_specific_non_head_version(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "v1 body").version_id
        save(conn, "note-1", "v2 body", parent=head)
    finally:
        conn.close()

    assert version_body(db_path, "note-1", head) == "v1 body"


def test_version_body_returns_none_for_an_unknown_version_id(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-1", "v1 body")
    finally:
        conn.close()

    assert version_body(db_path, "note-1", "nonexistent-version") is None
