"""The version-save path: append-only chains with a CAS-guarded head (lode-s2f.3).

This is the write side of the event-sourced version chain (``docs/storage.md``,
"Storage model: event-sourced, linear per-note chains"). It builds directly on
the two landed foundations and reimplements neither:

- the data shape in :mod:`lode.storage` / ``schema.sql`` — the ``notes`` head
  pointer and the immutable ``versions`` chain, with the ``DEFERRABLE INITIALLY
  DEFERRED`` FK that lets a root create insert the note row and its first version
  in one transaction;
- :func:`lode.hashing.content_version_id` — the SINGLE source of truth for a
  version's ``version_id`` (imported, never recomputed here).

Four operations, each atomic (wrapped in ``with conn:`` so the deferred FK is
checked at COMMIT):

- **create** (:func:`save` on an absent note) — a root version, ``op=create``.
- **update** (:func:`save` on a present note) — a new version parented on the
  current head, ``op=update``, **only if** the caller's ``parent`` still equals
  the live head (compare-and-swap). A body identical to the head's is a **no-op
  dedup**: it returns the head and writes no row.
- **delete** (:func:`delete`) — a tombstone version, ``op=delete``, carrying the
  head body forward so lineage is preserved; CAS-guarded like an update.
- **recover** (:func:`recover`) — soft-delete recovery is just **repointing the
  head** back to a prior version; it writes no new row.

**Branch prevention is the CAS** (``docs/storage.md``): every update/delete
parents the live head and is rejected if the head moved since the caller loaded
it. A reject raises :class:`HeadConflictError`, carrying the note, the parent the
caller expected, and the head it actually found. That exception is the **clean
seam** for ``lode-s2f.4``, which turns a reject into the user-facing conflict
surface (diff the buffer against the new head, preserve it as a draft, re-apply
or discard). This module deliberately stops at the honest reject; it does not
build that surface.
"""

import sqlite3
from dataclasses import dataclass

from lode.config import Settings
from lode.hashing import NO_PARENT, content_version_id


@dataclass(frozen=True)
class SaveResult:
    """The outcome of a save/delete/recover: which version the head now points at.

    ``op`` is the version op that was written (``create`` / ``update`` /
    ``delete``), or ``recover`` when the head was merely repointed. ``deduped`` is
    True only for a no-op update whose body equalled the head's — in that case no
    row was written and ``version_id`` is the unchanged head.
    """

    note_id: str
    version_id: str
    op: str
    deduped: bool = False


class HeadConflictError(Exception):
    """A save was rejected because the head is not where the caller parented on.

    Raised by the compare-and-swap when ``expected_parent`` (the head the caller
    loaded) no longer matches ``actual_head`` (the live head) — two editor panes
    on one note, or an edit landed while a slow save was in flight. The chain
    stays linear because the reject is honest rather than auto-merged.

    The attributes are the seam ``lode-s2f.4`` builds the conflict surface on:
    it catches this, diffs the caller's buffer against ``actual_head``, and lets
    the user re-apply or discard. ``actual_head`` is ``None`` when the conflict is
    a create against a note that already exists.
    """

    def __init__(
        self, note_id: str, expected_parent: str, actual_head: str | None
    ) -> None:
        super().__init__(
            f"note {note_id!r} changed since it was opened: "
            f"expected parent {expected_parent!r}, head is {actual_head!r}"
        )
        self.note_id = note_id
        self.expected_parent = expected_parent
        self.actual_head = actual_head


def _head(conn: sqlite3.Connection, note_id: str) -> tuple[str | None, str | None]:
    """Return ``(head_version_id, head_body)`` for ``note_id``, both None if absent.

    A present note with a NULL head is impossible here — every note this module
    creates points at its root version atomically — so a found note always has a
    body to compare against for dedup.
    """
    row = conn.execute(
        "SELECT v.version_id, v.body FROM notes n "
        "JOIN versions v ON v.version_id = n.head_version_id "
        "WHERE n.note_id = ?",
        (note_id,),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _write_version(
    conn: sqlite3.Connection,
    version_id: str,
    note_id: str,
    parent: str,
    body: str,
    op: str,
) -> None:
    """Insert one immutable version row.

    ``parent`` is the framed-hash parent (:data:`~lode.hashing.NO_PARENT` for a
    root create); it is stored as SQL NULL there so the ``parent_version_id`` FK
    references a real version or nothing. The ``versions.note_id`` FK is *not*
    deferred, so the ``notes`` row must already exist when this runs (on a create,
    the note row is inserted first, pointing at this id via the deferred head FK).
    """
    conn.execute(
        "INSERT INTO versions "
        "(version_id, note_id, parent_version_id, body, op) VALUES (?, ?, ?, ?, ?)",
        (version_id, note_id, parent or None, body, op),
    )


def _cas_head(
    conn: sqlite3.Connection, note_id: str, new_head: str, expected_parent: str
) -> None:
    """Move the head to ``new_head`` only if it still equals ``expected_parent``.

    The conditional UPDATE is the compare-and-swap itself: a zero rowcount means
    the head moved between the read and the write (a race), so the save loses and
    raises rather than clobbering the newer head.
    """
    cur = conn.execute(
        "UPDATE notes SET head_version_id = ? WHERE note_id = ? AND head_version_id = ?",
        (new_head, note_id, expected_parent),
    )
    if cur.rowcount != 1:
        actual_head, _ = _head(conn, note_id)
        raise HeadConflictError(note_id, expected_parent, actual_head)


def save(
    conn: sqlite3.Connection,
    note_id: str,
    body: str,
    *,
    parent: str = NO_PARENT,
    settings: Settings | None = None,
) -> SaveResult:
    """Create or update ``note_id`` to ``body``, returning the resulting head.

    Whether this is a create or an update is read off the store, not a flag:

    - **Note absent → create.** ``parent`` must be :data:`~lode.hashing.NO_PARENT`
      (a root has no parent); the note row and its root version are inserted in
      one transaction (the deferred FK permits the head to point at the
      not-yet-written version).
    - **Note present → update.** ``parent`` must equal the live head
      (compare-and-swap) or :class:`HeadConflictError` is raised. If ``body``
      already equals the head's body the save is a **no-op**: the head is returned
      with ``deduped=True`` and no row is written.

    Concurrency safety comes from the CAS plus SQLite serializing writes, not from
    there being one process (``docs/storage.md``).
    """
    settings = settings or Settings()
    with conn:
        head, head_body = _head(conn, note_id)

        if head is None:
            # Create: a root version. A non-empty parent on an absent note, or a
            # note that already exists, is a conflict (you cannot re-root).
            if parent != NO_PARENT:
                raise HeadConflictError(note_id, parent, None)
            version_id = content_version_id(note_id, NO_PARENT, body, settings)
            # Note row first: its head points at the not-yet-written version (the
            # deferred FK permits this), satisfying the version's note_id FK.
            conn.execute(
                "INSERT INTO notes (note_id, head_version_id) VALUES (?, ?)",
                (note_id, version_id),
            )
            _write_version(conn, version_id, note_id, NO_PARENT, body, "create")
            return SaveResult(note_id, version_id, "create")

        # Update: CAS against the live head.
        if parent != head:
            raise HeadConflictError(note_id, parent, head)
        if body == head_body:
            return SaveResult(note_id, head, "update", deduped=True)
        version_id = content_version_id(note_id, head, body, settings)
        _write_version(conn, version_id, note_id, head, body, "update")
        _cas_head(conn, note_id, version_id, head)
        return SaveResult(note_id, version_id, "update")


def delete(
    conn: sqlite3.Connection,
    note_id: str,
    *,
    parent: str,
    settings: Settings | None = None,
) -> SaveResult:
    """Soft-delete ``note_id`` by appending an ``op=delete`` tombstone.

    Like an update, the tombstone parents the live head and is CAS-guarded
    (``parent`` must equal the head). It carries the head body forward so the
    lineage records *what* was deleted; recovery is :func:`recover` repointing the
    head back past the tombstone. ``KeyError`` if the note does not exist.
    """
    settings = settings or Settings()
    with conn:
        head, head_body = _head(conn, note_id)
        if head is None:
            raise KeyError(note_id)
        if parent != head:
            raise HeadConflictError(note_id, parent, head)
        version_id = content_version_id(note_id, head, head_body, settings)
        _write_version(conn, version_id, note_id, head, head_body, "delete")
        _cas_head(conn, note_id, version_id, head)
        return SaveResult(note_id, version_id, "delete")


def recover(
    conn: sqlite3.Connection,
    note_id: str,
    *,
    target_version: str,
    settings: Settings | None = None,
) -> SaveResult:
    """Recover a soft-deleted note by repointing its head to ``target_version``.

    Recovery is *not* a new version (``docs/storage.md``: "recovery = repoint the
    head") — it simply moves the head pointer to an existing version of this note,
    typically the pre-delete head. ``target_version`` must be a version that
    belongs to ``note_id`` (``KeyError`` otherwise). ``settings`` is accepted for a
    uniform signature though no hashing is needed.
    """
    with conn:
        row = conn.execute(
            "SELECT 1 FROM versions WHERE version_id = ? AND note_id = ?",
            (target_version, note_id),
        ).fetchone()
        if row is None:
            raise KeyError(target_version)
        conn.execute(
            "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
            (target_version, note_id),
        )
        return SaveResult(note_id, target_version, "recover")
