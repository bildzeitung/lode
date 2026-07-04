"""Tests for lode.tui.related — passive connection surfacing (lode-mkc.3).

Pins the ticket's acceptance criterion directly: while writing, related past
notes surface from the **existing** retrieval/graph layer (``lode.retrieval``)
— not a reimplementation. The corpus is built through ``Repository`` +
``CompositeCache`` (same seam ``lode add`` / the capture screen use) so the
lexical (FTS5) and dense (LanceDB) legs index exactly what production would;
a tiny deterministic bag-of-words stub stands in for the embedder so the
gate never downloads a model (mirrors ``tests/test_retrieval.py``).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lode.config import lance_dir, load_settings
from lode.embedding import EmbeddingCacheBackend
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.related import find_related_notes, humanize_age

# A vocabulary mapped 1:1 onto the (overridden, tiny) vector dimension — same
# convention as tests/test_retrieval.py's _BagEmbedder.
_VOCAB = ("alpha", "bravo", "charlie", "delta", "echo")
DIM = len(_VOCAB)


class _BagEmbedder:
    """Deterministic offline stub: a text embeds to its vocab word counts.

    Implements both :class:`~lode.embedding.Embedder` methods (unlike
    ``test_retrieval.py``'s read-only ``_BagEmbedder``) since
    ``find_related_notes`` calls ``embed_query`` itself, not just the
    passage-indexing side.
    """

    def _vector(self, text: str) -> list[float]:
        return [float(text.lower().split().count(word)) for word in _VOCAB]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def settings():
    return load_settings(embedding_vector_dim=DIM, related_notes_min_chars=0)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "lode.db"


@pytest.fixture
def repo(db_path, settings) -> Repository:
    """A repository whose saves drive both index legs, as production does."""
    conn = init_db(db_path)
    return Repository(
        conn,
        CompositeCache(
            [
                LexicalCacheBackend(conn, settings=settings),
                EmbeddingCacheBackend(
                    conn,
                    lance_dir=lance_dir(db_path),
                    embedder=_BagEmbedder(),
                    settings=settings,
                ),
            ]
        ),
    )


@pytest.fixture
def lexical_only_repo(db_path, settings) -> Repository:
    """A repository indexed by the lexical leg only — no vectors ever land.

    Mirrors ``lode.tui.capture.save_capture``'s cache composition (embed
    stays async/pending), so a test built on this fixture proves related
    notes surface via the lexical leg alone, before any embedding runs.
    """
    conn = init_db(db_path)
    return Repository(
        conn, CompositeCache([LexicalCacheBackend(conn, settings=settings)])
    )


# --- the enabled gate: cheap, no DB touched, defaults on -------------------


def test_disabled_returns_empty_without_opening_the_db(tmp_path: Path) -> None:
    db_path = tmp_path / "nonexistent" / "lode.db"
    settings = load_settings(related_notes_enabled=False, related_notes_min_chars=0)

    result = find_related_notes(db_path, "alpha bravo charlie", settings=settings)

    assert result == []
    assert not db_path.exists()  # init_db never called


def test_enabled_defaults_to_true() -> None:
    assert load_settings().related_notes_enabled is True


# --- the min-chars gate: cheap, no DB touched -----------------------------


def test_short_draft_returns_empty_without_opening_the_db(tmp_path: Path) -> None:
    db_path = tmp_path / "nonexistent" / "lode.db"
    settings = load_settings(related_notes_min_chars=20)

    result = find_related_notes(db_path, "too short", settings=settings)

    assert result == []
    assert not db_path.exists()  # init_db never called


def test_whitespace_only_draft_is_treated_as_too_short(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    settings = load_settings(related_notes_min_chars=5)

    assert find_related_notes(db_path, "     \n\t  ", settings=settings) == []


# --- lexical leg alone (before any embedding runs) ------------------------


def test_surfaces_a_note_via_the_lexical_leg_before_embedding_runs(
    lexical_only_repo, db_path, settings
) -> None:
    lexical_only_repo.save("note-a", "alpha bravo staging certificate rotation")
    lexical_only_repo.conn.close()

    related = find_related_notes(
        db_path, "alpha bravo runbook draft", settings=settings, embedder=_BagEmbedder()
    )

    assert [r.note_id for r in related] == ["note-a"]
    assert "certificate rotation" in related[0].snippet or "alpha" in related[0].snippet


def test_unrelated_draft_surfaces_nothing(lexical_only_repo, db_path, settings) -> None:
    lexical_only_repo.save("note-a", "alpha bravo charlie")
    lexical_only_repo.conn.close()

    related = find_related_notes(
        db_path,
        "zzz completely unrelated wording",
        settings=settings,
        embedder=_BagEmbedder(),
    )

    assert related == []


# --- the dense leg (GraphRAG's direct-hit sibling) -------------------------


def test_surfaces_a_note_via_the_dense_leg_when_lexically_disjoint(
    repo, db_path, settings
) -> None:
    """A lexically-disjoint draft still surfaces via the dense leg (RRF fuses both)."""
    repo.save("note-a", "alpha alpha alpha")
    repo.conn.close()

    # Shares no word tokens with "alpha alpha alpha", but embeds to the same
    # direction under the bag-of-words stub, so only the dense leg can find it.
    related = find_related_notes(
        db_path, "alpha alpha alpha alpha", settings=settings, embedder=_BagEmbedder()
    )

    assert [r.note_id for r in related] == ["note-a"]


# --- dedup: one entry per distinct note, even with multiple matching passages --


def test_dedupes_multiple_matching_passages_of_the_same_note(
    lexical_only_repo, db_path, settings
) -> None:
    # Two structurally distinct paragraphs -> two passages, both mentioning
    # "alpha" -> both match lexically, but it is one note.
    body = "alpha bravo section one.\n\nalpha charlie section two."
    lexical_only_repo.save("note-a", body)
    lexical_only_repo.conn.close()

    related = find_related_notes(
        db_path, "alpha", settings=settings, embedder=_BagEmbedder()
    )

    assert [r.note_id for r in related] == ["note-a"]


# --- the result-count cap (Settings.related_notes_limit) -------------------


def test_caps_results_at_the_configured_limit(
    lexical_only_repo, db_path, settings
) -> None:
    for i in range(4):
        lexical_only_repo.save(f"note-{i}", f"alpha unique-{i} filler text")
    lexical_only_repo.conn.close()

    capped_settings = load_settings(
        embedding_vector_dim=DIM, related_notes_min_chars=0, related_notes_limit=2
    )
    related = find_related_notes(
        db_path, "alpha", settings=capped_settings, embedder=_BagEmbedder()
    )

    assert len(related) == 2


# --- the graph layer: a note reached only via an edge still surfaces -------


def test_surfaces_a_note_reached_only_through_a_graph_edge(
    lexical_only_repo, db_path, settings
) -> None:
    """The acceptance criterion's "graph layer" half: graph_expand is reused, not skipped."""
    seed = lexical_only_repo.save("note-seed", "alpha bravo").note_id
    linked = lexical_only_repo.save(
        "note-linked", "totally unrelated body text"
    ).note_id
    conn = lexical_only_repo.conn
    conn.execute(
        "INSERT INTO edges (from_id, to_id, source, status) VALUES (?, ?, ?, ?)",
        (seed, linked, "user", "fresh"),
    )
    conn.commit()
    conn.close()

    related = find_related_notes(
        db_path, "alpha bravo", settings=settings, embedder=_BagEmbedder()
    )

    note_ids = {r.note_id for r in related}
    assert {"note-seed", "note-linked"} <= note_ids


# --- humanize_age -----------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=5), "5 minutes ago"),
        (timedelta(hours=2), "2 hours ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=21), "3 weeks ago"),
        (timedelta(days=60), "2 months ago"),
        (timedelta(days=400), "1 year ago"),
    ],
)
def test_humanize_age_buckets(delta: timedelta, expected: str) -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    created = _iso(now - delta)

    assert humanize_age(created, now=now) == expected


def test_humanize_age_never_negative_for_a_clock_skewed_future_timestamp() -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    created = _iso(now + timedelta(minutes=5))  # created "after" now

    assert humanize_age(created, now=now) == "just now"
