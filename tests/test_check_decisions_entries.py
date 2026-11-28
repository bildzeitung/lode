"""Tests for the per-entry decision gate and its docs-index coverage (lode-ebayj.2)."""

import functools
from pathlib import Path

import pytest
from _gitrepo import _commit_file, _git
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
_gate = load_module_from_path(
    "check_decisions_entries", REPO_ROOT / "scripts" / "check_decisions_entries.py"
)
_query = load_module_from_path(
    "docs_index_query_for_decisions", REPO_ROOT / "scripts" / "docs_index_query.py"
)
runner = CliRunner()


def _entry(
    directory: Path,
    entry_id: str,
    *,
    slug: str = "x",
    supersedes: str = "[]",
    related: str = "[]",
    date: str = "2026-01-01",
    body: str = "body",
) -> Path:
    path = directory / f"{entry_id}-{slug}.md"
    path.write_text(
        f"---\nid: {entry_id}\ndate: {date}\nstatus: decided\n"
        f"supersedes: {supersedes}\nrelated: {related}\n---\n{body}\n"
    )
    return path


@pytest.fixture
def entries(tmp_path: Path) -> Path:
    directory = tmp_path / "docs" / "decisions"
    directory.mkdir(parents=True)
    return directory


def test_a_passing_supersedes_chain(entries: Path) -> None:
    _entry(entries, "aaaaa")
    _entry(entries, "bbbbb", supersedes="[aaaaa]", related="[aaaaa]", date="2026-02-01")
    _entry(entries, "ccccc", supersedes="[bbbbb]", date="2026-03-01")
    assert _gate.check_edges(entries) == []
    loaded, _ = _gate.load_entries(entries)
    assert _gate.chain_head(loaded, "aaaaa") == "ccccc"
    assert _gate.chain_head(loaded, "ccccc") == "ccccc"


def test_duplicate_id_is_flagged(entries: Path) -> None:
    _entry(entries, "aaaaa", slug="one")
    _entry(entries, "aaaaa", slug="two")
    assert any("duplicate id aaaaa" in p for p in _gate.check_edges(entries))


def test_id_filename_mismatch_is_flagged(entries: Path) -> None:
    path = _entry(entries, "aaaaa")
    path.rename(entries / "zzzzz-x.md")
    assert any("does not match filename" in p for p in _gate.check_edges(entries))


@pytest.mark.parametrize("key", ["supersedes", "related"])
def test_dangling_edge_is_flagged(entries: Path, key: str) -> None:
    _entry(entries, "aaaaa", **{key: "[nopqr]"})
    assert any("nopqr does not exist" in p for p in _gate.check_edges(entries))


def test_supersedes_cycle_is_flagged(entries: Path) -> None:
    _entry(entries, "aaaaa", supersedes="[bbbbb]")
    _entry(entries, "bbbbb", supersedes="[aaaaa]")
    assert "supersedes cycle" in _gate.check_edges(entries)


def _repo_with_committed_entry(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    directory = repo / "docs" / "decisions"
    directory.mkdir(parents=True)
    _entry(directory, "aaaaa")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, directory


def test_editing_a_committed_entry_is_flagged(tmp_path: Path) -> None:
    _, directory = _repo_with_committed_entry(tmp_path)
    _entry(directory, "aaaaa", body="rewritten")
    assert any("modified" in p for p in _gate.check_immutable(directory, "HEAD"))


def test_deleting_a_committed_entry_is_flagged(tmp_path: Path) -> None:
    _, directory = _repo_with_committed_entry(tmp_path)
    (directory / "aaaaa-x.md").unlink()
    assert any("deleted" in p for p in _gate.check_immutable(directory, "HEAD"))


def test_adding_an_entry_passes_the_immutability_check(tmp_path: Path) -> None:
    repo, directory = _repo_with_committed_entry(tmp_path)
    _commit_file(repo, "docs/decisions/bbbbb-y.md", "---\nid: bbbbb\n---\n", "add")
    assert _gate.check_immutable(directory, "HEAD~1") == []


def test_cli_exit_codes(tmp_path: Path) -> None:
    _, directory = _repo_with_committed_entry(tmp_path)
    ok = runner.invoke(_gate_app(), ["--directory", str(directory), "--base", "HEAD"])
    assert ok.exit_code == 0, ok.output
    _entry(directory, "aaaaa", body="rewritten")
    bad = runner.invoke(_gate_app(), ["--directory", str(directory), "--base", "HEAD"])
    assert bad.exit_code == 1
    nobase = runner.invoke(
        _gate_app(), ["--directory", str(directory), "--base", "no-such-ref"]
    )
    assert nobase.exit_code == 2


def _gate_app():
    import typer

    app = typer.Typer()
    app.command()(_gate.main)
    return app


def test_the_real_decisions_directory_passes_the_edge_check() -> None:
    assert _gate.check_edges(REPO_ROOT / "docs" / "decisions") == []


def test_readme_is_at_most_15_lines() -> None:
    readme = REPO_ROOT / "docs" / "decisions" / "README.md"
    assert len(readme.read_text().splitlines()) <= 15


def test_gate_is_at_most_150_lines_of_code() -> None:
    import ast

    path = REPO_ROOT / "scripts" / "check_decisions_entries.py"
    source = path.read_text()
    docstrings = {
        n.body[0].lineno
        for n in ast.walk(ast.parse(source))
        if isinstance(n, (ast.Module, ast.FunctionDef))
        and n.body
        and isinstance(n.body[0], ast.Expr)
    }
    code = [
        ln
        for i, ln in enumerate(source.splitlines(), 1)
        if ln.strip() and not ln.lstrip().startswith("#") and i not in docstrings
    ]
    assert len(code) <= 150


def _docs_with_chain(tmp_path: Path) -> Path:
    docs_dir = tmp_path / "docs"
    directory = docs_dir / "decisions"
    directory.mkdir(parents=True)
    _entry(directory, "aaaaa", body="# Old\n\nstale quuxlarb claim")
    _entry(
        directory,
        "bbbbb",
        slug="new",
        supersedes="[aaaaa]",
        date="2026-02-01",
        body="# New\n\nfresh claim",
    )
    return docs_dir


def test_docs_index_finds_an_entry_and_names_the_chain_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs_dir = _docs_with_chain(tmp_path)
    monkeypatch.setattr(
        _query._build,
        "build_index",
        functools.partial(_query._build.build_index, docs_dir=docs_dir),
    )

    result = runner.invoke(_query.app, ["quuxlarb"])
    assert result.exit_code == 0, result.output
    assert "aaaaa-x.md" in result.stdout
    assert str(docs_dir / "decisions" / "bbbbb-new.md") in result.stdout

    result = runner.invoke(_query.app, ["fresh"])
    assert "bbbbb-new.md" in result.stdout
    assert "superseded" not in result.stdout
