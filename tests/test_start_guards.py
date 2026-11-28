"""Tests for scripts/start-guards.sh (lode-fg7w7.4).

The wrapper only sequences scripts/isolation-guard.sh and
scripts/recycled-worktree-guard.sh (each pinned by its own test module); these
tests pin the sequencing contract: pass path, each guard's nonzero exit mapped to
its own wrapper status, and the second guard not running after the first stops.

Real scripts, real git repositories in `tmp_path` -- no mocked subprocess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "start-guards.sh"


def _init_repo(tmp_path: Path, *, with_origin: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    if with_origin:
        origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
        _git(repo, "remote", "add", "origin", str(origin))
        _git(repo, "push", "-q", "-u", "origin", "trunk")
    return repo


def _add_worktree(
    repo: Path, *, base: str = "origin/trunk", foreign_commit: bool = False
) -> Path:
    wt = repo / ".claude" / "worktrees" / "agent-x"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "worktree-agent-x", base)
    if foreign_commit:
        (wt / "foreign.txt").write_text("someone else's ticket\n")
        _git(wt, "add", "foreign.txt")
        _git(wt, "commit", "-q", "-m", "foreign")
    return wt


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_both_guards_pass_exits_0(tmp_path: Path) -> None:
    wt = _add_worktree(_init_repo(tmp_path))
    result = _run(wt, "before doing any work")
    assert result.returncode == 0, result.stdout + result.stderr


def test_recycled_worktree_is_repaired_and_still_exits_0(tmp_path: Path) -> None:
    """The recycled guard's exit-0-after-repair case passes through unchanged,
    and the context message reaches its diagnostic."""
    wt = _add_worktree(_init_repo(tmp_path), foreign_commit=True)
    result = _run(wt, "before doing any work")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (wt / "foreign.txt").exists()
    assert "before doing any work" in result.stderr


def test_isolation_refusal_is_exit_3_and_recycled_guard_never_runs(
    tmp_path: Path,
) -> None:
    """Run from the main checkout: isolation-guard refuses (exit 1). Had the
    recycled guard run it would also refuse; its CONTAMINATED/NOT-in-worktree
    text must be absent, proving the wrapper stopped at the first refusal."""
    repo = _init_repo(tmp_path, with_origin=False)
    result = _run(repo, "before doing any work")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "NOT DISPATCHED INTO AN ISOLATED WORKTREE" in result.stderr
    assert "isolation-guard.sh exited 1" in result.stderr
    assert "NOT in an isolated launch worktree" not in result.stderr


def test_recycled_guard_nonzero_is_exit_4(tmp_path: Path) -> None:
    """Isolation passes (a worktree under .claude/worktrees/) but the recycled
    guard cannot run: no `origin/trunk` -> its exit 2."""
    wt = _add_worktree(_init_repo(tmp_path, with_origin=False), base="trunk")
    result = _run(wt, "before doing any work")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "GUARD COULD NOT RUN" in result.stderr
    assert "recycled-worktree-guard.sh exited 2" in result.stderr


def test_wrong_argument_count_exits_2(tmp_path: Path) -> None:
    for args in ([], ["one", "two"]):
        result = _run(tmp_path, *args)
        assert result.returncode == 2, result.stdout + result.stderr
