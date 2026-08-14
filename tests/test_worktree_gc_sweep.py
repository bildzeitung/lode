"""scripts/worktree-gc-sweep.sh -- the destructive end-of-pass reclaim, on real repos.

This was ~80 lines of `git worktree remove --force` / `git branch -D` fenced inside
land/SKILL.md, where nothing lint-checked it and only a markdown scanner could say
anything about it. These are the most dangerous operations in the harness, so the
cases below assert the *refusals* as hard as the reclaims:

  * a DIRTY worktree is kept, whatever its ancestry
  * a NOT-MERGED worktree is kept
  * running from a worktree instead of the main checkout refuses outright
  * the summary distinguishes "0 of 0" (idle) from "0 of N" (everything skipped)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from conftest import _CHECKOUT_ROOT as REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "worktree-gc-sweep.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk", ".")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    # The sweep only considers paths under .claude/worktrees/, and reads a tree as
    # clean only because build junk is ignored -- both are load-bearing here.
    (repo / ".gitignore").write_text("venv/\n.nox/\n__pycache__/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    # Copy scripts/ wholesale rather than cherry-picking: the sweep's classifier
    # sources shared helpers, and a partial copy fails in a way that looks like a
    # classifier bug rather than a fixture gap.
    shutil.copytree(REPO_ROOT / "scripts", repo / "scripts")
    return repo


def _add_wt(repo: Path, name: str, branch: str, start: str = "trunk") -> Path:
    _git(
        repo, "worktree", "add", "-q", "-b", branch, f".claude/worktrees/{name}", start
    )
    return repo / ".claude" / "worktrees" / name


def _sweep(repo: Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "scripts" / "worktree-gc-sweep.sh"), "--base-ref", "trunk"],
        cwd=cwd or repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_idle_sweep_reports_zero_of_zero(tmp_path: Path) -> None:
    r = _sweep(_repo(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "reclaimed 0 of 0" in r.stdout


def test_clean_merged_worktree_is_fully_reclaimed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_wt(repo, "agent-a", "worktree-agent-a")
    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "full=1" in r.stdout
    assert "agent-a" not in _git(repo, "worktree", "list")
    assert "worktree-agent-a" not in _git(repo, "branch", "--list")


def test_dirty_worktree_is_kept_even_though_it_is_merged(tmp_path: Path) -> None:
    """The invariant that matters most: leak a directory rather than destroy work.
    Zero divergence alone reads TRUE for a live, uncommitted build."""
    repo = _repo(tmp_path)
    wt = _add_wt(repo, "agent-b", "worktree-agent-b")
    (wt / "uncommitted.txt").write_text("work in progress")
    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "dirty=1" in r.stdout and "full=0" in r.stdout
    assert "agent-b" in _git(repo, "worktree", "list")
    assert (wt / "uncommitted.txt").exists()


def test_not_merged_worktree_is_kept(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wt = _add_wt(repo, "agent-c", "worktree-agent-c")
    (wt / "f.txt").write_text("x")
    _git(wt, "add", "f.txt")
    _git(wt, "commit", "-q", "-m", "diverged")
    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "not-merged=1" in r.stdout
    assert "agent-c" in _git(repo, "worktree", "list")


def test_everything_skipped_is_distinguishable_from_idle(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wt = _add_wt(repo, "agent-d", "worktree-agent-d")
    (wt / "dirty.txt").write_text("x")
    out = _sweep(repo).stdout
    assert "reclaimed 0 of 1" in out, (
        "a sweep that reclaimed nothing must not read as idle"
    )
    assert "reclaimed 0 of 0" not in out


def test_a_worktree_outside_the_claude_dir_is_never_a_candidate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "worktree", "add", "-q", "-b", "elsewhere", "../side", "trunk")
    r = _sweep(repo)
    assert "reclaimed 0 of 0" in r.stdout
    assert "side" in _git(repo, "worktree", "list")


def test_running_from_a_worktree_refuses_with_exit_2(tmp_path: Path) -> None:
    """It would otherwise enumerate that worktree's own siblings and reclaim them."""
    repo = _repo(tmp_path)
    wt = _add_wt(repo, "agent-e", "worktree-agent-e")
    r = _sweep(repo, cwd=wt)
    assert r.returncode == 2, r.stdout
    assert "agent-e" in _git(repo, "worktree", "list")


def test_bare_ref_backstop_keeps_a_land_ref_whose_remote_still_exists(
    tmp_path: Path,
) -> None:
    """The suffix-strip case: a local `land/<id>--<worktree-dir>` must map back to
    origin's `land/<id>`. Comparing raw would make the keep-arm dead code and
    force-delete an in-flight ticket's ref along with its unpushed commits."""
    origin = tmp_path / "o"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "trunk", ".")
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "trunk")
    _git(repo, "branch", "land/t1", "trunk")
    _git(repo, "push", "-q", "origin", "land/t1")
    _git(repo, "branch", "land/t1--agent-xyz", "trunk")  # a reviewer's local name
    _git(repo, "fetch", "-q", "origin")

    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    branches = _git(repo, "branch", "--list")
    assert "land/t1--agent-xyz" in branches, (
        "suffixed ref deleted despite its remote existing"
    )


def test_bare_ref_backstop_deletes_a_land_ref_whose_remote_is_gone(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "o"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "trunk", ".")
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "trunk")
    _git(repo, "branch", "land/gone", "trunk")
    _git(repo, "fetch", "-q", "origin")

    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "land/gone" not in _git(repo, "branch", "--list")
