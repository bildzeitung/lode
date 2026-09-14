"""Tests for scripts/docs_index_log.py -- the docs-index invocation log
(lode-dozi).

Covers the acceptance criteria: one line appended per invocation, the log
path honors XDG_CACHE_HOME the same way the index db does, and --stats-style
summarization (invocations, miss rate, top zero-hit queries) works against a
sample log.
"""

import json
from pathlib import Path

import pytest
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent

_log_module = load_module_from_path(
    "docs_index_log", REPO_ROOT / "scripts" / "docs_index_log.py"
)
log_path = _log_module.log_path
append_invocation = _log_module.append_invocation
read_log = _log_module.read_log
compute_stats = _log_module.compute_stats
app = _log_module.app

runner = CliRunner()


def test_log_path_honors_xdg_cache_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert log_path() == tmp_path / "lode" / "docs-index-log.jsonl"


def test_log_path_falls_back_to_home_cache_when_xdg_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert log_path() == Path.home() / ".cache" / "lode" / "docs-index-log.jsonl"


def test_log_path_ignores_a_relative_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same rationale as docs_index_build.cache_db_path(): a relative
    # XDG_CACHE_HOME resolves against the current dir, which could place the
    # log inside a repo worktree -- ignored, per the XDG spec.
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache/dir")
    assert log_path() == Path.home() / ".cache" / "lode" / "docs-index-log.jsonl"


def test_append_invocation_writes_exactly_one_line(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    append_invocation(
        query_text="lode-dozi",
        hit_count=3,
        fallback_fired=False,
        elapsed_ms=12.5,
        cwd="/some/cwd",
        path=target,
    )
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["query"] == "lode-dozi"
    assert entry["hit_count"] == 3
    assert entry["fallback_fired"] is False
    assert entry["elapsed_ms"] == 12.5
    assert entry["cwd"] == "/some/cwd"
    assert "timestamp" in entry


def test_append_invocation_appends_a_second_line_without_clobbering(
    tmp_path: Path,
) -> None:
    target = tmp_path / "log.jsonl"
    append_invocation(
        query_text="one", hit_count=1, fallback_fired=False, elapsed_ms=1.0, path=target
    )
    append_invocation(
        query_text="two", hit_count=0, fallback_fired=False, elapsed_ms=2.0, path=target
    )
    entries = read_log(target)
    assert [e["query"] for e in entries] == ["one", "two"]


def test_append_invocation_creates_the_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "log.jsonl"
    append_invocation(
        query_text="x", hit_count=1, fallback_fired=False, elapsed_ms=1.0, path=target
    )
    assert target.exists()


def test_read_log_returns_empty_list_when_file_is_missing(tmp_path: Path) -> None:
    assert read_log(tmp_path / "does-not-exist.jsonl") == []


def test_read_log_skips_unparseable_lines(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_text('{"query": "ok", "hit_count": 1}\nnot json\n\n', encoding="utf-8")
    entries = read_log(target)
    assert len(entries) == 1
    assert entries[0]["query"] == "ok"


def test_compute_stats_reports_invocations_miss_rate_and_top_zero_hit() -> None:
    entries = [
        {"query": "a", "hit_count": 0},
        {"query": "a", "hit_count": 0},
        {"query": "b", "hit_count": 0},
        {"query": "c", "hit_count": 5},
    ]
    stats = compute_stats(entries, top=2)
    assert stats["invocations"] == 4
    assert stats["misses"] == 3
    assert stats["miss_rate"] == pytest.approx(0.75)
    assert stats["top_zero_hit_queries"] == [("a", 2), ("b", 1)]


def test_compute_stats_on_empty_log_does_not_divide_by_zero() -> None:
    stats = compute_stats([])
    assert stats["invocations"] == 0
    assert stats["miss_rate"] == 0.0
    assert stats["top_zero_hit_queries"] == []


def test_stats_cli_prints_summary_from_a_sample_log(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    append_invocation(
        query_text="lode-nt98",
        hit_count=0,
        fallback_fired=False,
        elapsed_ms=1.0,
        path=target,
    )
    append_invocation(
        query_text="lode-nt98",
        hit_count=0,
        fallback_fired=False,
        elapsed_ms=1.0,
        path=target,
    )
    append_invocation(
        query_text="retrieval",
        hit_count=4,
        fallback_fired=False,
        elapsed_ms=1.0,
        path=target,
    )
    result = runner.invoke(app, ["--log-path", str(target)])
    assert result.exit_code == 0, result.output
    assert "invocations: 3" in result.output
    assert "misses: 2" in result.output
    assert "lode-nt98" in result.output


def test_stats_cli_handles_no_invocations_logged(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--log-path", str(tmp_path / "empty.jsonl")])
    assert result.exit_code == 0
    assert "No invocations logged." in result.output
