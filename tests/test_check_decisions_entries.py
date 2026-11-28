"""Tests for the per-entry decision gate (lode-ebayj.2)."""

from pathlib import Path

import pytest
from _gitrepo import _git
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
_gate = load_module_from_path(
    "check_decisions_entries", REPO_ROOT / "scripts" / "check_decisions_entries.py"
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
    status: str = "decided",
    body: str = "body",
) -> Path:
    path = directory / f"{entry_id}-{slug}.md"
    path.write_text(
        f"---\nid: {entry_id}\ndate: {date}\nstatus: {status}\n"
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


@pytest.mark.parametrize(
    "date",
    [
        "",
        "2026-3-1",
        "20260301",
        "March 1",
        "2026-13-01",
        "2026-01-32",
        "２０２６-01-01",
    ],
)
def test_a_missing_or_malformed_date_is_flagged(entries: Path, date: str) -> None:
    _entry(entries, "aaaaa", date=date)
    assert _gate.check_edges(entries) == ["aaaaa-x.md: date must be YYYY-MM-DD"]


def test_an_entry_with_no_date_line_is_flagged(entries: Path) -> None:
    path = _entry(entries, "aaaaa")
    path.write_text(path.read_text().replace("date: 2026-01-01\n", ""))
    assert _gate.check_edges(entries) == ["aaaaa-x.md: date must be YYYY-MM-DD"]


@pytest.mark.parametrize("status", ["banana", "", "superseded"])
def test_a_status_outside_open_or_decided_is_flagged(
    entries: Path, status: str
) -> None:
    _entry(entries, "aaaaa", status=status)
    assert _gate.check_edges(entries) == [
        "aaaaa-x.md: status must be one of open|decided"
    ]


def test_open_status_passes(entries: Path) -> None:
    _entry(entries, "aaaaa", status="open")
    assert _gate.check_edges(entries) == []


@pytest.mark.parametrize(
    "later", ["[aaaaa]", "[aaaaa, bbbbb]"], ids=["sibling", "ancestor"]
)
def test_an_entry_superseded_by_two_entries_is_flagged(
    entries: Path, later: str
) -> None:
    """Every ``supersedes`` edge counts (``ancestor``), so a correction names
    only the entry it copies, not the entries that one superseded."""
    _entry(entries, "aaaaa")
    _entry(entries, "bbbbb", supersedes="[aaaaa]")
    _entry(entries, "ccccc", supersedes=later)
    assert _gate.check_edges(entries) == [
        "aaaaa-x.md: superseded by 2 entries (bbbbb, ccccc); only one may supersede it"
    ]


def test_an_id_listed_twice_in_one_list_counts_once(entries: Path) -> None:
    _entry(entries, "aaaaa")
    _entry(entries, "bbbbb", supersedes="[aaaaa, aaaaa]")
    assert _gate.check_edges(entries) == []
    _entry(entries, "ccccc", related="[nopqr, nopqr]")
    assert _gate.check_edges(entries) == [
        "ccccc-x.md: related target nopqr does not exist"
    ]


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


@pytest.mark.parametrize("value", ["", "aaaaa", "[aaaaa]  # ids only"])
def test_an_edge_value_that_is_not_an_inline_list_is_flagged(
    entries: Path, value: str
) -> None:
    """A blank value is how a YAML block list reads to the gate's line parser,
    so passing it would drop that list's edges without a word."""
    _entry(entries, "aaaaa")
    _entry(entries, "bbbbb", supersedes=value)
    assert _gate.check_edges(entries) == [
        "bbbbb-x.md: supersedes must be an inline [id, ...] list"
    ]


def test_supersedes_cycle_is_flagged(entries: Path) -> None:
    _entry(entries, "aaaaa", supersedes="[bbbbb]")
    _entry(entries, "bbbbb", supersedes="[aaaaa]")
    assert _gate.check_edges(entries) == ["supersedes cycle: aaaaa -> bbbbb -> aaaaa"]


def test_an_entry_superseding_itself_is_a_cycle(entries: Path) -> None:
    _entry(entries, "aaaaa", supersedes="[aaaaa]")
    assert _gate.check_edges(entries) == ["supersedes cycle: aaaaa -> aaaaa"]


def _repo_with_committed_entry(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    directory = repo / "docs" / "decisions"
    directory.mkdir(parents=True)
    _commit_entry(repo, directory, "aaaaa")
    return repo, directory


def _commit_entry(repo: Path, directory: Path, entry_id: str, **fields: str) -> Path:
    path = _entry(directory, entry_id, **fields)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", f"add {entry_id}")
    return path


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
    _commit_entry(repo, directory, "bbbbb")
    assert _gate.check_immutable(directory, "HEAD~1") == []


def test_an_entry_the_base_gained_after_the_fork_is_not_a_deletion(
    tmp_path: Path,
) -> None:
    """A branch under review is routinely behind the base ref. The entries the
    base gained since the fork are absent from the branch, not deleted by it;
    the branch's own edit of an entry it forked with is still caught."""
    repo, directory = _repo_with_committed_entry(tmp_path)
    _git(repo, "branch", "feature")
    _commit_entry(repo, directory, "bbbbb")
    _git(repo, "checkout", "-q", "feature")
    assert _gate.check_immutable(directory, "trunk") == []
    _entry(directory, "aaaaa", body="rewritten")
    assert _gate.check_immutable(directory, "trunk") == [
        "aaaaa-x.md: committed entry was modified"
    ]


def test_editing_a_committed_entry_with_a_non_ascii_slug_is_flagged(
    tmp_path: Path,
) -> None:
    """git quotes such a path in its default diff output, ``.md`` included."""
    repo, directory = _repo_with_committed_entry(tmp_path)
    path = _commit_entry(repo, directory, "bbbbb", slug="café")
    path.write_text(path.read_text() + "more\n")
    assert _gate.check_immutable(directory, "HEAD") == [
        "bbbbb-café.md: committed entry was modified"
    ]


def test_cli_exit_codes(tmp_path: Path) -> None:
    _, directory = _repo_with_committed_entry(tmp_path)
    ok = runner.invoke(_gate.app, ["--directory", str(directory), "--base", "HEAD"])
    assert ok.exit_code == 0, ok.output
    _entry(directory, "aaaaa", body="rewritten")
    bad = runner.invoke(_gate.app, ["--directory", str(directory), "--base", "HEAD"])
    assert bad.exit_code == 1
    _entry(directory, "bbbbb", related="[nopqr]")
    nobase = runner.invoke(
        _gate.app, ["--directory", str(directory), "--base", "no-such-ref"]
    )
    assert nobase.exit_code == 2
    assert "nopqr does not exist" in nobase.output


def test_the_real_decisions_directory_passes_the_edge_check() -> None:
    assert _gate.check_edges(REPO_ROOT / "docs" / "decisions") == []


def test_readme_is_at_most_15_lines() -> None:
    readme = REPO_ROOT / "docs" / "decisions" / "README.md"
    assert len(readme.read_text().splitlines()) <= 15


def test_gate_is_at_most_150_lines_of_code() -> None:
    """Every non-blank line that is not a comment counts, docstrings included."""
    source = (REPO_ROOT / "scripts" / "check_decisions_entries.py").read_text()
    code = [ln for ln in source.splitlines() if ln.strip() and ln.lstrip()[0] != "#"]
    assert len(code) <= 150
