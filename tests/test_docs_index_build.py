"""Tests for scripts/docs_index_build.py -- the docs/ lookup index FTS5 build
step (lode-t6o1.2).

Covers the acceptance criteria: builds into a cache dir that resolves OUTSIDE
this repo's worktree, rebuilds from scratch on every call (no cache reuse),
uses stdlib sqlite3 FTS5 only, and never imports lode's own embedding/FTS
retrieval pipeline.
"""

import sqlite3
import time
from pathlib import Path

import pytest
from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't an installed package, so load by file path via the shared
# helper (tests/conftest.py) -- same pattern as tests/test_docs_index_chunker.py.
# Registered under a name private to THIS test module; docs_index_build.py
# itself loads the chunker under ITS OWN private sys.modules name (see that
# module's _load_chunker docstring), so this and
# tests/test_docs_index_chunker.py's separate load of "docs_index_chunker"
# never collide regardless of pytest's collection order.
_build = load_module_from_path(
    "docs_index_build", REPO_ROOT / "scripts" / "docs_index_build.py"
)
build_index = _build.build_index
cache_db_path = _build.cache_db_path
DEFAULT_DOCS_DIR = _build.DEFAULT_DOCS_DIR


def test_cache_db_path_resolves_outside_the_worktree() -> None:
    """Structural half of the never-tracked constraint (the gate-test half
    is lode-t6o1.4): the default build target can never land inside this
    checkout."""
    path = cache_db_path()
    assert not path.is_relative_to(REPO_ROOT)


def test_relative_xdg_cache_home_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative XDG_CACHE_HOME resolves against the CWD, so honouring one
    inside the checkout would place the index in the worktree and defeat the
    structural half of the never-tracked constraint. Spec-correct too: XDG
    says a non-absolute value must be ignored."""
    monkeypatch.setenv("XDG_CACHE_HOME", "relative-cache")
    assert cache_db_path() == Path.home() / ".cache" / "lode" / "docs-index.sqlite3"

    monkeypatch.setenv("XDG_CACHE_HOME", "")
    assert cache_db_path() == Path.home() / ".cache" / "lode" / "docs-index.sqlite3"

    monkeypatch.setenv("XDG_CACHE_HOME", "/abs/cache")
    assert cache_db_path() == Path("/abs/cache/lode/docs-index.sqlite3")


def test_build_index_queryable_and_covers_real_corpus(tmp_path: Path) -> None:
    db_path = tmp_path / "docs-index.sqlite3"
    conn = build_index(DEFAULT_DOCS_DIR, db_path=db_path)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM units").fetchone()
        assert count > 0

        rows = conn.execute(
            "SELECT path, line_lo, line_hi, first_line, doc_class "
            "FROM units WHERE units MATCH 'invariant' LIMIT 5"
        ).fetchall()
        assert rows, "expected at least one FTS5 hit for a real docs/ term"
        for _path, line_lo, line_hi, first_line, doc_class in rows:
            assert line_lo <= line_hi
            assert first_line
            assert doc_class in ("decision-record", "reference/process")
    finally:
        conn.close()


def test_build_index_rebuilds_from_scratch_every_call(tmp_path: Path) -> None:
    """No cache: a stale row present in an existing db file at the target path
    must not survive a rebuild."""
    db_path = tmp_path / "docs-index.sqlite3"

    stale = sqlite3.connect(db_path)
    stale.execute("CREATE VIRTUAL TABLE units USING fts5(path)")
    stale.execute("INSERT INTO units (path) VALUES ('stale-row-should-not-survive')")
    stale.commit()
    stale.close()

    conn = build_index(DEFAULT_DOCS_DIR, db_path=db_path)
    try:
        hit = conn.execute(
            "SELECT 1 FROM units WHERE path = 'stale-row-should-not-survive'"
        ).fetchone()
        assert hit is None
    finally:
        conn.close()


def test_build_index_measured_time_is_fast(tmp_path: Path) -> None:
    """Freshness call from docs/decisions.md (lode-t6o1): no cache is correct
    because a full rebuild is cheap. Measured 83-118 ms on an unloaded dev box.

    The ceiling here is deliberately NOT the ~500 ms design revisit trigger.
    A bare wall-clock assertion is load-dependent by construction, and this
    suite runs under `pytest -n 8`: the identical `elapsed < 0.5` shape in
    tests/test_sha_fabrication_guard.py measured 0.749s under parallel load
    and flaked, passing 3/3 in isolation (lode-vaxe) -- so 0.5 s against a
    ~95 ms operation is a known-flaky margin, not a safe one. What is pinned
    instead is a scheduler-tolerant backstop (~20x the unloaded measurement)
    that still catches an order-of-magnitude regression. The ~500 ms design
    trigger stays a MEASURED figure recorded on the ticket and in
    build_index's docstring, per the acceptance criterion; it is not
    something a parallel test suite can honestly assert."""
    db_path = tmp_path / "docs-index.sqlite3"
    start = time.monotonic()
    conn = build_index(DEFAULT_DOCS_DIR, db_path=db_path)
    elapsed = time.monotonic() - start
    conn.close()
    assert elapsed < 2.0, (
        f"docs/ index build took {elapsed:.3f}s -- far past even generous "
        "scheduler noise; the no-cache freshness call assumes a cheap rebuild"
    )


def test_no_import_of_lodes_own_retrieval_pipeline() -> None:
    """Independence constraint (epic lode-t6o1, SETTLED item 4): the build
    step must not import lode's own embedding/FTS retrieval code."""
    source = (REPO_ROOT / "scripts" / "docs_index_build.py").read_text(encoding="utf-8")
    for forbidden in ("lode.retrieval", "lode.embedding", "lode.vectorstore"):
        assert forbidden not in source
