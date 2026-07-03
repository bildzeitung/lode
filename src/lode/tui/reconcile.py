"""The CAS-conflict reconciliation flow (lode-mkc.4) — "changed since you
opened it," resolved by hand.

``docs/storage.md`` ("What the user sees when CAS rejects a save"): the
storage layer's contract stops at the honest CAS reject plus the structured
:class:`~lode.versions.HeadConflictError` it hands back (the rejected buffer,
the new head's version id + body) — it persists nothing itself. *Persisting*
that buffer as a durable draft and offering interactive re-apply/discard is
"the TUI (E11) owns the interactive re-apply/discard store," and this module
is that store: the **one** place any TUI save path turns a CAS reject into a
:class:`Conflict` (a preserved draft + everything the reconcile screen needs
for the diff), and the **one** place a conflict is resolved — re-applied
(re-parented onto the new head, via the exact same
:meth:`~lode.repository.Repository.save` CAS path every other save uses) or
discarded (the draft dropped). :mod:`lode.tui.capture` is one caller today;
a future edit screen would be another, through the same two functions.

No merge machinery here, on purpose (``docs/storage.md``): a renewed conflict
on re-apply (the head moved again while the user was looking at the diff)
comes back as a fresh :class:`Conflict` rather than being silently retried,
same as the honest reject it wraps.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lode.config import Settings
from lode.hashing import NO_PARENT
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.versions import HeadConflictError, SaveResult


@dataclass(frozen=True)
class Conflict:
    """A CAS-rejected save, with everything the reconcile screen needs.

    Mirrors :class:`~lode.versions.HeadConflictError`'s payload plus the
    draft file the rejected buffer was preserved to
    (``docs/storage.md``: "the rejected buffer is preserved as a draft until
    they resolve it").
    """

    note_id: str
    expected_parent: str
    rejected_buffer: str
    actual_head: str | None
    actual_head_body: str | None
    draft_path: Path


def write_draft(db_path: Path, note_id: str, body: str) -> Path:
    """Persist a CAS-rejected buffer beside the DB — the TUI's one draft store.

    A plain temp file, not a DB row (``docs/storage.md`` defers a dedicated
    ``drafts`` table until the system is exercised in production) — enough
    that an unlucky CAS loss never costs the unsaved edit.
    """
    fd, name = tempfile.mkstemp(
        prefix=f"{note_id}.", suffix=".draft", dir=db_path.parent
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return Path(name)


def conflict_from_error(db_path: Path, error: HeadConflictError) -> Conflict:
    """Turn a raised :class:`HeadConflictError` into a :class:`Conflict`.

    Writes the rejected buffer to a draft immediately — the conflict is
    "preserved as a draft" from the moment it exists, not only once the
    reconcile screen gets around to showing it.
    """
    draft_path = write_draft(db_path, error.note_id, error.rejected_buffer or "")
    return Conflict(
        note_id=error.note_id,
        expected_parent=error.expected_parent,
        rejected_buffer=error.rejected_buffer or "",
        actual_head=error.actual_head,
        actual_head_body=error.actual_head_body,
        draft_path=draft_path,
    )


def reapply(
    db_path: Path, conflict: Conflict, *, settings: Settings | None = None
) -> SaveResult | Conflict:
    """Re-parent ``conflict``'s buffer onto the new head and save it.

    Drives the identical ``Repository.save`` + synchronous-FTS5-only cache
    seam :func:`lode.tui.capture.save_capture` uses, so a re-applied edit is
    indexed exactly like any other TUI save. On success the now-resolved
    draft is deleted. If the head moved *again* in the meantime, this raises
    no exception — it returns a fresh :class:`Conflict` (a new draft, the
    newer head; the superseded draft is dropped) for the caller to show and
    resolve, same honest-reject contract as the original save.
    """
    settings = settings or Settings()
    conn = init_db(db_path)
    try:
        repo = Repository(conn, cache=CompositeCache([LexicalCacheBackend(conn)]))
        try:
            result = repo.save(
                conflict.note_id,
                conflict.rejected_buffer,
                parent=conflict.actual_head or NO_PARENT,
                settings=settings,
            )
        except HeadConflictError as exc:
            # The head moved again while the user was resolving. Supersede the
            # now-stale draft with the renewed conflict's fresh one (identical
            # buffer, newer head) so repeated re-applies against a moving head
            # never accumulate orphaned drafts.
            renewed = conflict_from_error(db_path, exc)
            discard(conflict)
            return renewed
        discard(conflict)
        return result
    finally:
        conn.close()


def discard(conflict: Conflict) -> None:
    """Resolve ``conflict`` by dropping the edit — removes its preserved draft.

    The live head is left exactly as it was; discarding never touches it.
    """
    conflict.draft_path.unlink(missing_ok=True)
