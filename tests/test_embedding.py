"""Tests for lode.embedding — the in-process embed leg (lode-x6r.2).

Covers the acceptance criteria: embedding a saved head version produces one
passage vector per passage in the LanceDB store, and re-embedding the same head
version is idempotent (no duplicate rows, converges to the same state). A stub
:class:`~lode.embedding.Embedder` keeps the gate fast and offline — the real
fastembed model load is the opt-in smoke test (``tests/test_models_smoke.py``).
The vector dim is overridden small so the stub's vectors are trivial to build;
the production dim is the pinned build constant.
"""

import sqlite3
from pathlib import Path

import lancedb
import pytest

from lode.config import load_settings
from lode.embedding import EmbeddingCacheBackend, FastEmbedEmbedder, embed
from lode.repository import CacheBackend, CompositeCache, Repository
from lode.storage import init_db
from lode.versions import save

# Small vector dim so the stub embedder's vectors are trivial; the real dim is
# the pinned build constant (Settings.embedding_vector_dim).
DIM = 4

# A body with several structural blocks, so it chunks into multiple passages.
BODY = "# Title\nIntro paragraph.\n\n## Section A\n- one\n- two\n\n## Section B\nmore text here.\n"


class _StubEmbedder:
    """Deterministic stand-in for the model: a per-text vector of length ``dim``.

    The first component is a stable function of the text so a re-embed of the
    same passages yields identical vectors (lets idempotency assert on values,
    not just row counts) while distinct passages get distinct vectors.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(t) % 97)] + [0.0] * (self.dim - 1) for t in texts]


def _settings():
    return load_settings(embedding_vector_dim=DIM)


def _save_note(conn: sqlite3.Connection, body: str = BODY) -> str:
    """Save a fresh note and return its head version_id."""
    settings = _settings()
    return save(conn, "note-1", body, settings=settings).version_id


def _open_vector_table(lance_dir: Path):
    return lancedb.connect(lance_dir).open_table("embeddings")


def test_embed_writes_one_vector_per_passage(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        version = _save_note(conn)
        lance_dir = tmp_path / "vectors"

        n = embed(
            conn,
            version,
            lance_dir=lance_dir,
            embedder=_StubEmbedder(DIM),
            settings=_settings(),
        )

        # One passage row in SQLite per chunked passage, and one vector each.
        (passage_count,) = conn.execute(
            "SELECT COUNT(*) FROM passages WHERE target_version = ?", (version,)
        ).fetchone()
        assert n == passage_count > 1

        rows = _open_vector_table(lance_dir).to_arrow().to_pylist()
        assert len(rows) == n
        assert {r["target_version"] for r in rows} == {version}
        assert all(len(r["vector"]) == DIM for r in rows)
        assert all(r["model"] == _settings().embedding_model for r in rows)
        # Vector rows are keyed to the persisted passages.
        sqlite_ids = {
            pid
            for (pid,) in conn.execute(
                "SELECT passage_id FROM passages WHERE target_version = ?", (version,)
            )
        }
        assert {r["passage_id"] for r in rows} == sqlite_ids
    finally:
        conn.close()


def test_reembed_same_head_is_idempotent(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        version = _save_note(conn)
        lance_dir = tmp_path / "vectors"
        settings = _settings()

        first = embed(
            conn,
            version,
            lance_dir=lance_dir,
            embedder=_StubEmbedder(DIM),
            settings=settings,
        )
        before = _open_vector_table(lance_dir).to_arrow().to_pylist()

        second = embed(
            conn,
            version,
            lance_dir=lance_dir,
            embedder=_StubEmbedder(DIM),
            settings=settings,
        )
        after = _open_vector_table(lance_dir).to_arrow().to_pylist()

        # Same passage count, no duplicate vector rows, same passages persisted.
        assert first == second
        assert len(after) == len(before)
        (passage_count,) = conn.execute(
            "SELECT COUNT(*) FROM passages WHERE target_version = ?", (version,)
        ).fetchone()
        assert passage_count == first
        # Deterministic stub → identical vectors per passage on the re-embed.
        key = lambda rows: {r["passage_id"]: r["vector"] for r in rows}  # noqa: E731
        assert key(after) == key(before)
    finally:
        conn.close()


def test_unknown_version_raises_keyerror(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        with pytest.raises(KeyError):
            embed(
                conn,
                "nope",
                lance_dir=tmp_path / "vectors",
                embedder=_StubEmbedder(DIM),
                settings=_settings(),
            )
    finally:
        conn.close()


# --- polymorphic body resolution (lode-c5l) --------------------------------
#
# _version_body must resolve a note version_id OR an external snapshot_id, so
# an embed job enqueued for a snapshot (lode.externals.ingest_snapshot) runs
# to completion instead of raising KeyError.


def test_embed_resolves_a_snapshot_id_body_no_keyerror(tmp_path: Path) -> None:
    """Acceptance: an embed job enqueued for a snapshot_id runs to completion."""
    from lode.externals import ingest_snapshot

    conn = init_db(tmp_path / "lode.db")
    try:
        result = ingest_snapshot(conn, "https://example.com/x", "web", BODY)
        lance_dir = tmp_path / "vectors"

        n = embed(
            conn,
            result.snapshot_id,
            lance_dir=lance_dir,
            embedder=_StubEmbedder(DIM),
            settings=_settings(),
        )

        (passage_count,) = conn.execute(
            "SELECT COUNT(*) FROM passages WHERE target_version = ?",
            (result.snapshot_id,),
        ).fetchone()
        assert n == passage_count > 1
        rows = _open_vector_table(lance_dir).to_arrow().to_pylist()
        assert {r["target_version"] for r in rows} == {result.snapshot_id}
    finally:
        conn.close()


def test_embed_redacts_a_secret_in_a_snapshot_body_too(tmp_path: Path) -> None:
    """redact-before-index applies identically whether the body came from
    ``versions`` or ``snapshots`` (lode-c5l) — the vector leg's redaction is
    driven by :func:`lode.embedding._version_body`'s resolution, not by which
    table it happened to read from.
    """
    from lode.externals import ingest_snapshot

    conn = init_db(tmp_path / "lode.db")
    try:
        secret = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern
        body = f"mirrored page contents\ncreds: {secret} keep private\n"
        result = ingest_snapshot(conn, "https://example.com/y", "web", body)
        lance_dir = tmp_path / "vectors"
        stub = _StubEmbedder(DIM)

        embed(
            conn,
            result.snapshot_id,
            lance_dir=lance_dir,
            embedder=stub,
            settings=_settings(),
        )

        assert not any(secret in text for texts in stub.calls for text in texts)
        rows = conn.execute(
            "SELECT text FROM passages WHERE target_version = ?",
            (result.snapshot_id,),
        ).fetchall()
        assert rows, "sanity: the body chunked to at least one passage"
        assert not any(secret in text for (text,) in rows)
        # snapshots.body (the irreplaceable mirrored copy) still carries the
        # raw secret — only the text handed to chunk() is redacted.
        (stored_body,) = conn.execute(
            "SELECT body FROM snapshots WHERE snapshot_id = ?", (result.snapshot_id,)
        ).fetchone()
        assert secret in stored_body
    finally:
        conn.close()


def test_empty_body_embeds_nothing(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        # A whitespace-only body chunks to zero passages.
        version = _save_note(conn, body="   \n\n  \n")
        stub = _StubEmbedder(DIM)
        n = embed(
            conn,
            version,
            lance_dir=tmp_path / "vectors",
            embedder=stub,
            settings=_settings(),
        )
        assert n == 0
        # Nothing to embed → the model is never invoked.
        assert stub.calls == []
    finally:
        conn.close()


# --- FastEmbedEmbedder.embed_query: the asymmetric query side (lode-bkc) --------
#
# The query path applies the ``search_query:`` prefix (vs ``search_document:`` for
# indexed passages) and returns a single vector. Verified offline by stubbing the
# model so the gate never downloads it; the real model load is the smoke test.


def test_embed_query_applies_search_query_prefix() -> None:
    captured: dict[str, list[str]] = {}

    class _FakeVector:
        def tolist(self) -> list[float]:
            return [0.1, 0.2, 0.3]

    class _FakeModel:
        def embed(self, texts: list[str]) -> list[_FakeVector]:
            captured["texts"] = list(texts)
            return [_FakeVector()]

    embedder = FastEmbedEmbedder(_settings())
    # Bypass the real (downloaded) model with the offline fake.
    embedder._load = lambda: _FakeModel()  # type: ignore[method-assign]

    vector = embedder.embed_query("how do I rotate the certs?")

    # The query is prefixed for the asymmetric query side, embedded as a single
    # item, and returned as one plain-float vector.
    assert captured["texts"] == ["search_query: how do I rotate the certs?"]
    assert vector == [0.1, 0.2, 0.3]


# --- FastEmbedEmbedder._load: cache_dir under $LODE_HOME, never /tmp (lode-gmo) -
#
# Without an explicit cache_dir, fastembed falls back to
# tempfile.gettempdir()/fastembed_cache -- wiped on reboot by WSL/systemd-tmpfiles
# -- so weights would be silently re-downloaded on a semi-regular basis. Verified
# by patching the fastembed.TextEmbedding constructor itself (never called for
# real), so this stays offline like every other test here.


def test_load_passes_durable_model_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fastembed

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))
    captured: dict[str, object] = {}

    class _FakeTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)

    embedder = FastEmbedEmbedder(_settings())
    embedder._load()

    assert captured["cache_dir"] == str(tmp_path / "root" / "models")


def test_load_logs_progress_around_the_model_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first-use ONNX cold load is a named op_progress step (lode-olmi.15):
    a stuck 'lode work' inside a first embed job should show 'embedding.
    load_model' as the step in progress rather than staying silent.
    """
    import logging

    import fastembed

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))

    class _FakeTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)

    embedder = FastEmbedEmbedder(_settings())
    with caplog.at_level(logging.INFO):
        embedder._load()

    assert "embedding.load_model: starting" in caplog.text
    assert "embedding.load_model: done" in caplog.text


# --- EmbeddingCacheBackend: vectors reached THROUGH the Repository (lode-1f9) ---
#
# The embed leg wrapped as a CacheBackend, so a save on the Repository fills the
# vector cache without the caller ever touching lode.embedding / VectorStore.


def test_embedding_backend_satisfies_the_cache_protocol():
    """The vector engine plugs into the same seam the composite fans out to."""
    backend = EmbeddingCacheBackend(None, lance_dir="unused")  # type: ignore[arg-type]
    assert isinstance(backend, CacheBackend)


def test_repository_save_fills_the_vector_cache_through_the_backend(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        lance_dir = tmp_path / "vectors"
        settings = _settings()
        backend = EmbeddingCacheBackend(
            conn, lance_dir=lance_dir, embedder=_StubEmbedder(DIM), settings=settings
        )
        repo = Repository(conn, CompositeCache([backend]))

        # The caller only touches the Repository — never embed() / VectorStore.
        result = repo.save("note-1", BODY, settings=settings)

        rows = _open_vector_table(lance_dir).to_arrow().to_pylist()
        assert len(rows) > 1
        assert {r["target_version"] for r in rows} == {result.version_id}
    finally:
        conn.close()


def test_repository_dedup_save_does_not_re_embed(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        lance_dir = tmp_path / "vectors"
        settings = _settings()
        stub = _StubEmbedder(DIM)
        repo = Repository(
            conn,
            CompositeCache(
                [
                    EmbeddingCacheBackend(
                        conn, lance_dir=lance_dir, embedder=stub, settings=settings
                    )
                ]
            ),
        )

        root = repo.save("note-1", BODY, settings=settings).version_id
        calls_after_create = len(stub.calls)
        repo.save("note-1", BODY, parent=root, settings=settings)  # no-op dedup

        # The deduped save changed no body, so the cache seam never fires.
        assert len(stub.calls) == calls_after_create
    finally:
        conn.close()


# --- redact-before-index (lode-n60): the vector leg is async, driven off ------
# target_version alone (no body travels through the enqueue), so embed() must
# independently redact what it reads from versions.body rather than relying on
# a caller having already redacted it.


def test_embed_redacts_seeded_secret_before_chunk_and_embed(tmp_path: Path) -> None:
    """A pasted secret is stripped before it reaches the embedder or LanceDB.

    Regression for lode-n60: before this fix, embed() read versions.body raw
    and chunked/embedded it unredacted — a pasted secret was locally
    retrievable via vector search. Fixed by applying redact_before_index()
    right after the versions.body read, before chunk().
    """
    conn = init_db(tmp_path / "lode.db")
    try:
        secret = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern
        body = f"# Notes\ncreds: {secret} keep private\n\nOther prose stays intact.\n"
        version = _save_note(conn, body=body)
        lance_dir = tmp_path / "vectors"
        stub = _StubEmbedder(DIM)

        embed(conn, version, lance_dir=lance_dir, embedder=stub, settings=_settings())

        # The embedder never sees the raw secret text.
        assert not any(secret in text for texts in stub.calls for text in texts)
        # Nor does the passages table the vector rows (and the lexical leg's
        # context-expansion) are keyed off.
        rows = conn.execute(
            "SELECT text FROM passages WHERE target_version = ?", (version,)
        ).fetchall()
        assert rows, "sanity: the body chunked to at least one passage"
        assert not any(secret in text for (text,) in rows)
        # versions.body (the irreplaceable store) still carries the raw secret
        # — only `purge` clears that durable copy (docs/externals.md).
        (stored_body,) = conn.execute(
            "SELECT body FROM versions WHERE version_id = ?", (version,)
        ).fetchone()
        assert secret in stored_body
    finally:
        conn.close()


def test_embedding_backend_evict_drops_the_versions_vectors(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        lance_dir = tmp_path / "vectors"
        settings = _settings()
        backend = EmbeddingCacheBackend(
            conn, lance_dir=lance_dir, embedder=_StubEmbedder(DIM), settings=settings
        )
        version = _save_note(conn)

        backend.index("note-1", version, BODY)
        assert _open_vector_table(lance_dir).count_rows() > 0

        backend.evict("note-1", version)
        assert _open_vector_table(lance_dir).count_rows() == 0
    finally:
        conn.close()
