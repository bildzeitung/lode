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
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from lode import versions
from lode.config import Settings
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.versions import SaveResult


class EmptyCaptureError(Exception):
    """Raised by :func:`save_capture` on an empty/whitespace-only buffer.

    Mirrors ``lode add``'s "refusing to save an empty note" refusal
    (``lode.cli.add``) so the same rule holds however a note is captured.
    """


@dataclass(frozen=True)
class CaptureConflict:
    """A create-path CAS reject, surfaced the same way ``lode add`` does.

    Practically unreachable in normal use — each capture mints a fresh
    ``uuid4`` note id, so there is nothing for the compare-and-swap to
    collide with — but handled rather than assumed away, exactly like
    ``lode add``'s own fallback. The rejected buffer is preserved as a draft
    beside the DB rather than lost (``docs/storage.md`` "What the user sees
    when CAS rejects a save").
    """

    draft_path: Path


def _write_draft(db_path: Path, note_id: str, body: str) -> Path:
    """Persist a CAS-rejected capture buffer beside the DB so it is never lost.

    Mirrors ``lode.cli._write_draft`` (``lode add``'s identical fallback) but
    is not imported from there: ``docs/storage.md`` is explicit that "the TUI
    (E11) owns the interactive re-apply/discard store" as its own mechanism,
    not one shared with the CLI, and this keeps the TUI's capture wiring free
    of any dependency on the Typer CLI module.
    """
    fd, name = tempfile.mkstemp(
        prefix=f"{note_id}.", suffix=".draft", dir=db_path.parent
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return Path(name)


def save_capture(
    db_path: Path, body: str, *, settings: Settings | None = None
) -> SaveResult | CaptureConflict:
    """Persist a captured note instantly — no AI call anywhere in this path.

    Mints a fresh ``uuid4`` note id (a capture always creates, never edits an
    existing note), refuses an empty/whitespace-only body
    (:class:`EmptyCaptureError`), then saves through
    :meth:`~lode.repository.Repository.save` behind the same capture-path
    cache composite ``lode add`` uses (:class:`~lode.lexical.LexicalCacheBackend`
    only — embedding stays async). A CAS reject (see
    :class:`CaptureConflict`) preserves the buffer as a draft rather than
    losing it.
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
        except versions.HeadConflictError:
            return CaptureConflict(_write_draft(db_path, note_id, body))
    finally:
        conn.close()
