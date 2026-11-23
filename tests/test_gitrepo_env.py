"""Test-side git helpers ignore the caller's repository-locating env vars (lode-wcpss)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _gitrepo import _git, git_env
from conftest import tracked_paths


def _repo(path: Path, filename: str) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "trunk")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@t")
    (path / filename).write_text("x\n")
    _git(path, "add", filename)
    _git(path, "commit", "-q", "-m", filename)
    return path


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch repo, built while GIT_DIR/GIT_WORK_TREE point at a decoy repo."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    decoy = _repo(tmp_path / "decoy", "decoy.txt")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    return _repo(tmp_path / "scratch", "scratch.txt")


def test_git_helper_acts_on_scratch_repo_not_decoy(scratch: Path) -> None:
    assert _git(scratch, "ls-files").stdout.split() == ["scratch.txt"]


def test_tracked_paths_acts_on_root_not_decoy(scratch: Path) -> None:
    assert tracked_paths(root=scratch) == ["scratch.txt"]


def test_git_env_drops_locating_vars_keeps_extra(scratch: Path) -> None:
    env = git_env({"GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert env["GIT_COMMITTER_DATE"] == "2026-01-01T00:00:00Z"


def test_unscrubbed_git_would_hit_decoy(scratch: Path) -> None:
    """Non-vacuity: with the env inherited as-is, cwd=scratch lists the decoy."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=scratch, capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["decoy.txt"]
