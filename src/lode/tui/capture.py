"""Capture-path save wiring for the TUI's capture screen (lode-mkc.1).

Pure I/O, no widget/App state, so it is unit-testable without spinning up a
Textual app — :class:`~lode.tui.screens.capture.CaptureScreen` is its only
caller. It drives the exact same seams ``lode add`` (``lode.cli.add``) drives:
:meth:`~lode.repository.Repository.save` (the atomic version-write + derive-job
enqueue, ``docs/storage.md`` "Enqueue ownership") behind a
:class:`~lode.repository.CompositeCache` holding only
:class:`~lode.lexical.LexicalCacheBackend` — the synchronous, model-free FTS5
leg (lode-xyb) that makes a fresh note keyword-findable the instant the
transaction commits.

**No AI call anywhere in this path (the ticket's acceptance criterion) —
stricter than ``lode add``.** ``Repository.save`` enqueues both the ``embed``
and ``enrich`` derive jobs atomically with the version write, same as ``lode
add``, but this module never follows up with ``lode add``'s opportunistic
immediate-enrich claim (``lode.cli._enrich_immediately``): that CLI-only
optimization makes one blocking Haiku call before the command returns, which
would put a real AI call back in the capture path. Here both jobs are simply
left ``pending`` for the async ``lode work`` drain to pick up later
(``docs/design.md`` §1's "async, fast, local" / "async, slow" tiers) — capture
itself only ever waits on the synchronous version-write + FTS5 tier.

**CAS-reject handling lives in :mod:`lode.tui.reconcile` (lode-mkc.4).** A
capture-path reject is practically unreachable in normal use — each capture
mints a fresh ``uuid4`` note id, so there is nothing for the compare-and-swap
to collide with — but it is handled rather than assumed away, exactly like
``lode add``'s own fallback, and routed through the same one draft
store/reconcile flow every TUI save path shares rather than keeping a
capture-only copy of it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from lode.config import Settings
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.reconcile import Conflict, conflict_from_error
from lode.versions import HeadConflictError, SaveResult

#: Alias kept for readability at capture's call sites and for existing
#: callers/tests: a capture-path CAS reject is a
#: :class:`lode.tui.reconcile.Conflict` like any other TUI save path's.
CaptureConflict = Conflict


class EmptyCaptureError(Exception):
    """Raised by :func:`save_capture` on an empty/whitespace-only buffer.

    Mirrors ``lode add``'s "refusing to save an empty note" refusal
    (``lode.cli.add``) so the same rule holds however a note is captured.
    """


def save_capture(
    db_path: Path, body: str, *, settings: Settings | None = None
) -> SaveResult | Conflict:
    """Persist a captured note instantly — no AI call anywhere in this path.

    Mints a fresh ``uuid4`` note id (a capture always creates, never edits an
    existing note), refuses an empty/whitespace-only body
    (:class:`EmptyCaptureError`), then saves through
    :meth:`~lode.repository.Repository.save` behind the same capture-path
    cache composite ``lode add`` uses (:class:`~lode.lexical.LexicalCacheBackend`
    only — embedding stays async). A CAS reject is handed to
    :func:`lode.tui.reconcile.conflict_from_error`, which preserves the
    buffer as a draft and returns the :class:`~lode.tui.reconcile.Conflict`
    the reconcile screen (lode-mkc.4) diffs and resolves.
    """
    if not body.strip():
        raise EmptyCaptureError("refusing to save an empty note")

    settings = settings or Settings()
    note_id = str(uuid.uuid4())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    try:
        repo = Repository(conn, cache=CompositeCache([LexicalCacheBackend(conn)]))
        try:
            return repo.save(note_id, body, settings=settings)
        except HeadConflictError as exc:
            return conflict_from_error(db_path, exc)
    finally:
        conn.close()
