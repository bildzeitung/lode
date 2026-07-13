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

A fifth operation, **purge** (:func:`purge`), is the deliberate immutability break
(``docs/externals.md`` "Hard delete", ``docs/storage.md``) — the E8 escape hatch
for sensitive content. Unlike the four above it *rewrites* existing version rows:
it overwrites every body in the chain (incl. tombstones) with a ``[purged
YYYY-MM-DD]`` marker and sets ``purged_at``, keeping ``version_id`` /
``parent_version_id`` / ``op`` / ``created`` so lineage survives — only the bytes
die. The id stays as the historical identifier but no longer hashes to its body
(``purged_at`` is the flag; the hash is no longer recomputable). It also drops the
chain's regenerable ``source='ai'`` annotations (keeping ``source='user'``
corrections, which are curation, not content). The cache-side cascade — LanceDB
vectors and FTS rows — is driven by :class:`lode.repository.Repository`, which
owns the cache seam.

**Branch prevention is the CAS** (``docs/storage.md``): every update/delete
parents the live head and is rejected if the head moved since the caller loaded
it. A reject raises :class:`HeadConflictError` — the **structured "changed since
you opened it" conflict** (``lode-s2f.4``): it carries the rejected buffer (the
caller's body, preserved so the unsaved edit is never lost) and the new live head
(version id + body) for the diff the user is shown. The reject never auto-merges
and never clobbers the newer head; the TUI reconciliation UI (E11) consumes the
conflict to let the user re-apply onto the new head or discard. The durable draft
store and the diff/resolution UI are that consumer's job — this module's contract
is the honest reject plus the buffer-preserving conflict it hands back.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

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


@dataclass(frozen=True)
class PurgeResult:
    """The outcome of a :func:`purge`: what the hard delete swept and rewrote.

    ``head_version_id`` / ``head_op`` identify the live head (and its op, so the
    caller knows whether the head is a soft-delete tombstone) for the cache-side
    re-derive. ``marker_body`` is the ``[purged YYYY-MM-DD]`` redaction marker every
    swept version now carries. ``purged_versions`` is the whole chain (incl.
    tombstones) whose bodies were overwritten — the exact set the cache cascade
    must evict.
    """

    note_id: str
    head_version_id: str
    head_op: str
    marker_body: str
    purged_versions: tuple[str, ...]


class HeadConflictError(Exception):
    """A save/delete was rejected because the head moved — the structured conflict.

    Raised by the compare-and-swap when ``expected_parent`` (the head the caller
    loaded) no longer matches the live head — two editor panes on one note, or an
    edit that landed while a slow save was in flight. The chain stays linear
    because the reject is honest: never auto-merged, never clobbering the newer
    head.

    This **is** the "changed since you opened it" conflict surface
    (``docs/storage.md``, "What the user sees when CAS rejects a save"). It
    carries everything the TUI reconciliation UI (E11) needs to let the user
    re-apply onto the new head or discard, with no merge machinery and no lost
    work:

    - ``rejected_buffer`` — the caller's body the save would have written. It is
      **preserved here** (handed back, never dropped) so an unlucky CAS loss
      never costs the unsaved edit; the durable draft store is the consumer's
      job, this layer's contract is simply that the buffer survives the reject.
      A delete has no user-typed buffer, so it is ``None`` there — the conflict
      still carries the new head so the UI can re-confirm the delete.
    - ``actual_head`` / ``actual_head_body`` — the new live head's version id and
      body, the right-hand side of the diff the user is shown. Both are ``None``
      when the conflict is a save against a note that does not exist (there is no
      head to diff against).
    - ``expected_parent`` — the head the caller parented on, for context.
    """

    def __init__(
        self,
        note_id: str,
        expected_parent: str,
        actual_head: str | None,
        *,
        actual_head_body: str | None = None,
        rejected_buffer: str | None = None,
    ) -> None:
        super().__init__(
            f"note {note_id!r} changed since it was opened: "
            f"expected parent {expected_parent!r}, head is {actual_head!r}"
        )
        self.note_id = note_id
        self.expected_parent = expected_parent
        self.actual_head = actual_head
        self.actual_head_body = actual_head_body
        self.rejected_buffer = rejected_buffer


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
    conn: sqlite3.Connection,
    note_id: str,
    new_head: str,
    expected_parent: str,
    rejected_buffer: str | None,
) -> None:
    """Move the head to ``new_head`` only if it still equals ``expected_parent``.

    The conditional UPDATE is the compare-and-swap itself: a zero rowcount means
    the head moved between the read and the write (a race), so the save loses and
    raises rather than clobbering the newer head. On the loss it re-reads the new
    live head (id + body) and raises the structured :class:`HeadConflictError`
    carrying ``rejected_buffer`` (the save's body, or ``None`` for a delete) so an
    unsaved edit survives the reject.
    """
    cur = conn.execute(
        "UPDATE notes SET head_version_id = ? WHERE note_id = ? AND head_version_id = ?",
        (new_head, note_id, expected_parent),
    )
    if cur.rowcount != 1:
        actual_head, actual_head_body = _head(conn, note_id)
        raise HeadConflictError(
            note_id,
            expected_parent,
            actual_head,
            actual_head_body=actual_head_body,
            rejected_buffer=rejected_buffer,
        )


def _save_core(
    conn: sqlite3.Connection,
    note_id: str,
    body: str,
    *,
    parent: str = NO_PARENT,
    settings: Settings,
) -> SaveResult:
    """Create or update ``note_id`` to ``body`` on ``conn`` — no transaction boundary.

    The raw save logic (CAS-guarded head read, version insert, head pointer update)
    without any ``with conn:`` wrapper so a caller can fold this into a larger
    transaction.  All semantics are identical to :func:`save`; the only difference
    is **who owns the transaction**: here it is the caller, not this function.

    :func:`lode.repository.Repository.save` calls this inside its own ``with
    conn:`` so that the version-write and the derive-job enqueue commit atomically.
    :func:`save` (below) wraps this in ``with conn:`` for direct, standalone use.
    """
    head, head_body = _head(conn, note_id)

    if head is None:
        # Create: a root version. A non-empty parent on an absent note, or a
        # note that already exists, is a conflict (you cannot re-root).
        if parent != NO_PARENT:
            raise HeadConflictError(note_id, parent, None, rejected_buffer=body)
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
        raise HeadConflictError(
            note_id,
            parent,
            head,
            actual_head_body=head_body,
            rejected_buffer=body,
        )
    if body == head_body:
        return SaveResult(note_id, head, "update", deduped=True)
    version_id = content_version_id(note_id, head, body, settings)
    _write_version(conn, version_id, note_id, head, body, "update")
    _cas_head(conn, note_id, version_id, head, body)
    return SaveResult(note_id, version_id, "update")


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

    The transaction is owned by this function (``with conn:`` commits or rolls back
    on return). Callers that need to fold the version-write into a larger transaction
    — specifically :class:`lode.repository.Repository`, which wraps the write and
    the derive-job enqueue atomically — use :func:`_save_core` directly.
    """
    settings = settings or Settings()
    with conn:
        return _save_core(conn, note_id, body, parent=parent, settings=settings)


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

    **Idempotent re-delete (lode-n8q):** ``version_id`` is content-addressed
    (:func:`~lode.hashing.content_version_id`, which folds in ``note_id`` /
    ``parent`` / ``body`` but not ``op``). A delete → recover → delete cycle on
    an unchanged note reproduces the exact ``(note_id, parent, body)`` of the
    first tombstone, so the second delete recomputes the identical
    ``version_id``. That is content-addressing working correctly, not a
    collision — so instead of re-inserting (which would violate the primary
    key), this repoints the head to the existing tombstone row. No new version
    is written and the returned :class:`SaveResult` carries the same
    ``version_id`` as the original delete.
    """
    settings = settings or Settings()
    with conn:
        head, head_body = _head(conn, note_id)
        if head is None:
            raise KeyError(note_id)
        if parent != head:
            # A delete carries no user buffer, so rejected_buffer is None; the
            # new head still surfaces so the UI can re-confirm against the change.
            raise HeadConflictError(
                note_id,
                parent,
                head,
                actual_head_body=head_body,
                rejected_buffer=None,
            )
        version_id = content_version_id(note_id, head, head_body, settings)
        existing = conn.execute(
            "SELECT 1 FROM versions WHERE version_id = ? AND note_id = ?",
            (version_id, note_id),
        ).fetchone()
        if existing is None:
            _write_version(conn, version_id, note_id, head, head_body, "delete")
        _cas_head(conn, note_id, version_id, head, rejected_buffer=None)
        return SaveResult(note_id, version_id, "delete")


def recover(
    conn: sqlite3.Connection,
    note_id: str,
    *,
    target_version: str,
) -> SaveResult:
    """Recover a soft-deleted note by repointing its head to ``target_version``.

    Recovery is *not* a new version (``docs/storage.md``: "recovery = repoint the
    head") — it simply moves the head pointer to an existing version of this note,
    typically the pre-delete head. ``target_version`` must be a version that
    belongs to ``note_id`` (``KeyError`` otherwise). No hashing is involved, so —
    unlike :func:`save` / :func:`delete` — there is no ``settings`` parameter.
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


def purge(conn: sqlite3.Connection, note_id: str) -> PurgeResult:
    """Hard-delete ``note_id``: overwrite its whole chain and drop ``ai`` annotations.

    The deliberate immutability break (``docs/externals.md`` "Hard delete"). In one
    transaction it:

    - overwrites **every** version body of the note — the live head, prior updates,
      and soft-delete tombstones alike — with a ``[purged YYYY-MM-DD]`` marker and
      stamps ``purged_at`` (a secret pasted then edited-around survives in older
      versions, so the sweep is chain-wide, not head-only);
    - keeps ``version_id`` / ``parent_version_id`` / ``op`` / ``created`` so lineage
      and undo structure survive — only the sensitive bytes die. ``purged_at`` is
      now the structural "this id no longer hashes to its body" flag;
    - drops the chain's regenerable ``source='ai'`` annotations (version-scoped, so
      keyed by ``source_version``), keeping ``source='user'`` corrections — those
      attach to the logical note and are curation, not content
      (``docs/storage.md`` "Provenance & user override").

    Returns a :class:`PurgeResult` carrying the swept version ids and the live head,
    which :class:`lode.repository.Repository` uses to drive the cache-side cascade
    (LanceDB vectors + FTS rows) through its cache seam. ``KeyError`` if the note
    does not exist. Idempotent: re-purging a purged note re-stamps the same marker.
    """
    with conn:
        row = conn.execute(
            "SELECT n.head_version_id, v.op FROM notes n "
            "JOIN versions v ON v.version_id = n.head_version_id "
            "WHERE n.note_id = ?",
            (note_id,),
        ).fetchone()
        if row is None:
            raise KeyError(note_id)
        head_version_id, head_op = row
        # ORDER BY rowid, never ``created`` -- and not ``created, rowid`` either:
        # ``created`` is wall-clock, and the OS can step that backward, so a later
        # version can sort *before* its own parent. That is a wrong order, not a
        # tie, so a tiebreaker never even runs. ``rowid`` is insertion order, and
        # a child's parent FK must already exist, so insertion order *is* chain
        # order. Full rule: docs/storage.md, "Ordering a version chain" (lode-t1y).
        version_ids = tuple(
            r[0]
            for r in conn.execute(
                "SELECT version_id FROM versions WHERE note_id = ? ORDER BY rowid",
                (note_id,),
            )
        )
        now = datetime.now(timezone.utc)
        marker = f"[purged {now:%Y-%m-%d}]"
        purged_at = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        conn.execute(
            "UPDATE versions SET body = ?, purged_at = ? WHERE note_id = ?",
            (marker, purged_at, note_id),
        )
        conn.execute(
            "DELETE FROM annotations WHERE source = 'ai' AND source_version IN "
            "(SELECT version_id FROM versions WHERE note_id = ?)",
            (note_id,),
        )
        return PurgeResult(note_id, head_version_id, head_op, marker, version_ids)
