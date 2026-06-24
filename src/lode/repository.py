"""Thin repository façade: irreplaceable SQLite rows vs the swappable cache (lode-s2f.5).

The store has two halves whose boundary is by *rows / value, not by file*
(``docs/stack.md``, "The partition is by rows, not by file"; ``docs/storage.md``):

- the **irreplaceable** rows — ``notes``/``versions`` (and, later, ``externals``/
  ``snapshots`` plus the ``source = user`` curation rows) — owned content and
  genuine user decisions that must be backed up. They live in SQLite and the
  repository owns them directly, via :mod:`lode.versions`.
- the **regenerable cache** — passage vectors in LanceDB, the FTS5 lexical index,
  the networkx knowledge graph — rebuilt from the irreplaceable rows, so losing
  it costs only a rebuild, never data.

This module is the seam between them: the irreplaceable save path runs against
SQLite, and every head change is handed to a pluggable :class:`CacheBackend`.
Callers depend only on :class:`Repository`, so the cache *engine* is swappable
(a fake in tests, ``NullCache`` until a real engine lands, LanceDB/sqlite-vec in
production) **without touching the core** (``docs/stack.md``: "Keep the cache
behind a repository interface … so LanceDB can be swapped"). It deliberately
does no chunking/embedding/indexing itself — that pipeline is the cache engine's
own job, wired by later tickets; this seam only carries *which* version changed
and its body, the regeneration input.
"""

import sqlite3
from typing import Protocol, runtime_checkable

from lode import versions
from lode.config import Settings
from lode.hashing import NO_PARENT
from lode.versions import SaveResult


@runtime_checkable
class CacheBackend(Protocol):
    """The swappable regenerable-cache engine, behind a thin two-method seam.

    The repository drives a backend from the version-save path: each head change
    re-indexes that version's body (:meth:`index`), each soft-delete drops it
    (:meth:`evict`). A backend turns those signals into whatever its engine needs
    — chunk + embed into LanceDB, write the FTS5 row, update the networkx graph —
    but that is entirely the backend's concern, so the engine can be swapped
    without the core knowing which one is in use.
    """

    def index(self, note_id: str, version_id: str, body: str) -> None:
        """A note's head is now ``version_id`` with ``body`` — (re)build its cache."""
        ...

    def evict(self, note_id: str, version_id: str) -> None:
        """The note's head is a delete tombstone — drop it from the cache."""
        ...


class NullCache:
    """A cache backend that does nothing — the default until a real engine lands.

    The real engines (LanceDB/FTS5/networkx) are wired by later tickets; until
    then the irreplaceable save path runs against this no-op, so the storage core
    works standalone. It also pins the seam's shape: any swap-in backend
    implements these same two methods.
    """

    def index(self, note_id: str, version_id: str, body: str) -> None:
        pass

    def evict(self, note_id: str, version_id: str) -> None:
        pass


class Repository:
    """Façade over the irreplaceable SQLite store plus a swappable cache backend.

    The irreplaceable ops — save/delete/recover a note's version chain — run
    against SQLite via :mod:`lode.versions`, preserving its CAS-guarded contract
    (a stale-parent save raises :class:`~lode.versions.HeadConflictError`, an
    identical body is a no-op dedup). After each *successful* irreplaceable write
    the matching cache op is delegated to the :class:`CacheBackend`:

    - a non-dedup save → :meth:`CacheBackend.index` (a deduped save changed no
      body, so the cache is already current and is left untouched);
    - a delete → :meth:`CacheBackend.evict` (the head is now a tombstone);
    - a recover → :meth:`CacheBackend.index` of the repointed head's body.

    The cache is touched only after the irreplaceable write has committed, so a
    cache-engine failure can never corrupt the owned data — the cache is
    regenerable, the source rows are not.
    """

    def __init__(
        self, conn: sqlite3.Connection, cache: CacheBackend | None = None
    ) -> None:
        self.conn = conn
        self.cache = cache or NullCache()

    def save(
        self,
        note_id: str,
        body: str,
        *,
        parent: str = NO_PARENT,
        settings: Settings | None = None,
    ) -> SaveResult:
        """Create/update ``note_id`` (see :func:`lode.versions.save`), then index it."""
        result = versions.save(
            self.conn, note_id, body, parent=parent, settings=settings
        )
        if not result.deduped:
            self.cache.index(note_id, result.version_id, body)
        return result

    def delete(
        self,
        note_id: str,
        *,
        parent: str,
        settings: Settings | None = None,
    ) -> SaveResult:
        """Soft-delete ``note_id`` (see :func:`lode.versions.delete`), then evict it."""
        result = versions.delete(self.conn, note_id, parent=parent, settings=settings)
        self.cache.evict(note_id, result.version_id)
        return result

    def recover(self, note_id: str, *, target_version: str) -> SaveResult:
        """Recover ``note_id`` (see :func:`lode.versions.recover`), then re-index it."""
        result = versions.recover(self.conn, note_id, target_version=target_version)
        self.cache.index(note_id, result.version_id, self._body(result.version_id))
        return result

    def _body(self, version_id: str) -> str:
        """Read a version's body from the irreplaceable store (cache regen input)."""
        (body,) = self.conn.execute(
            "SELECT body FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return body
