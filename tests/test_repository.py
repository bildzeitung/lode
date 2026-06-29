"""Tests for lode.repository — the thin irreplaceable-vs-cache façade (lode-s2f.5).

Covers the acceptance criteria: the repository interface separates irreplaceable
ops (the SQLite version chain) from cache ops (a swappable engine behind
:class:`~lode.repository.CacheBackend`), and a *fake* cache implementation passes
the same core tests without changing callers.

The core-behaviour tests are parametrized over the cache backend — the default
``NullCache`` and a recording ``FakeCache`` — so the **identical caller code**
runs against both: that is the "swap the cache without touching the core"
acceptance, made executable. A second group asserts the seam actually fires
(index on create/update/recover, skipped on dedup, evict on delete).
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from lode.hashing import NO_PARENT, content_version_id
from lode.repository import CacheBackend, CompositeCache, NullCache, Repository
from lode.storage import init_db
from lode.versions import HeadConflictError


@dataclass
class CacheCall:
    """One recorded cache invocation: the op plus its arguments."""

    op: str  # "index" | "evict"
    note_id: str
    version_id: str
    body: str | None = None


class FakeCache:
    """A swap-in cache backend that records calls instead of indexing anything.

    Structurally a :class:`~lode.repository.CacheBackend` (duck-typed against the
    Protocol — see :func:`test_fake_cache_satisfies_the_backend_protocol`); it
    stands in for LanceDB/FTS5/networkx so the core save path can be exercised
    without a real engine.
    """

    def __init__(self) -> None:
        self.calls: list[CacheCall] = []

    def index(self, note_id: str, version_id: str, body: str) -> None:
        self.calls.append(CacheCall("index", note_id, version_id, body))

    def evict(self, note_id: str, version_id: str) -> None:
        self.calls.append(CacheCall("evict", note_id, version_id))


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


# --- swappable cache: the SAME core tests pass under either backend -----------
#
# `repo` is parametrized over the default no-op cache and the fake recorder. The
# test bodies below never mention the backend, so they are the unchanged caller;
# passing under both params is the swappable-cache acceptance criterion.


@pytest.fixture(params=["null", "fake"])
def repo(request, conn):
    cache = NullCache() if request.param == "null" else FakeCache()
    return Repository(conn, cache)


def _head(conn, note_id: str) -> str:
    (head,) = conn.execute(
        "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    ).fetchone()
    return head


def _count_versions(conn, note_id: str) -> int:
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM versions WHERE note_id = ?", (note_id,)
    ).fetchone()
    return n


def test_create_inserts_note_and_root_version(repo):
    result = repo.save("note-1", "hello")
    assert result.op == "create"
    assert result.version_id == content_version_id("note-1", NO_PARENT, "hello")
    assert _head(repo.conn, "note-1") == result.version_id


def test_update_chains_a_new_version_onto_the_head(repo):
    root = repo.save("note-1", "v1").version_id
    result = repo.save("note-1", "v2", parent=root)
    assert result.op == "update"
    assert _head(repo.conn, "note-1") == result.version_id
    assert _count_versions(repo.conn, "note-1") == 2


def test_update_with_stale_parent_is_rejected(repo):
    root = repo.save("note-1", "v1").version_id
    repo.save("note-1", "v2", parent=root)  # head moves past root
    with pytest.raises(HeadConflictError):
        repo.save("note-1", "conflict", parent=root)
    assert _count_versions(repo.conn, "note-1") == 2


def test_resaving_identical_body_is_a_noop_dedup(repo):
    root = repo.save("note-1", "same").version_id
    result = repo.save("note-1", "same", parent=root)
    assert result.deduped
    assert result.version_id == root
    assert _count_versions(repo.conn, "note-1") == 1


def test_delete_writes_a_tombstone(repo):
    root = repo.save("note-1", "body").version_id
    result = repo.delete("note-1", parent=root)
    assert result.op == "delete"
    assert _head(repo.conn, "note-1") == result.version_id
    assert _count_versions(repo.conn, "note-1") == 2


def test_recover_repoints_head_past_the_tombstone(repo):
    root = repo.save("note-1", "body").version_id
    repo.delete("note-1", parent=root)
    result = repo.recover("note-1", target_version=root)
    assert result.op == "recover"
    assert _head(repo.conn, "note-1") == root


def test_default_cache_is_the_null_backend(conn):
    """A repository with no cache argument falls back to the no-op NullCache."""
    repo = Repository(conn)
    assert isinstance(repo.cache, NullCache)
    # The core path still works against the default backend.
    assert repo.save("note-1", "hello").op == "create"


# --- the cache seam fires correctly (irreplaceable op -> cache op) ------------


def test_fake_cache_satisfies_the_backend_protocol():
    """The fake stands in structurally for any CacheBackend (swappability)."""
    assert isinstance(FakeCache(), CacheBackend)
    assert isinstance(NullCache(), CacheBackend)


def test_create_indexes_the_new_head(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    result = repo.save("note-1", "hello")
    assert cache.calls == [CacheCall("index", "note-1", result.version_id, "hello")]


def test_update_indexes_the_new_head(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    root = repo.save("note-1", "v1").version_id
    cache.calls.clear()
    result = repo.save("note-1", "v2", parent=root)
    assert cache.calls == [CacheCall("index", "note-1", result.version_id, "v2")]


def test_dedup_save_does_not_touch_the_cache(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    root = repo.save("note-1", "same").version_id
    cache.calls.clear()
    repo.save("note-1", "same", parent=root)  # no-op dedup
    assert cache.calls == []


def test_rejected_save_does_not_touch_the_cache(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    root = repo.save("note-1", "v1").version_id
    repo.save("note-1", "v2", parent=root)  # head moves
    cache.calls.clear()
    with pytest.raises(HeadConflictError):
        repo.save("note-1", "conflict", parent=root)
    assert cache.calls == []  # the CAS reject never reaches the cache


def test_delete_evicts_the_note(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    root = repo.save("note-1", "body").version_id
    cache.calls.clear()
    result = repo.delete("note-1", parent=root)
    assert cache.calls == [CacheCall("evict", "note-1", result.version_id)]


def test_recover_reindexes_the_recovered_head(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    root = repo.save("note-1", "body").version_id
    repo.delete("note-1", parent=root)
    cache.calls.clear()
    repo.recover("note-1", target_version=root)
    assert cache.calls == [CacheCall("index", "note-1", root, "body")]


# --- CompositeCache: one slot fans every head change out to N engines ----------
#
# The composition decision lode-1f9 settles: the repository keeps one cache slot,
# and the several regenerable engines (vectors, FTS5, graph) ride a CompositeCache
# that is itself a backend and forwards each index/evict to every member in order.


def test_composite_is_a_cache_backend():
    """The multiplexer plugs into the same seam it fans out to (it is a backend)."""
    assert isinstance(CompositeCache([]), CacheBackend)


def test_composite_fans_index_and_evict_to_every_engine_in_order(conn):
    a, b = FakeCache(), FakeCache()
    repo = Repository(conn, CompositeCache([a, b]))

    root = repo.save("note-1", "body").version_id
    repo.delete("note-1", parent=root)

    # Both engines saw the same head changes, in the same sequence.
    expected = [
        CacheCall("index", "note-1", root, "body"),
        CacheCall("evict", "note-1", _head(conn, "note-1")),
    ]
    assert a.calls == expected
    assert b.calls == expected


def test_composite_with_no_engines_is_a_safe_noop(conn):
    """An empty composite degrades to NullCache behaviour — the core still saves."""
    repo = Repository(conn, CompositeCache([]))
    assert repo.save("note-1", "hello").op == "create"


def test_composite_preserves_engine_order(conn):
    """Engines are driven in the order given (a shared log records the sequence)."""
    order: list[str] = []

    class _Named:
        def __init__(self, name: str) -> None:
            self.name = name

        def index(self, note_id: str, version_id: str, body: str) -> None:
            order.append(self.name)

        def evict(self, note_id: str, version_id: str) -> None:
            order.append(self.name)

    repo = Repository(conn, CompositeCache([_Named("vectors"), _Named("fts")]))
    repo.save("note-1", "hello")
    assert order == ["vectors", "fts"]


# --- purge: the note-wide hard cascade through the cache seam (lode-fk8.4) -----
#
# purge evicts EVERY version in the chain (the per-head soft-delete evict leaves
# superseded versions' cache rows behind), then re-derives the live head from the
# now-purged marker — unless that head is a soft-delete tombstone.


def test_purge_evicts_the_whole_chain_then_redrives_the_head(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    root = repo.save("note-1", "secret v1").version_id
    head = repo.save("note-1", "secret v2", parent=root).version_id
    cache.calls.clear()

    result = repo.purge("note-1")

    # Every version is evicted (chain-wide drop), then the live head is re-derived
    # locally from the purge marker so the note stays present without leaking.
    assert cache.calls == [
        CacheCall("evict", "note-1", root),
        CacheCall("evict", "note-1", head),
        CacheCall("index", "note-1", head, result.marker_body),
    ]


def test_purge_of_a_soft_deleted_note_does_not_reindex_the_tombstone(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    root = repo.save("note-1", "secret").version_id
    tombstone = repo.delete("note-1", parent=root).version_id
    cache.calls.clear()

    repo.purge("note-1")

    # A tombstone head carries no passages of its own, so it is left evicted (no
    # re-derive) — mirroring the normal delete path.
    assert cache.calls == [
        CacheCall("evict", "note-1", root),
        CacheCall("evict", "note-1", tombstone),
    ]


def test_purge_clears_the_secret_from_the_real_lexical_index(conn):
    from lode.lexical import LexicalCacheBackend, LexicalIndex

    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))
    root = repo.save("note-1", "the launch code is hunter2").version_id
    repo.save("note-1", "the launch code is hunter2 plus extra", parent=root)

    index = LexicalIndex(conn)
    assert index.search("hunter2", k=10)  # secret is findable before purge

    result = repo.purge("note-1")

    # No FTS row carries the original content anymore (every chain version swept).
    assert index.search("hunter2", k=10) == []
    # The head is re-derived to the marker, so the purged note stays findable.
    assert index.search("purged", k=10)
    assert result.head_op == "update"


# --- enqueue ownership + atomicity (lode-i05.1) --------------------------------
#
# Repository.save is the SOLE enqueue site: it wraps the version-write and the
# job enqueue in one WITH conn transaction.  These tests verify:
# - a successful save enqueues exactly the DERIVE_JOB_TYPES pending jobs;
# - a deduped save (identical body) enqueues nothing;
# - a failure during enqueue rolls back the version-write too (atomicity).


def test_save_enqueues_derive_jobs(conn):
    """repo.save enqueues the embed + enrich pending jobs for the new version."""
    from lode.jobs import DERIVE_JOB_TYPES

    repo = Repository(conn)
    result = repo.save("note-1", "hello")
    rows = conn.execute(
        "SELECT type, target_version, status FROM jobs ORDER BY type"
    ).fetchall()
    assert rows == [
        (job_type, result.version_id, "pending")
        for job_type in sorted(DERIVE_JOB_TYPES)
    ]


def test_save_dedup_enqueues_nothing(conn):
    """A no-op dedup save (identical body) does not enqueue any jobs."""
    repo = Repository(conn)
    root = repo.save("note-1", "same").version_id
    # Advance the head so the dedup is against the live body.
    result = repo.save("note-1", "same", parent=root)
    assert result.deduped
    # Still only the two jobs from the first (create) save.
    (n,) = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    from lode.jobs import DERIVE_JOB_TYPES

    assert n == len(DERIVE_JOB_TYPES)


def test_save_version_and_enqueue_are_atomic(tmp_path, monkeypatch):
    """An injected failure in enqueue rolls back the version-write too (atomicity).

    Verifies the lode-i05.1 acceptance criterion: "an injected failure between
    the version-write and the enqueue (or in the enqueue) rolls back BOTH — no
    version without its jobs, no jobs without the version."
    """
    import lode.jobs as jobs_mod
    from lode.storage import init_db as real_init_db

    db_path = tmp_path / "lode.db"
    conn = real_init_db(db_path)
    try:
        # Patch enqueue_derive_jobs to raise after the version row is written
        # but before anything is committed.
        def _boom(c, v):
            raise RuntimeError("injected enqueue failure")

        monkeypatch.setattr(jobs_mod, "enqueue_derive_jobs", _boom)

        repo = Repository(conn)
        with pytest.raises(RuntimeError, match="injected enqueue failure"):
            repo.save("note-1", "body")

        # The version-write rolled back: neither the version nor the note row exist.
        (n_versions,) = conn.execute("SELECT COUNT(*) FROM versions").fetchone()
        (n_notes,) = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        (n_jobs,) = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert n_versions == 0, "version row must roll back with the enqueue failure"
        assert n_notes == 0, "note row must roll back with the enqueue failure"
        assert n_jobs == 0, "no jobs should exist after a rolled-back save"
    finally:
        conn.close()
