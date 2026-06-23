"""Tests for lode.versions — the CAS-guarded version-save path (lode-s2f.3).

Covers the acceptance criteria: a save whose parent != the live head is rejected
(CAS); a save whose body equals the head's is a no-op dedup (head returned, no
row written); delete writes an op=delete tombstone preserving lineage; and a
soft-deleted note recovers by repointing the head.
"""

from pathlib import Path

import pytest

from lode.hashing import NO_PARENT, content_version_id
from lode.storage import init_db
from lode.versions import HeadConflictError, delete, recover, save


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
