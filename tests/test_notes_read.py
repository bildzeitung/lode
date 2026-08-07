"""Tests for lode.notes_read -- the shared notes read side (lode-0wj.5, lode-1gr.1).

Pins the ticket's acceptance criterion at the module level, mirroring
``tests/test_tui_ask.py``'s direct unit style: live notes only (a soft-deleted
note is excluded), newest-first ordering, the edit-count/chain-length column,
and the summary-annotation-or-first-line fallback for the Summary column.
Also covers :func:`list_deleted_notes` (lode-d32.2), the tombstoned-only
sibling reader ``lode notes --deleted`` calls.
"""

import json
from pathlib import Path

import pytest

from lode.lexical import LexicalCacheBackend
from lode.notes_read import (
    candidate_rows_conn,
    list_deleted_notes,
    list_notes,
    list_notes_with_all_tags,
    list_tags,
    list_versions,
    search_notes,
    short_note_id,
    version_body,
)
from lode.storage import init_db
from lode.versions import delete, save


def _write_tag(
    db_path: Path,
    note_id: str,
    version_id: str,
    tag: str,
    *,
    source: str = "ai",
    status: str = "fresh",
) -> None:
    """Insert a ``kind='tag'`` annotation directly (lode-olmi.6's shape)."""
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'tag', ?, ?, ?)",
            (note_id, version_id, json.dumps(tag), source, status),
        )
        conn.commit()
    finally:
        conn.close()


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


def test_short_note_id_truncates_to_8_chars() -> None:
    """The shared short-id helper (lode-1gr.2) -- Browse's Id column, 'lode show'."""
    assert short_note_id("0123456789abcdef") == "01234567"


def test_short_note_id_leaves_a_shorter_id_unchanged() -> None:
    assert short_note_id("short") == "short"


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


def test_list_notes_orders_deterministically_on_a_created_tie(
    tmp_path: Path,
) -> None:
    """A tied ``created`` no longer falls back to SQLite's unstable tie-break.

    Failure mode 1 from lode-7h8j's investigation: forcing three notes'
    ``created`` to an identical value used to make ``list_notes`` return
    rowid-ASCENDING (oldest-first) order for the tied rows -- the opposite of
    the newest-first order the whole Browse UI assumes. Insertion order here
    is a, b, c, so newest-first is c, b, a.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "first saved")
        save(conn, "note-b", "second saved")
        save(conn, "note-c", "third saved")
        conn.execute("UPDATE notes SET created = '2026-01-01T00:00:00.000Z'")
        conn.commit()
    finally:
        conn.close()

    rows = list_notes(db_path)

    assert [row.note_id for row in rows] == ["note-c", "note-b", "note-a"]


def test_list_notes_orders_by_insertion_when_created_regresses(
    tmp_path: Path,
) -> None:
    """A wall-clock ``created`` regression still yields insertion order.

    Failure mode 2 from lode-7h8j's investigation, reproduced live under CPU
    load (not merely forced): an earlier-saved note's ``created`` can land
    *after* a later-saved note's, with no tie involved. A tiebreaker on
    ``created`` cannot fix this -- it only ever runs when ``created`` values
    are equal. Here note-a is saved FIRST but is given the LATEST ``created``
    timestamp; the correct newest-first order is still insertion order
    (c, b, a), not created-DESC order (which would wrongly put a first).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "first saved")
        save(conn, "note-b", "second saved")
        save(conn, "note-c", "third saved")
        conn.execute(
            "UPDATE notes SET created = '2026-01-01T00:00:52.016Z' "
            "WHERE note_id = 'note-a'"
        )
        conn.execute(
            "UPDATE notes SET created = '2026-01-01T00:00:51.192Z' "
            "WHERE note_id = 'note-b'"
        )
        conn.execute(
            "UPDATE notes SET created = '2026-01-01T00:00:51.203Z' "
            "WHERE note_id = 'note-c'"
        )
        conn.commit()
    finally:
        conn.close()

    rows = list_notes(db_path)

    assert [row.note_id for row in rows] == ["note-c", "note-b", "note-a"]


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


# lode-b4w.3: both cases exercise the same "no annotation -> fall back to
# first non-blank line" summary logic, differing only in body content and
# expected summary. Parametrized over (body, expected_summary), 2 tests -> 1.
@pytest.mark.parametrize(
    "body, expected_summary",
    [
        pytest.param(
            "the actual first line\nmore text below",
            "the actual first line",
            id="first_line",
        ),
        pytest.param(
            "\n   \nfirst real content\nmore",
            "first real content",
            id="first_non_blank_line",
        ),
    ],
)
def test_list_notes_falls_back_to_first_non_blank_line(
    tmp_path: Path, body: str, expected_summary: str
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-1", body)
    finally:
        conn.close()

    rows = list_notes(db_path)

    assert rows[0].summary == expected_summary


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


def test_list_deleted_notes_returns_only_tombstoned_notes(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "live-note", "still here")
        gone_head = save(conn, "gone-note", "will be deleted").version_id
        delete(conn, "gone-note", parent=gone_head)
    finally:
        conn.close()

    rows = list_deleted_notes(db_path)

    assert [row.note_id for row in rows] == ["gone-note"]


def test_list_deleted_notes_empty_when_nothing_is_tombstoned(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "live-note", "still here")
    finally:
        conn.close()

    assert list_deleted_notes(db_path) == []


def test_list_deleted_notes_orders_newest_first(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head1 = save(conn, "n1", "first note").version_id
        head2 = save(conn, "n2", "second note").version_id
        delete(conn, "n1", parent=head1)
        delete(conn, "n2", parent=head2)
    finally:
        conn.close()
    # Both tombstoned in the same tick, order pinned via an explicit re-seed of
    # notes.created (same technique test_list_notes_orders_newest_first uses).
    conn = init_db(db_path)
    try:
        conn.execute(
            "UPDATE notes SET created = '2026-01-01T00:00:00.000Z' WHERE note_id = 'n1'"
        )
        conn.execute(
            "UPDATE notes SET created = '2026-02-01T00:00:00.000Z' WHERE note_id = 'n2'"
        )
        conn.commit()
    finally:
        conn.close()

    rows = list_deleted_notes(db_path)

    assert [row.note_id for row in rows] == ["n2", "n1"]


def test_list_deleted_notes_orders_deterministically_on_a_created_tie(
    tmp_path: Path,
) -> None:
    """A tied ``created`` no longer falls back to SQLite's unstable tie-break.

    Same failure mode as lode-7h8j's ``test_list_notes_orders_deterministically_
    on_a_created_tie``, reproduced here for ``list_deleted_notes``: forcing three
    tombstoned notes' ``created`` to an identical value used to make
    ``list_deleted_notes`` return rowid-ASCENDING (oldest-first) order for the
    tied rows -- the opposite of the newest-first order the CLI's ``--deleted``
    view assumes. Insertion order here is a, b, c, so newest-first is c, b, a.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_a = save(conn, "note-a", "first saved").version_id
        head_b = save(conn, "note-b", "second saved").version_id
        head_c = save(conn, "note-c", "third saved").version_id
        delete(conn, "note-a", parent=head_a)
        delete(conn, "note-b", parent=head_b)
        delete(conn, "note-c", parent=head_c)
        conn.execute("UPDATE notes SET created = '2026-01-01T00:00:00.000Z'")
        conn.commit()
    finally:
        conn.close()

    rows = list_deleted_notes(db_path)

    assert [row.note_id for row in rows] == ["note-c", "note-b", "note-a"]


def test_list_deleted_notes_orders_by_insertion_when_created_regresses(
    tmp_path: Path,
) -> None:
    """A wall-clock ``created`` regression still yields insertion order.

    Same failure mode as lode-7h8j's ``test_list_notes_orders_by_insertion_when_
    created_regresses``, reproduced here for ``list_deleted_notes``: an
    earlier-saved note's ``created`` can land *after* a later-saved note's, with
    no tie involved. A tiebreaker on ``created`` cannot fix this -- it only ever
    runs when ``created`` values are equal. Here note-a is saved FIRST but is
    given the LATEST ``created`` timestamp; the correct newest-first order is
    still insertion order (c, b, a), not created-DESC order (which would
    wrongly put a first).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_a = save(conn, "note-a", "first saved").version_id
        head_b = save(conn, "note-b", "second saved").version_id
        head_c = save(conn, "note-c", "third saved").version_id
        delete(conn, "note-a", parent=head_a)
        delete(conn, "note-b", parent=head_b)
        delete(conn, "note-c", parent=head_c)
        conn.execute(
            "UPDATE notes SET created = '2026-01-01T00:00:52.016Z' "
            "WHERE note_id = 'note-a'"
        )
        conn.execute(
            "UPDATE notes SET created = '2026-01-01T00:00:51.192Z' "
            "WHERE note_id = 'note-b'"
        )
        conn.execute(
            "UPDATE notes SET created = '2026-01-01T00:00:51.203Z' "
            "WHERE note_id = 'note-c'"
        )
        conn.commit()
    finally:
        conn.close()

    rows = list_deleted_notes(db_path)

    assert [row.note_id for row in rows] == ["note-c", "note-b", "note-a"]


def test_list_deleted_notes_summary_falls_back_to_the_tombstones_carried_body(
    tmp_path: Path,
) -> None:
    """A tombstone's body is the pre-delete body, carried forward by ``delete``."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "the original first line\nmore text").version_id
        delete(conn, "note-1", parent=head)
    finally:
        conn.close()

    rows = list_deleted_notes(db_path)

    assert rows[0].summary == "the original first line"


# ---------------------------------------------------------------------------
# candidate_rows_conn (lode-l38d.10) -- the ambiguous-prefix CLI error's
# lookup: date/summary/deleted-state for a specific candidate set, spanning
# both live and tombstoned notes at once (unlike list_notes/list_deleted_notes,
# each scoped to a single state).
# ---------------------------------------------------------------------------


def test_candidate_rows_conn_resolves_live_candidates_in_the_given_order(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-aaa222", "second body")
        save(conn, "note-aaa111", "first body")

        rows = candidate_rows_conn(conn, ["note-aaa222", "note-aaa111"])
    finally:
        conn.close()

    # Same order as the input list, NOT re-sorted by created/note_id.
    assert [row.note_id for row in rows] == ["note-aaa222", "note-aaa111"]
    assert [row.summary for row in rows] == ["second body", "first body"]
    assert [row.deleted for row in rows] == [False, False]


def test_candidate_rows_conn_flags_a_tombstoned_candidate_and_uses_its_body(
    tmp_path: Path,
) -> None:
    """A candidate set mixing live and tombstoned notes resolves both.

    The WRINKLE lode-l38d.10 calls out: ``recover`` resolves with
    ``include_deleted=True``, so its candidate set can span both states. The
    tombstoned candidate must render *correctly* -- not blank -- and must
    carry a marker distinguishing it from a live match, since for ``recover``
    it is the one the user actually wants.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-ddd111", "still live")
        gone_head = save(conn, "note-ddd222", "gone soon").version_id
        delete(conn, "note-ddd222", parent=gone_head)

        rows = candidate_rows_conn(conn, ["note-ddd111", "note-ddd222"])
    finally:
        conn.close()

    assert rows[0].note_id == "note-ddd111"
    assert rows[0].summary == "still live"
    assert rows[0].deleted is False

    assert rows[1].note_id == "note-ddd222"
    assert rows[1].summary == "gone soon"  # the tombstone's carried-forward body
    assert rows[1].deleted is True


def test_candidate_rows_conn_uses_the_head_summary_annotation_when_present(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-1", "some long note body").version_id
    finally:
        conn.close()
    _write_summary(db_path, "note-1", head, "AI one-line summary")

    conn = init_db(db_path)
    try:
        rows = candidate_rows_conn(conn, ["note-1"])
    finally:
        conn.close()

    assert rows[0].summary == "AI one-line summary"


def test_candidate_rows_conn_empty_input_returns_empty_list(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        rows = candidate_rows_conn(conn, [])
    finally:
        conn.close()

    assert rows == []


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


# ---------------------------------------------------------------------------
# list_tags / list_notes_with_all_tags (lode-olmi.6) -- the Tags screen's read
# side: the distinct tag set, and the AND/intersection notes filter over it.
# ---------------------------------------------------------------------------


def test_list_tags_returns_distinct_sorted_values(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_a = save(conn, "note-a", "about staging").version_id
        head_b = save(conn, "note-b", "about prod").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head_a, "staging")
    _write_tag(db_path, "note-b", head_b, "prod")
    _write_tag(db_path, "note-b", head_b, "staging")  # duplicate value

    assert list_tags(db_path) == ["prod", "staging"]


def test_list_tags_excludes_a_curation_tombstone(tmp_path: Path) -> None:
    """A user ``status='orphaned'`` row is a curation tombstone, never a real tag."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "a note").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head, "removed-tag", source="user", status="orphaned")

    assert list_tags(db_path) == []


def test_list_tags_includes_a_stale_tag(tmp_path: Path) -> None:
    """Tags are shown, just flagged stale elsewhere -- never hidden for staleness."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "a note").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head, "stale-tag", status="stale")

    assert list_tags(db_path) == ["stale-tag"]


def test_list_tags_empty_when_no_notes_are_tagged(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "untagged")
    finally:
        conn.close()

    assert list_tags(db_path) == []


def test_list_notes_with_all_tags_returns_every_live_note_when_empty(
    tmp_path: Path,
) -> None:
    """No selected tags -- the same rows :func:`list_notes` returns."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "first")
        save(conn, "note-b", "second")
    finally:
        conn.close()

    assert list_notes_with_all_tags(db_path, []) == list_notes(db_path)


def test_list_notes_with_all_tags_requires_every_selected_tag(tmp_path: Path) -> None:
    """AND/intersection: a note must carry EVERY selected tag, not just one."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_both = save(conn, "note-both", "has both tags").version_id
        head_one = save(conn, "note-one", "has only one tag").version_id
        save(conn, "note-neither", "has no tags")
    finally:
        conn.close()
    _write_tag(db_path, "note-both", head_both, "staging")
    _write_tag(db_path, "note-both", head_both, "urgent")
    _write_tag(db_path, "note-one", head_one, "staging")

    rows = list_notes_with_all_tags(db_path, ["staging", "urgent"])

    assert [row.note_id for row in rows] == ["note-both"]


def test_list_notes_with_all_tags_single_tag_is_a_plain_membership_filter(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head_a = save(conn, "note-a", "tagged").version_id
        save(conn, "note-b", "untagged")
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head_a, "staging")

    rows = list_notes_with_all_tags(db_path, ["staging"])

    assert [row.note_id for row in rows] == ["note-a"]


def test_list_notes_with_all_tags_excludes_a_deleted_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "gone-note", "will be deleted").version_id
        _write_tag(db_path, "gone-note", head, "staging")
    finally:
        conn.close()
    conn = init_db(db_path)
    try:
        delete(conn, "gone-note", parent=head)
    finally:
        conn.close()

    assert list_notes_with_all_tags(db_path, ["staging"]) == []


def test_list_notes_with_all_tags_treats_a_tombstoned_tag_as_absent(
    tmp_path: Path,
) -> None:
    """A curation-tombstoned tag row does not count toward the AND filter."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-a", "a note").version_id
    finally:
        conn.close()
    _write_tag(db_path, "note-a", head, "removed-tag", source="user", status="orphaned")

    assert list_notes_with_all_tags(db_path, ["removed-tag"]) == []


def test_list_notes_with_all_tags_orders_newest_first(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_note_with_created(
        db_path, "note-1", "v1", "first", "2026-01-01T00:00:00.000Z"
    )
    _seed_note_with_created(
        db_path, "note-2", "v2", "second", "2026-02-01T00:00:00.000Z"
    )
    _write_tag(db_path, "note-1", "v1", "staging")
    _write_tag(db_path, "note-2", "v2", "staging")

    rows = list_notes_with_all_tags(db_path, ["staging"])

    assert [row.note_id for row in rows] == ["note-2", "note-1"]


# --- search_notes: BM25 quick search over live notes (lode-35nu.6) ------------


def _seed_and_index(db_path: Path, note_id: str, body: str) -> str:
    """Save a note AND drive its lexical (FTS5) index, mirroring the production
    save path (``Repository`` + ``LexicalCacheBackend``) -- plain
    :func:`lode.versions.save` alone never touches ``passages_fts``."""
    conn = init_db(db_path)
    try:
        result = save(conn, note_id, body)
        LexicalCacheBackend(conn).index(note_id, result.version_id, body)
        return result.version_id
    finally:
        conn.close()


def test_search_notes_finds_a_note_by_keyword(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_and_index(db_path, "note-1", "rotate the staging certificates nightly")
    _seed_and_index(db_path, "note-2", "grocery list: eggs, milk, bread")

    rows = search_notes(db_path, "certificates")

    assert [row.note_id for row in rows] == ["note-1"]


def test_search_notes_matches_a_still_incomplete_word_via_prefix(
    tmp_path: Path,
) -> None:
    """Incremental typing: "cert" (word not yet finished) still matches "certificates"."""
    db_path = tmp_path / "lode.db"
    _seed_and_index(db_path, "note-1", "rotate the staging certificates nightly")

    rows = search_notes(db_path, "cert")

    assert [row.note_id for row in rows] == ["note-1"]


def test_search_notes_orders_by_relevance_best_first(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_and_index(db_path, "note-weak", "widget mentioned once")
    _seed_and_index(db_path, "note-strong", "widget widget widget widget")

    rows = search_notes(db_path, "widget")

    assert [row.note_id for row in rows] == ["note-strong", "note-weak"]


def test_search_notes_empty_query_returns_no_rows(tmp_path: Path) -> None:
    """The caller (BrowseScreen) branches to the full list on an empty query --
    this function itself returns nothing for one, never "everything"."""
    db_path = tmp_path / "lode.db"
    _seed_and_index(db_path, "note-1", "some note body")

    assert search_notes(db_path, "") == []
    assert search_notes(db_path, '   "-:') == []  # no usable token either


def test_search_notes_excludes_a_soft_deleted_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    version_id = _seed_and_index(db_path, "note-1", "widget rotation runbook")
    conn = init_db(db_path)
    try:
        delete(conn, "note-1", parent=version_id)
    finally:
        conn.close()

    assert search_notes(db_path, "widget") == []


def test_search_notes_excludes_stale_prior_head_content(tmp_path: Path) -> None:
    """An edited note's superseded body must not still surface via quick search."""
    db_path = tmp_path / "lode.db"
    root = _seed_and_index(db_path, "note-1", "original gadget text")
    conn = init_db(db_path)
    try:
        result = save(conn, "note-1", "replacement text", parent=root)
        LexicalCacheBackend(conn).index("note-1", result.version_id, "replacement text")
    finally:
        conn.close()

    assert search_notes(db_path, "gadget") == []
    assert [row.note_id for row in search_notes(db_path, "replacement")] == ["note-1"]


def test_search_notes_excludes_external_snapshot_passages(tmp_path: Path) -> None:
    """passages_fts also carries externals' own passages (lode.externals) -- an
    unfiltered scope would surface those with no owning note (/challenge gap 1)."""
    from lode.externals import ingest_snapshot

    db_path = tmp_path / "lode.db"
    _seed_and_index(db_path, "note-1", "a note about widgets")
    conn = init_db(db_path)
    try:
        ingest_snapshot(conn, "ext-1", "web", "widgets mentioned in an external page")
    finally:
        conn.close()

    rows = search_notes(db_path, "widgets")

    assert [row.note_id for row in rows] == ["note-1"]


def test_search_notes_deduplicates_a_note_with_multiple_matching_passages(
    tmp_path: Path,
) -> None:
    body = "# widget\n\nfirst widget section\n\n# gadget\n\nsecond widget section"
    db_path = tmp_path / "lode.db"
    _seed_and_index(db_path, "note-1", body)

    rows = search_notes(db_path, "widget")

    assert [row.note_id for row in rows] == ["note-1"]  # one row, not one per passage
