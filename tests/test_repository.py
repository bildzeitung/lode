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

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from lode.hashing import NO_PARENT, content_version_id
from lode.repository import (
    AmbiguousNoteIdError,
    CacheBackend,
    CompositeCache,
    NullCache,
    Repository,
)
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


# --- redact-before-index (lode-n60): the cache seam sees redacted text --------


def test_save_hands_the_cache_seam_a_redacted_body(conn):
    """Regression for lode-n60: redact_before_index() had zero callers.

    A pasted secret matching the seed pattern set must be stripped from the
    text handed to CacheBackend.index — every engine fanned out to by
    CompositeCache (today, LexicalCacheBackend) sees redacted text, never the
    raw body. versions.body (the irreplaceable store) is untouched — only
    purge clears that durable copy (docs/externals.md "Two redactions").
    """
    from lode.redact import REDACTION_MARKER

    cache = FakeCache()
    repo = Repository(conn, cache)
    secret = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern
    body = f"key: {secret} done"

    result = repo.save("note-1", body)

    assert len(cache.calls) == 1
    call = cache.calls[0]
    assert call.op == "index"
    assert secret not in call.body
    assert REDACTION_MARKER in call.body
    # The irreplaceable store still carries the raw secret.
    (stored_body,) = conn.execute(
        "SELECT body FROM versions WHERE version_id = ?", (result.version_id,)
    ).fetchone()
    assert secret in stored_body


def test_recover_reindexes_the_recovered_head(conn):
    cache = FakeCache()
    repo = Repository(conn, cache)
    root = repo.save("note-1", "body").version_id
    repo.delete("note-1", parent=root)
    cache.calls.clear()
    repo.recover("note-1", target_version=root)
    assert cache.calls == [CacheCall("index", "note-1", root, "body")]


def test_recover_hands_the_cache_seam_a_redacted_body(conn):
    """Regression for lode-ibv: recover() fed cache.index() an unredacted body.

    Same shape as ``test_save_hands_the_cache_seam_a_redacted_body`` (lode-n60):
    a secret-bearing version, once recovered, must reach CacheBackend.index
    with the secret stripped — not the raw body read off ``_body``.
    versions.body (the irreplaceable store) is untouched — only purge clears
    that durable copy (docs/externals.md "Two redactions").
    """
    from lode.redact import REDACTION_MARKER

    cache = FakeCache()
    repo = Repository(conn, cache)
    secret = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern
    body = f"key: {secret} done"

    root = repo.save("note-1", body).version_id
    repo.delete("note-1", parent=root)
    cache.calls.clear()
    repo.recover("note-1", target_version=root)

    assert len(cache.calls) == 1
    call = cache.calls[0]
    assert call.op == "index"
    assert secret not in call.body
    assert REDACTION_MARKER in call.body
    # The irreplaceable store still carries the raw secret.
    (stored_body,) = conn.execute(
        "SELECT body FROM versions WHERE version_id = ?", (root,)
    ).fetchone()
    assert secret in stored_body


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


# --- resolve_note_prefix: prefix -> full id, scoped to LIVE notes (lode-1gr.3) -
#
# Shared resolver behind `lode purge <prefix>` (and, later, `lode show`,
# lode-1gr.5): a full 36-char id always bypasses resolution (works regardless
# of note state, unchanged from before this ticket); anything shorter resolves
# only against LIVE notes (same `v.op != 'delete'` guard Browse uses) to
# exactly one match, or raises.

_FULL_ID = "0" * 36  # not a real uuid4, just 36 chars — length is all that matters


def test_resolve_note_prefix_returns_a_full_length_id_unchanged(conn):
    repo = Repository(conn)

    # No note with this id exists at all — a full id is never resolved/looked
    # up here, just passed through so purge's own KeyError still fires.
    assert repo.resolve_note_prefix(_FULL_ID) == _FULL_ID


def test_resolve_note_prefix_resolves_a_unique_prefix(conn):
    repo = Repository(conn)
    repo.save("note-aaa111", "body a")
    repo.save("note-bbb222", "body b")

    assert repo.resolve_note_prefix("note-aaa") == "note-aaa111"


def test_resolve_note_prefix_raises_keyerror_when_nothing_matches(conn):
    repo = Repository(conn)
    repo.save("note-aaa111", "body a")

    with pytest.raises(KeyError):
        repo.resolve_note_prefix("ghost")


def test_resolve_note_prefix_rejects_an_empty_prefix(conn):
    repo = Repository(conn)
    repo.save("note-aaa111", "body a")

    # An empty string is not an unambiguous prefix of anything; it must not
    # resolve to (and let `lode purge ""` sweep) the sole live note.
    with pytest.raises(KeyError):
        repo.resolve_note_prefix("")


def test_resolve_note_prefix_raises_ambiguous_for_multiple_live_matches(conn):
    repo = Repository(conn)
    repo.save("note-aaa111", "body a")
    repo.save("note-aaa222", "body b")

    with pytest.raises(AmbiguousNoteIdError) as excinfo:
        repo.resolve_note_prefix("note-aaa")

    # Nothing was purged — the caller decides what to do, this only reports.
    assert set(excinfo.value.candidates) == {"note-aaa111", "note-aaa222"}


def test_resolve_note_prefix_excludes_a_tombstoned_note(conn):
    repo = Repository(conn)
    root = repo.save("note-aaa111", "body a").version_id
    repo.delete("note-aaa111", parent=root)

    # The tombstoned note is not reachable by prefix — it isn't in Browse
    # either — even though it is the only note with that prefix.
    with pytest.raises(KeyError):
        repo.resolve_note_prefix("note-aaa")


# --- enqueue ownership + atomicity (lode-i05.1 / lode-npx.2) ------------------
#
# Repository.save is the SOLE enqueue site for derive jobs. After lode-npx.2 it
# enqueues BOTH embed and enrich atomically with the version write — no special
# case for enrich — so a pending enrich job exists from the instant the
# transaction commits. The CLI opportunistically claims + runs that job inline
# right after save() returns (worker.claim_and_run_one); if it loses that race
# or never runs, the job stays live for the normal worker path. These tests:
# - a successful save enqueues exactly the embed + enrich jobs;
# - a deduped save (identical body) enqueues nothing;
# - a failure during enqueue rolls back the version-write too (atomicity).


def test_save_enqueues_embed_and_enrich_jobs(conn):
    """repo.save enqueues both embed and enrich atomically (lode-npx.2)."""
    repo = Repository(conn)
    result = repo.save("note-1", "hello")
    rows = conn.execute(
        "SELECT type, target_version, status FROM jobs ORDER BY type"
    ).fetchall()
    assert rows == [
        ("embed", result.version_id, "pending"),
        ("enrich", result.version_id, "pending"),
    ]


def test_save_dedup_enqueues_nothing(conn):
    """A no-op dedup save (identical body) does not enqueue any jobs."""
    repo = Repository(conn)
    root = repo.save("note-1", "same").version_id
    # Advance the head so the dedup is against the live body.
    result = repo.save("note-1", "same", parent=root)
    assert result.deduped
    # Still only the embed + enrich jobs from the first (create) save.
    (n,) = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    assert (
        n == 2
    )  # embed + enrich from the first (create) save, none added by the dedup


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
        def _boom(c, v, **kwargs):
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


# --- re-anchor wiring on the real update path (lode-atv) -----------------------
#
# lode-npx.3 built lode.staleness (reanchor_annotations/reanchor_edges) and unit-
# tested it directly, but nothing in src/ ever called it — a real note update via
# Repository.save never re-anchored anything. These tests exercise the wiring
# through the PUBLIC save path (not staleness.py directly), proving the ticket's
# literal acceptance criteria happens in the live system: on update, an unchanged
# quote stays fresh, a changed quote goes stale, a missing quote is orphaned.


def _insert_ai_annotation(
    conn,
    *,
    target: str,
    source_version: str,
    payload_value: str = "python",
    quoted_text: str | None = None,
    status: str = "fresh",
) -> int:
    """Insert one source='ai' annotation row directly (bypassing enrichment)."""
    with conn:
        cur = conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status, quoted_text) "
            "VALUES (?, ?, 'tag', ?, 'ai', ?, ?)",
            (target, source_version, json.dumps(payload_value), status, quoted_text),
        )
        return cur.lastrowid  # type: ignore[return-value]


def _insert_ai_edge(
    conn,
    *,
    from_id: str,
    to_id: str,
    source_version: str,
    quoted_text: str | None = None,
    status: str = "fresh",
) -> int:
    """Insert one source='ai' edge row directly (bypassing enrichment)."""
    with conn:
        cur = conn.execute(
            "INSERT INTO edges "
            "(from_id, to_id, source, reason, confidence, source_version, "
            "status, quoted_text) "
            "VALUES (?, ?, 'ai', 'test reason', 0.8, ?, ?, ?)",
            (from_id, to_id, source_version, status, quoted_text),
        )
        return cur.lastrowid  # type: ignore[return-value]


def _annotation_status(conn, row_id: int) -> tuple[str, str]:
    row = conn.execute(
        "SELECT status, source_version FROM annotations WHERE id = ?", (row_id,)
    ).fetchone()
    return (row[0], row[1])


def _edge_status(conn, row_id: int) -> tuple[str, str]:
    row = conn.execute(
        "SELECT status, source_version FROM edges WHERE id = ?", (row_id,)
    ).fetchone()
    return (row[0], row[1])


def test_save_update_reanchors_annotations_fresh_stale_orphaned(conn) -> None:
    """Repository.save on update re-anchors AI annotations per the documented rules.

    Three annotations anchored at the root version: one whose quote survives
    verbatim in the new body (fresh, source_version advances), one whose quote
    is gone but the underlying concept is still mentioned (stale, source_version
    unchanged), and one whose subject is gone entirely (orphaned).
    """
    repo = Repository(conn)
    root = repo.save(
        "note-1", "python auth tutorial and rust programming notes"
    ).version_id

    fresh_id = _insert_ai_annotation(
        conn,
        target="note-1",
        source_version=root,
        payload_value="python",
        quoted_text="python auth tutorial",
    )
    stale_id = _insert_ai_annotation(
        conn,
        target="note-1",
        source_version=root,
        payload_value="rust",
        quoted_text="rust programming notes",
    )
    orphaned_id = _insert_ai_annotation(
        conn,
        target="note-1",
        source_version=root,
        payload_value="golang",
        quoted_text="golang concurrency patterns",
    )

    result = repo.save(
        "note-1",
        # "python auth tutorial" survives verbatim; "rust programming notes" is
        # gone but "rust" is still mentioned; "golang" is gone entirely.
        "python auth tutorial, now covering rust and some new java notes",
        parent=root,
    )
    assert result.op == "update"

    assert _annotation_status(conn, fresh_id) == ("fresh", result.version_id)
    assert _annotation_status(conn, stale_id) == ("stale", root)
    assert _annotation_status(conn, orphaned_id) == ("orphaned", root)


def test_save_update_reanchors_edges_fresh_stale_orphaned(conn) -> None:
    """Repository.save on update re-anchors AI edges per the same rules."""
    repo = Repository(conn)
    root = repo.save("note-1", "see jwt-topic for the full auth guide").version_id

    fresh_id = _insert_ai_edge(
        conn,
        from_id="note-1",
        to_id="jwt-topic",
        source_version=root,
        quoted_text="see jwt-topic for the full auth guide",
    )
    stale_id = _insert_ai_edge(
        conn,
        from_id="note-1",
        to_id="oauth-topic",
        source_version=root,
        quoted_text="oauth-topic is discussed at length here",
    )
    orphaned_id = _insert_ai_edge(
        conn,
        from_id="note-1",
        to_id="saml-topic",
        source_version=root,
        quoted_text="also mentions saml-topic briefly",
    )

    result = repo.save(
        "note-1",
        "see jwt-topic for the full auth guide; oauth-topic comes up too",
        parent=root,
    )
    assert result.op == "update"

    assert _edge_status(conn, fresh_id) == ("fresh", result.version_id)
    assert _edge_status(conn, stale_id) == ("stale", root)
    assert _edge_status(conn, orphaned_id) == ("orphaned", root)


def test_save_dedup_does_not_reanchor(conn) -> None:
    """A no-op dedup save (identical body) leaves AI annotations untouched."""
    repo = Repository(conn)
    root = repo.save("note-1", "same body").version_id
    ann_id = _insert_ai_annotation(
        conn,
        target="note-1",
        source_version=root,
        payload_value="whatever",
        quoted_text="same body",
    )

    result = repo.save("note-1", "same body", parent=root)
    assert result.deduped

    # Untouched: still fresh at the original source_version (root), even though
    # its quote is verbatim in the (identical) body — the dedup path never runs
    # re-anchor at all.
    assert _annotation_status(conn, ann_id) == ("fresh", root)


def test_save_create_does_not_error_with_no_prior_annotations(conn) -> None:
    """A create save has no prior AI-derived layer; re-anchor is simply skipped."""
    repo = Repository(conn)
    result = repo.save("note-1", "brand new note")
    assert result.op == "create"
    # No annotations/edges exist yet, and save must not error attempting to
    # re-anchor a nonexistent layer.
    (n,) = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()
    assert n == 0


def test_save_update_does_not_touch_user_annotations(conn) -> None:
    """source='user' annotations are never re-anchored, even through save."""
    repo = Repository(conn)
    root = repo.save("note-1", "original body").version_id
    user_id = _insert_ai_annotation(
        conn,
        target="note-1",
        source_version=root,
        payload_value="note",
        quoted_text="totally different quote",
        status="fresh",
    )
    # Flip it to source='user' after insert (helper only writes 'ai').
    with conn:
        conn.execute("UPDATE annotations SET source = 'user' WHERE id = ?", (user_id,))

    repo.save("note-1", "completely rewritten content", parent=root)

    # Untouched: still fresh at the original source_version despite the quote
    # being long gone from the new body.
    assert _annotation_status(conn, user_id) == ("fresh", root)
