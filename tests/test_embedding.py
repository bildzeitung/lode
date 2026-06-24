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
from lode.embedding import embed
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
