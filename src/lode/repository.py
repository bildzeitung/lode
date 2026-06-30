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
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from lode import jobs, staleness, versions
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


class CompositeCache:
    """One cache slot, many engines — the multiplexer behind the cache seam.

    The :class:`Repository` holds a *single* cache, but the regenerable cache is
    several engines that must all see every head change: passage vectors in
    LanceDB (:class:`lode.embedding.EmbeddingCacheBackend`, lode-x6r.3/x6r.2), the
    FTS5 lexical index (lode-x6r.4), and later the networkx knowledge graph. This
    backend *is itself* a :class:`CacheBackend` that simply fans each
    :meth:`index` / :meth:`evict` out to its member engines in order. So the
    composition decision is: the Repository never grows a second slot or learns
    the engine list — adding the FTS leg is appending one engine to the composite
    at the wiring point, and every engine plugs into the same two-method seam
    rather than inventing its own (``docs/storage.md`` "the whole shape sits behind
    a thin repository interface"; ``docs/stack.md`` "Keep the cache behind a
    repository interface").

    Fan-out is sequential and order-preserving (engines are driven in the order
    given); it does no error isolation, so a failing engine propagates — the
    cache is touched only after the irreplaceable write has committed
    (:class:`Repository`), so a propagated failure costs a rebuild, never data.
    """

    def __init__(self, backends: Iterable[CacheBackend]) -> None:
        self._backends = tuple(backends)

    def index(self, note_id: str, version_id: str, body: str) -> None:
        for backend in self._backends:
            backend.index(note_id, version_id, body)

    def evict(self, note_id: str, version_id: str) -> None:
        for backend in self._backends:
            backend.evict(note_id, version_id)


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

    **Enqueue ownership (lode-i05.1):** :meth:`save` is the **sole enqueue site**
    for derive jobs. It wraps the version-write (:func:`~lode.versions._save_core`)
    and the enqueue (:func:`~lode.jobs.enqueue_derive_jobs`) in a single ``with
    conn:`` so both commit atomically — no version without its jobs, no jobs without
    the version. A deduped save writes no row and enqueues nothing. Direct callers
    (e.g. the CLI) must go through :meth:`save`, never call
    :func:`lode.jobs.enqueue_derive_jobs` separately.

    **Enqueue scope (lode-npx.2):** only the ``embed`` job is enqueued here. The
    ``enrich`` job is NOT enqueued from the capture path — the CLI calls
    :func:`lode.enrich.enrich_version` immediately after :meth:`save` for the
    interactive one-shot (seconds, full Haiku price). Bulk / backfill enrich jobs
    are enqueued by the reconciliation scan (``enrich_gap`` step in
    :mod:`lode.reconcile`) and submitted to the Batches API by the worker.

    **Re-anchor ownership (lode-atv):** an ``update`` save also re-anchors the
    prior head's AI annotations/edges against the new body, in the same ``with
    conn:`` transaction — :func:`~lode.staleness.reanchor_annotations` and
    :func:`~lode.staleness.reanchor_edges`. This only runs for ``op == "update"``
    (a ``create`` has no prior AI-derived layer to re-anchor yet) and only on a
    non-dedup save (a deduped save changed no body, so nothing needs reclassifying).
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
        """Create/update ``note_id`` and enqueue its derive jobs, atomically.

        Wraps :func:`~lode.versions._save_core` (the CAS-guarded version-write)
        and :func:`~lode.jobs.enqueue_derive_jobs` in a single ``with conn:``
        transaction, so "write version row + enqueue its derive jobs" either commits
        together or rolls back together (``docs/storage.md`` §E2, pinned 2026-06-28,
        lode-i05.1). A deduped save (identical body → no-op) enqueues nothing and
        leaves the cache untouched.

        After a successful commit, the cache backend is driven:
        - non-dedup save → :meth:`CacheBackend.index`;
        - cache is not touched on a dedup.

        An ``update`` (non-dedup) save also re-anchors ``note_id``'s AI
        annotations/edges against the new body in the same transaction
        (lode-atv) — ``create`` has no prior AI-derived layer yet, and a dedup
        changed no body, so both are skipped.
        """
        settings = settings or Settings()
        with self.conn:
            result = versions._save_core(
                self.conn, note_id, body, parent=parent, settings=settings
            )
            if not result.deduped:
                # Enqueue only the embed job here; enrich is handled by the CLI's
                # immediate Haiku call on the capture path, and by the worker's
                # batch-submit step for bulk / backfill (lode-npx.2).
                jobs.enqueue_derive_jobs(self.conn, result.version_id, types=("embed",))
                if result.op == "update":
                    staleness.reanchor_annotations(
                        self.conn, note_id, result.version_id, body
                    )
                    staleness.reanchor_edges(
                        self.conn, note_id, result.version_id, body
                    )
        # Cache is driven AFTER the txn commits so a cache failure never rolls
        # back the irreplaceable write (cache is regenerable, the source rows are not).
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

    def purge(self, note_id: str) -> versions.PurgeResult:
        """Hard-delete ``note_id`` (see :func:`lode.versions.purge`), then cascade.

        The irreplaceable side — overwrite every body in the chain with the
        ``[purged YYYY-MM-DD]`` marker, set ``purged_at``, drop ``source='ai'``
        annotations — runs first via :func:`lode.versions.purge`. Then the cache
        cascade runs **through the cache seam**, never reaching into an engine
        module: every swept version is :meth:`CacheBackend.evict`-ed, so the
        :class:`CompositeCache` fans the drop to every engine (LanceDB vectors, FTS
        rows, …). This is the **note-wide hard cascade** the soft-delete evict
        deliberately leaves to purge: an update/delete only ever indexes the new
        head, so each superseded version's vectors/FTS rows linger (retrieval just
        filters to the live head) — purge is the one op that clears the whole chain.

        Finally the live head is re-derived locally from the now-purged marker body
        (:meth:`CacheBackend.index`), so a purged note stays present in the index as
        ``[purged …]`` without leaking the original content — unless the head is a
        soft-delete tombstone, which carries no passages of its own and so is left
        evicted (mirroring the normal delete path). The cache is touched only after
        the irreplaceable rewrite has committed, so a cache-engine failure costs a
        rebuild, never the purge.
        """
        result = versions.purge(self.conn, note_id)
        for version_id in result.purged_versions:
            self.cache.evict(note_id, version_id)
        if result.head_op != "delete":
            self.cache.index(note_id, result.head_version_id, result.marker_body)
        return result

    def _body(self, version_id: str) -> str:
        """Read a version's body from the irreplaceable store (cache regen input)."""
        (body,) = self.conn.execute(
            "SELECT body FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return body
