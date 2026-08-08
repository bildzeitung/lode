"""Edit-path save wiring for the TUI's browse-to-edit flow (lode-0wj.6).

Pure I/O, no widget/App state — same convention as :mod:`lode.tui.services.capture` /
:mod:`lode.notes_read` — so it is unit-testable without spinning up a Textual
app; :class:`~lode.tui.screens.edit.EditScreen` is its only caller.

**Update, never create — the whole point of this module.** :func:`save_capture`
always mints a fresh ``uuid4`` note id (a capture only ever creates); this
module's :func:`save_edit` is the mirror for the opposite case — the note
already exists, and the caller has already loaded its current head
(:func:`load_head`) into an editable buffer. Saving reparents the buffer onto
that loaded head via the exact same CAS-guarded
:meth:`~lode.repository.Repository.save` path (``docs/storage.md``
"event-sourced, linear per-note chains") — a live head that moved since the
buffer was loaded (a second edit session, a concurrent process) is rejected
exactly like any other update, and handed to
:func:`lode.tui.services.reconcile.conflict_from_error` for the same preserved-draft,
manual-reconciliation contract every other TUI save path shares. No new note
is ever minted here.

**Empty-body refusal mirrors capture's (lode-mkc.1).** A whitespace-only save
is refused the same way :func:`~lode.tui.services.capture.save_capture` refuses an
empty capture — this module does not grow "clear the buffer to delete the
note" semantics; deletion is :meth:`~lode.repository.Repository.delete`'s own
explicit, separately-confirmed path, not an accidental side effect of saving
blank.

**Delete from browse (lode-d32.1).** That explicit path is :func:`delete_note`
— the soft-delete counterpart to :func:`save_edit`, called by
:class:`~lode.tui.screens.browse.BrowseScreen` after its own confirm modal.
It goes through :class:`~lode.repository.Repository` (the same cache-backed
seam :func:`save_edit` uses) rather than :func:`lode.versions.delete`
directly, so the FTS/lexical cache leg is evicted along with the tombstone
write (the epic's own ``/debate`` pass on lode-d32 called this out: skipping
Repository would leave a "deleted" note still keyword-findable). Unlike
:func:`save_edit`, a CAS reject is **not** turned into a preserved-draft
:class:`~lode.tui.services.reconcile.Conflict` here — a delete carries no user buffer
to preserve, so it is simplest for the caller to let
:class:`~lode.versions.HeadConflictError` propagate, notify, and reload the
list (which already reflects the current state either way).
"""

from __future__ import annotations

from pathlib import Path

from lode.config import Settings
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.services.reconcile import Conflict, conflict_from_error
from lode.versions import HeadConflictError, SaveResult

#: Alias kept for readability at edit's call sites — an edit-path CAS reject
#: is a :class:`lode.tui.services.reconcile.Conflict` like any other TUI save path's.
EditConflict = Conflict


class EmptyEditError(Exception):
    """Raised by :func:`save_edit` on an empty/whitespace-only buffer.

    Mirrors :class:`~lode.tui.services.capture.EmptyCaptureError` so the same "refuse
    an empty note" rule holds however a note reaches the store.
    """


def load_head(db_path: Path, note_id: str) -> tuple[str, str] | None:
    """Return ``(head_version_id, head_body)`` for ``note_id``'s live head.

    ``None`` if the note is absent or its head is a soft-delete tombstone (``op =
    'delete'``) — same "live heads only" guard :func:`lode.notes_read.list_notes`
    applies, since a tombstoned note has nothing editable to load.
    :class:`~lode.tui.screens.edit.EditScreen` uses this both to seed the editable
    buffer and to remember the CAS ``parent`` its save must reparent onto.
    """
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT n.head_version_id, v.body FROM notes n "
            "JOIN versions v ON v.version_id = n.head_version_id "
            "WHERE n.note_id = ? AND v.op != 'delete'",
            (note_id,),
        ).fetchone()
        return (row[0], row[1]) if row is not None else None
    finally:
        conn.close()


def delete_note(
    db_path: Path,
    note_id: str,
    *,
    parent: str,
    settings: Settings | None = None,
) -> SaveResult:
    """Soft-delete ``note_id`` via the CAS-guarded tombstone path (lode-d32.1).

    Goes through :class:`~lode.repository.Repository`, the same cache-backed
    seam :func:`save_edit` uses, rather than calling :func:`lode.versions.delete`
    directly — so the FTS/lexical cache leg is evicted along with the
    irreplaceable tombstone write. Skipping the Repository would leave a
    "deleted" note still keyword-findable until something else happened to
    touch the cache.

    Unlike :func:`save_edit`, a CAS reject (:class:`~lode.versions.HeadConflictError` —
    someone else edited or deleted the note first) is **not** converted into a
    preserved-draft :class:`~lode.tui.services.reconcile.Conflict` here: a delete
    carries no user buffer to preserve, so there is nothing to reconcile. It is simplest
    for the caller (:class:`~lode.tui.screens.browse.BrowseScreen`) to let the exception
    propagate, notify, and reload the list — which already reflects the current state
    either way.
    """
    settings = settings or Settings()
    conn = init_db(db_path)
    try:
        repo = Repository(conn, cache=CompositeCache([LexicalCacheBackend(conn)]))
        return repo.delete(note_id, parent=parent, settings=settings)
    finally:
        conn.close()


def save_edit(
    db_path: Path,
    note_id: str,
    body: str,
    *,
    parent: str,
    settings: Settings | None = None,
) -> SaveResult | Conflict:
    """Reparent ``body`` onto ``parent`` (the loaded head) — never mints a note.

    Refuses an empty/whitespace-only ``body`` (:class:`EmptyEditError`), then
    saves through :meth:`~lode.repository.Repository.save` behind the same
    capture-path cache composite (:class:`~lode.lexical.LexicalCacheBackend`
    only — embedding stays async). A CAS reject (the head moved since
    ``parent`` was loaded) is handed to
    :func:`lode.tui.services.reconcile.conflict_from_error`, which preserves the
    buffer as a draft and returns the :class:`~lode.tui.services.reconcile.Conflict`
    the caller resolves (re-apply / discard) exactly like a capture-path
    reject would.

    An unchanged buffer (``body`` identical to the loaded head's) is not a
    special case here — :meth:`~lode.repository.Repository.save` already
    dedups a body-identical update into a no-op (``SaveResult.deduped``), so
    saving an untouched load simply writes nothing and returns the unchanged
    head, same as it would for any other update path.
    """
    if not body.strip():
        raise EmptyEditError("refusing to save an empty note")

    settings = settings or Settings()
    conn = init_db(db_path)
    try:
        repo = Repository(conn, cache=CompositeCache([LexicalCacheBackend(conn)]))
        try:
            return repo.save(note_id, body, parent=parent, settings=settings)
        except HeadConflictError as exc:
            return conflict_from_error(db_path, exc)
    finally:
        conn.close()
