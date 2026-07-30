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
from types import SimpleNamespace

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
        # _StubEmbedder has no model_revision() -- the duck-typed probe finds
        # nothing to call, so every row carries NULL (lode-g274.4).
        assert all(r["model_revision"] is None for r in rows)
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
        key = lambda rows: {r["passage_id"]: r["vector"] for r in rows}
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


# --- model_revision: per-vector provenance (lode-g274.4, lode-crh8.1's ---------
# DETECT-not-PIN / WARN-per-vector decision, docs/storage.md §8a)


class _StubEmbedderWithRevision(_StubEmbedder):
    """A stub that DOES report a resolved revision, unlike the plain stub above.

    Exercises the duck-typed :func:`lode.embedding._embedder_model_revision`
    probe's positive path -- most other tests in this file use the plain
    `_StubEmbedder`, which has no `model_revision()` at all (the negative path,
    covered by `test_embed_writes_one_vector_per_passage` above).
    """

    def __init__(self, dim: int, revision: str) -> None:
        super().__init__(dim)
        self.revision = revision
        self.revision_calls = 0

    def model_revision(self) -> str:
        self.revision_calls += 1
        return self.revision


def test_embed_records_the_embedder_reported_revision_per_row(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        version = _save_note(conn)
        lance_dir = tmp_path / "vectors"
        stub = _StubEmbedderWithRevision(DIM, "sha-abc123")

        embed(conn, version, lance_dir=lance_dir, embedder=stub, settings=_settings())

        rows = _open_vector_table(lance_dir).to_arrow().to_pylist()
        assert rows
        assert all(r["model_revision"] == "sha-abc123" for r in rows)
    finally:
        conn.close()


def test_embed_skips_the_revision_probe_when_nothing_to_embed(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "lode.db")
    try:
        # A whitespace-only body chunks to zero passages -- same edge as
        # test_empty_body_embeds_nothing, but asserting the revision probe
        # specifically is never reached either (nothing will record it).
        version = _save_note(conn, body="   \n\n  \n")
        stub = _StubEmbedderWithRevision(DIM, "sha-abc123")

        n = embed(
            conn,
            version,
            lance_dir=tmp_path / "vectors",
            embedder=stub,
            settings=_settings(),
        )

        assert n == 0
        assert stub.revision_calls == 0
    finally:
        conn.close()


def test_embedder_model_revision_probe_never_raises_on_a_broken_method(
    tmp_path: Path,
) -> None:
    """A revision probe that raises must not take the embed down (WARN, never fail)."""
    conn = init_db(tmp_path / "lode.db")
    try:

        class _BoomEmbedder(_StubEmbedder):
            def model_revision(self) -> str:
                raise RuntimeError("network's out")

        version = _save_note(conn)
        lance_dir = tmp_path / "vectors"

        n = embed(
            conn,
            version,
            lance_dir=lance_dir,
            embedder=_BoomEmbedder(DIM),
            settings=_settings(),
        )

        assert n > 0
        rows = _open_vector_table(lance_dir).to_arrow().to_pylist()
        assert all(r["model_revision"] is None for r in rows)
    finally:
        conn.close()


def test_resolve_model_revision_returns_the_probed_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import huggingface_hub

    from lode.embedding import resolve_model_revision

    class _FakeModelInfo:
        sha = "deadbeef123"

    captured: dict[str, str] = {}

    def _fake_model_info(repo_id: str) -> _FakeModelInfo:
        captured["repo_id"] = repo_id
        return _FakeModelInfo()

    monkeypatch.setattr(huggingface_hub, "model_info", _fake_model_info)

    model_id = _settings().embedding_model
    assert resolve_model_revision(model_id) == "deadbeef123"
    # Probed via the pinned HF source repo id, not the friendly model id --
    # they can differ for some models (lode.config.model_cache_identity).
    from lode.config import model_cache_identity

    hf_source, _model_file = model_cache_identity(model_id)  # type: ignore[misc]
    assert captured["repo_id"] == hf_source


def test_resolve_model_revision_unknown_model_returns_none_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import huggingface_hub

    from lode.embedding import resolve_model_revision

    def _boom(repo_id: str) -> None:
        raise AssertionError("must not be reached for an unpinned model id")

    monkeypatch.setattr(huggingface_hub, "model_info", _boom)

    assert resolve_model_revision("not-a-real/model-id") is None


def test_resolve_model_revision_never_raises_on_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import huggingface_hub

    from lode.embedding import resolve_model_revision

    def _offline(repo_id: str) -> None:
        raise OSError("no network")

    monkeypatch.setattr(huggingface_hub, "model_info", _offline)

    assert resolve_model_revision(_settings().embedding_model) is None


def test_resolve_model_revision_short_circuits_under_hf_hub_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lode-r4r2: HF_HUB_OFFLINE=1 must skip the network call entirely, not
    just eventually fail after it -- huggingface_hub.model_info does not
    honor the flag itself (see resolve_model_revision's docstring), so
    without this short-circuit a real no-network run would block on the OS
    TCP timeout instead of returning None immediately.
    """
    import huggingface_hub

    from lode.embedding import resolve_model_revision

    def _boom(repo_id: str) -> None:
        raise AssertionError("must not be reached when HF_HUB_OFFLINE is set")

    monkeypatch.setattr(huggingface_hub, "model_info", _boom)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    # A model id that DOES resolve a pinned identity -- proves the
    # short-circuit is the flag, not model_cache_identity() already
    # returning None for an unpinned id (that's the sibling test above).
    assert resolve_model_revision(_settings().embedding_model) is None


def test_resolve_model_revision_probes_normally_when_hf_hub_offline_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offline short-circuit must not swallow the ordinary online path."""
    import huggingface_hub

    from lode.embedding import resolve_model_revision

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

    class _FakeModelInfo:
        sha = "online-sha"

    monkeypatch.setattr(huggingface_hub, "model_info", lambda repo_id: _FakeModelInfo())

    assert resolve_model_revision(_settings().embedding_model) == "online-sha"


def test_fast_embed_embedder_model_revision_resolves_and_caches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FastEmbedEmbedder.model_revision() loads the model, probes once, caches."""
    import fastembed
    import huggingface_hub

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))

    class _FakeTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)

    probe_calls = 0

    class _FakeModelInfo:
        sha = "resolved-sha"

    def _fake_model_info(repo_id: str) -> _FakeModelInfo:
        nonlocal probe_calls
        probe_calls += 1
        return _FakeModelInfo()

    monkeypatch.setattr(huggingface_hub, "model_info", _fake_model_info)

    embedder = FastEmbedEmbedder(_settings())
    assert embedder.model_revision() == "resolved-sha"
    assert embedder.model_revision() == "resolved-sha"
    # Resolved once at load time, cached for this instance's lifetime -- not
    # re-probed on every model_revision() call.
    assert probe_calls == 1


def test_fast_embed_embedder_model_revision_none_on_probe_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fastembed
    import huggingface_hub

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))

    class _FakeTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)

    def _offline(repo_id: str) -> None:
        raise OSError("no network")

    monkeypatch.setattr(huggingface_hub, "model_info", _offline)

    embedder = FastEmbedEmbedder(_settings())
    assert embedder.model_revision() is None


def test_embed_query_never_probes_the_revision_even_with_a_warm_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A query-only embed makes no HF revision probe at all (lode-dj6m).

    Before this fix, ``FastEmbedEmbedder._load`` resolved the HF revision
    unconditionally, so every ``embed_query`` (the passive related-notes panel,
    ``ask``/``retrieve``) paid a live, untimed HTTPS round trip whose result
    nothing on that path reads -- regardless of whether the ONNX weights were
    already cached on disk.

    ``huggingface_hub.model_info`` is stubbed to COUNT its calls, and the count
    is asserted to be zero. Deliberately not a stub that *raises*: everything
    reached through ``resolve_model_revision`` runs inside its documented
    ``except Exception: return None``, which swallows a raised ``AssertionError``
    just as readily as a real network error -- so a raising stub would leave the
    probe silently returning ``None`` and ``embed_query`` returning its vector,
    i.e. green on exactly the regression this test exists to catch. Verified by
    reintroducing the probe into ``_load`` and watching a raising version of
    this test still pass. Sabotage recipe for re-proving it non-vacuous: add
    ``self._revision = resolve_model_revision(self._model_name)`` back inside
    ``_load``'s critical section -- the ``probe_calls == 0`` assertion below
    must fail.
    """
    import fastembed
    import huggingface_hub

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))

    class _FakeVector:
        def tolist(self) -> list[float]:
            return [0.1, 0.2, 0.3]

    class _FakeTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            pass

        def embed(self, texts: list[str]) -> list[_FakeVector]:
            return [_FakeVector() for _ in texts]

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)

    probe_calls = 0

    class _FakeModelInfo:
        sha = "must-not-be-probed"

    def _counting_model_info(repo_id: str) -> _FakeModelInfo:
        nonlocal probe_calls
        probe_calls += 1
        return _FakeModelInfo()

    monkeypatch.setattr(huggingface_hub, "model_info", _counting_model_info)

    embedder = FastEmbedEmbedder(_settings())
    assert embedder.embed_query("anything") == [0.1, 0.2, 0.3]
    # Twice, to rule out a "resolved lazily on the first call only" reading --
    # embed_query must stay off this probe for the instance's whole lifetime.
    assert embedder.embed_query("anything again") == [0.1, 0.2, 0.3]
    assert probe_calls == 0


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
    import huggingface_hub

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))
    captured: dict[str, object] = {}

    class _FakeTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)
    # _load() also probes the revision (lode-g274.4) -- stub it offline so this
    # stays a hermetic, network-free test like every other one in this section.
    monkeypatch.setattr(
        huggingface_hub, "model_info", lambda repo_id: SimpleNamespace(sha="unused")
    )

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
    import huggingface_hub

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))

    class _FakeTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)
    # Same offline stub as the sibling test above -- _load() now also probes
    # the revision (lode-g274.4).
    monkeypatch.setattr(
        huggingface_hub, "model_info", lambda repo_id: SimpleNamespace(sha="unused")
    )

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
