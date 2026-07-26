"""Tests for lode.versions — the CAS-guarded version-save path (lode-s2f.3).

Covers the acceptance criteria: a save whose parent != the live head is rejected
(CAS); a save whose body equals the head's is a no-op dedup (head returned, no
row written); delete writes an op=delete tombstone preserving lineage; and a
soft-deleted note recovers by repointing the head.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lode.hashing import NO_PARENT, content_version_id
from lode.storage import init_db
from lode.versions import HeadConflictError, delete, purge, recover, save


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


def _count_versions(conn, note_id: str) -> int:
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM versions WHERE note_id = ?", (note_id,)
    ).fetchone()
    return n


def _head(conn, note_id: str) -> str:
    (head,) = conn.execute(
        "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    ).fetchone()
    return head


# --- create -----------------------------------------------------------------


def test_create_inserts_note_and_root_version(conn):
    result = save(conn, "note-1", "hello")
    assert result.op == "create"
    assert not result.deduped
    # version_id is the single-source-of-truth hash (root => NO_PARENT).
    assert result.version_id == content_version_id("note-1", NO_PARENT, "hello")
    # The head points at the root version; its parent is NULL in the chain.
    assert _head(conn, "note-1") == result.version_id
    (body, op, parent) = conn.execute(
        "SELECT body, op, parent_version_id FROM versions WHERE version_id = ?",
        (result.version_id,),
    ).fetchone()
    assert (body, op, parent) == ("hello", "create", None)


def test_recreating_an_existing_note_conflicts(conn):
    save(conn, "note-1", "hello")
    # A root create against a note that already exists is a CAS conflict.
    with pytest.raises(HeadConflictError):
        save(conn, "note-1", "again")


# --- update + CAS -----------------------------------------------------------


def test_update_chains_a_new_version_onto_the_head(conn):
    root = save(conn, "note-1", "v1").version_id
    result = save(conn, "note-1", "v2", parent=root)
    assert result.op == "update"
    assert not result.deduped
    assert result.version_id == content_version_id("note-1", root, "v2")
    assert _head(conn, "note-1") == result.version_id
    # The new version parents the prior head — lineage is preserved.
    (parent,) = conn.execute(
        "SELECT parent_version_id FROM versions WHERE version_id = ?",
        (result.version_id,),
    ).fetchone()
    assert parent == root
    assert _count_versions(conn, "note-1") == 2


def test_update_with_stale_parent_is_rejected(conn):
    """A save parented on anything but the live head is refused (CAS)."""
    root = save(conn, "note-1", "v1").version_id
    save(conn, "note-1", "v2", parent=root)  # head moves past root
    with pytest.raises(HeadConflictError) as exc:
        save(conn, "note-1", "v2-conflict", parent=root)  # still parented on root
    assert exc.value.note_id == "note-1"
    assert exc.value.expected_parent == root
    assert exc.value.actual_head == _head(conn, "note-1")
    # The rejected save wrote nothing; the chain is untouched.
    assert _count_versions(conn, "note-1") == 2


def test_update_against_unknown_parent_is_rejected(conn):
    root = save(conn, "note-1", "v1").version_id
    with pytest.raises(HeadConflictError):
        save(conn, "note-1", "v2", parent="bogus-parent")
    assert _head(conn, "note-1") == root


# --- no-op dedup ------------------------------------------------------------


def test_resaving_identical_body_is_a_noop_dedup(conn):
    root = save(conn, "note-1", "same").version_id
    result = save(conn, "note-1", "same", parent=root)
    assert result.deduped
    assert result.op == "update"
    assert result.version_id == root  # returns the unchanged head
    assert _head(conn, "note-1") == root
    assert _count_versions(conn, "note-1") == 1  # NO new row written


# --- delete (tombstone) -----------------------------------------------------


def test_delete_writes_a_tombstone_preserving_lineage(conn):
    root = save(conn, "note-1", "body").version_id
    result = delete(conn, "note-1", parent=root)
    assert result.op == "delete"
    # The tombstone parents the prior head and carries its body forward.
    (op, parent, body) = conn.execute(
        "SELECT op, parent_version_id, body FROM versions WHERE version_id = ?",
        (result.version_id,),
    ).fetchone()
    assert (op, parent, body) == ("delete", root, "body")
    assert _head(conn, "note-1") == result.version_id
    assert _count_versions(conn, "note-1") == 2  # root + tombstone retained


def test_delete_is_cas_guarded(conn):
    root = save(conn, "note-1", "v1").version_id
    save(conn, "note-1", "v2", parent=root)  # head moves
    with pytest.raises(HeadConflictError):
        delete(conn, "note-1", parent=root)  # stale parent


def test_delete_unknown_note_raises_keyerror(conn):
    with pytest.raises(KeyError):
        delete(conn, "ghost", parent=NO_PARENT)


# --- recover (repoint head) -------------------------------------------------


def test_recover_repoints_head_past_the_tombstone(conn):
    root = save(conn, "note-1", "body").version_id
    tomb = delete(conn, "note-1", parent=root).version_id
    assert _head(conn, "note-1") == tomb  # soft-deleted

    result = recover(conn, "note-1", target_version=root)
    assert result.op == "recover"
    assert result.version_id == root
    assert _head(conn, "note-1") == root  # recovered, no new row
    assert _count_versions(conn, "note-1") == 2  # tombstone is retained, not purged


def test_recover_rejects_a_version_from_another_note(conn):
    save(conn, "note-1", "a")
    other = save(conn, "note-2", "b").version_id
    with pytest.raises(KeyError):
        recover(conn, "note-1", target_version=other)


# --- idempotent re-delete (lode-n8q) -----------------------------------------


def test_delete_recover_delete_is_idempotent(conn):
    """delete -> recover -> delete on an unchanged note must not raise.

    content_version_id folds in note_id/parent/body but not op, so the second
    delete recomputes the SAME tombstone id as the first (identical inputs).
    delete() detects the existing row and repoints the head to it instead of
    re-inserting, so the second delete is a no-op on the version table.
    """
    head = save(conn, "note-1", "body").version_id
    t1 = delete(conn, "note-1", parent=head).version_id
    recover(conn, "note-1", target_version=head)

    result = delete(conn, "note-1", parent=head)  # must not raise IntegrityError

    assert result.version_id == t1  # same content => same tombstone, repointed
    assert result.op == "delete"
    assert _head(conn, "note-1") == t1  # head is the tombstone
    assert _count_versions(conn, "note-1") == 2  # exactly one tombstone, no dup row
    # The pre-delete content head stays reachable in the chain (lineage intact).
    (op, body) = conn.execute(
        "SELECT op, body FROM versions WHERE version_id = ?", (head,)
    ).fetchone()
    assert (op, body) == ("create", "body")


def test_delete_recover_edit_delete_mints_a_new_tombstone(conn):
    """Negative control: an edit between recover and the second delete changes
    the tombstone's inputs (different parent), so it must NOT dedup.
    """
    head = save(conn, "note-1", "body").version_id
    t1 = delete(conn, "note-1", parent=head).version_id
    recover(conn, "note-1", target_version=head)
    edited = save(conn, "note-1", "body-v2", parent=head).version_id

    t2 = delete(conn, "note-1", parent=edited).version_id

    assert t2 != t1  # different content => different tombstone, no dedup
    assert _head(conn, "note-1") == t2
    assert _count_versions(conn, "note-1") == 4  # root, t1, edit, t2 all retained


def test_re_delete_against_a_stale_parent_still_conflicts(conn):
    """The CAS guard runs BEFORE the dedup probe.

    Once a tombstone exists, a delete from a stale parent must still raise
    rather than silently resolving to that tombstone and repointing the head
    onto it. (The plain single-delete control lives in
    test_delete_writes_a_tombstone_preserving_lineage.)
    """
    head = save(conn, "note-1", "body").version_id
    delete(conn, "note-1", parent=head)
    recover(conn, "note-1", target_version=head)
    edited = save(conn, "note-1", "body-v2", parent=head).version_id

    with pytest.raises(HeadConflictError):
        delete(conn, "note-1", parent=head)  # stale: the head is now `edited`

    assert _head(conn, "note-1") == edited  # no silent repoint onto the tombstone


# --- structured conflict surface (lode-s2f.4) -------------------------------


def test_update_conflict_carries_rejected_buffer_and_new_head(conn):
    """A CAS-rejected update hands back the buffer + new head for reconciliation.

    The conflict is the "changed since you opened it" surface (docs/storage.md):
    it carries the caller's rejected buffer (preserved, never lost) and the new
    live head (id + body) so the TUI can diff and let the user re-apply/discard —
    while clobbering nothing (the chain is untouched).
    """
    root = save(conn, "note-1", "v1").version_id
    new_head = save(conn, "note-1", "v2", parent=root).version_id  # head moves
    with pytest.raises(HeadConflictError) as exc:
        save(conn, "note-1", "my-unsaved-edit", parent=root)  # stale parent
    # The rejected buffer is preserved on the conflict, not clobbered or merged.
    assert exc.value.rejected_buffer == "my-unsaved-edit"
    # The new head (id + body) is carried for the diff the user is shown.
    assert exc.value.actual_head == new_head
    assert exc.value.actual_head_body == "v2"
    assert exc.value.expected_parent == root
    # Nothing was written: no auto-merge, no clobber.
    assert _count_versions(conn, "note-1") == 2
    assert _head(conn, "note-1") == new_head


def test_recreate_conflict_carries_buffer_and_existing_head(conn):
    """Re-rooting an existing note conflicts and preserves the rejected body."""
    head = save(conn, "note-1", "hello").version_id
    with pytest.raises(HeadConflictError) as exc:
        save(conn, "note-1", "again")  # NO_PARENT against a present note
    assert exc.value.rejected_buffer == "again"
    assert exc.value.actual_head == head
    assert exc.value.actual_head_body == "hello"


def test_update_on_absent_note_conflict_has_no_head_to_diff(conn):
    """A parented save on a missing note preserves the buffer; there is no head."""
    with pytest.raises(HeadConflictError) as exc:
        save(conn, "ghost", "my-edit", parent="some-parent")
    assert exc.value.rejected_buffer == "my-edit"
    # No note exists, so there is nothing to diff against.
    assert exc.value.actual_head is None
    assert exc.value.actual_head_body is None


def test_delete_conflict_surfaces_new_head_with_no_buffer(conn):
    """A CAS-rejected delete surfaces the new head but has no buffer to preserve."""
    root = save(conn, "note-1", "original").version_id
    new_head = save(conn, "note-1", "edited", parent=root).version_id  # head moves
    with pytest.raises(HeadConflictError) as exc:
        delete(conn, "note-1", parent=root)  # stale parent
    # A delete has no user-typed buffer, so nothing is "lost" to preserve.
    assert exc.value.rejected_buffer is None
    # The new head is still surfaced so the UI can re-confirm the delete.
    assert exc.value.actual_head == new_head
    assert exc.value.actual_head_body == "edited"


# --- purge (the hard delete) ------------------------------------------------


def _bodies(conn, note_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT body FROM versions WHERE note_id = ? ORDER BY rowid",
            (note_id,),
        )
    ]


def test_purge_overwrites_targeted_body_and_sets_purged_at(conn):
    root = save(conn, "note-1", "the secret password is hunter2").version_id

    result = purge(conn, "note-1")

    # The marker date is UTC (matching the store's UTC timestamps), so compare
    # against the UTC date — comparing local date would flake near midnight.
    marker = f"[purged {datetime.now(UTC):%Y-%m-%d}]"
    assert result.marker_body == marker
    (body, purged_at) = conn.execute(
        "SELECT body, purged_at FROM versions WHERE version_id = ?", (root,)
    ).fetchone()
    assert body == marker
    assert purged_at is not None  # purged_at is the structural "purged" flag


def test_purge_sweeps_the_whole_chain_including_tombstones(conn):
    root = save(conn, "note-1", "secret v1").version_id
    v2 = save(conn, "note-1", "secret v2", parent=root).version_id
    delete(conn, "note-1", parent=v2)  # soft-delete tombstone carries the body forward

    result = purge(conn, "note-1")

    # Every version body — root, update, and tombstone — is now the marker.
    assert _bodies(conn, "note-1") == [result.marker_body] * 3
    assert len(result.purged_versions) == 3
    # The id stays as the historical identifier (lineage survives), only bytes die.
    (op,) = conn.execute(
        "SELECT op FROM versions WHERE version_id = ?", (root,)
    ).fetchone()
    assert op == "create"


def test_purge_drops_ai_annotations_but_keeps_user_annotations(conn):
    root = save(conn, "note-1", "secret").version_id
    conn.execute(
        "INSERT INTO annotations (target, source_version, kind, payload, source, status) "
        "VALUES (?, ?, 'tag', 'ai-tag', 'ai', 'fresh')",
        ("note-1", root),
    )
    conn.execute(
        "INSERT INTO annotations (target, source_version, kind, payload, source, status) "
        "VALUES (?, NULL, 'tag', 'user-tag', 'user', 'fresh')",
        ("note-1",),
    )
    conn.commit()

    purge(conn, "note-1")

    rows = conn.execute(
        "SELECT source, payload FROM annotations ORDER BY source"
    ).fetchall()
    assert rows == [("user", "user-tag")]


def test_purge_is_idempotent(conn):
    save(conn, "note-1", "secret")
    first = purge(conn, "note-1")
    second = purge(conn, "note-1")
    assert _bodies(conn, "note-1") == [first.marker_body]
    assert second.purged_versions == first.purged_versions


def test_purge_unknown_note_raises(conn):
    with pytest.raises(KeyError):
        purge(conn, "ghost")
