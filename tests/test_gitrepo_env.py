"""Test-side git helpers ignore the caller's repository-locating env vars (lode-wcpss)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _gitrepo import _commit_file, _git, _local_env_vars, git_env, os_env
from conftest import tracked_paths


def _repo(path: Path, filename: str) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "trunk")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@t")
    _commit_file(path, filename, "x\n", filename)
    return path


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch repo, built while GIT_DIR/GIT_WORK_TREE point at a decoy repo.

    Every other locating variable is unset first: a caller's own GIT_INDEX_FILE (a commit
    hook's) would otherwise decide what the unscrubbed call in the non-vacuity test lists.
    """
    for name in _local_env_vars():
        monkeypatch.delenv(name, raising=False)
    decoy = _repo(tmp_path / "decoy", "decoy.txt")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    return _repo(tmp_path / "scratch", "scratch.txt")


def test_git_helper_acts_on_scratch_repo_not_decoy(scratch: Path) -> None:
    assert _git(scratch, "ls-files").stdout.split() == ["scratch.txt"]


def test_tracked_paths_acts_on_root_not_decoy(scratch: Path) -> None:
    assert tracked_paths(root=scratch) == ["scratch.txt"]


@pytest.mark.parametrize(
    "name",
    [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
    ],
)
def test_git_env_drops_a_locating_var(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, "x")
    assert name not in git_env()


def test_git_env_keeps_extra() -> None:
    env = git_env({"GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
    assert env["GIT_COMMITTER_DATE"] == "2026-01-01T00:00:00Z"


def test_git_env_repr_shows_no_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing ``subprocess.run(..., env=git_env(), check=True)`` has its arguments
    printed by pytest; the environment's names and values must not be among them."""
    monkeypatch.setenv("LODE_PROBE_SECRET", "hunter2")
    env = git_env({"GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
    assert env["LODE_PROBE_SECRET"] == "hunter2"
    shown = f"{env!r} {env}"
    assert "hunter2" not in shown
    assert "LODE_PROBE_SECRET" not in shown
    assert "GIT_COMMITTER_DATE" not in shown


def test_git_env_keeps_config_passed_through_the_environment(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git -c`` and GIT_CONFIG_COUNT config is not a repository location: it still
    reaches the scratch repo."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "lode.probe")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "count")
    assert _git(scratch, "config", "lode.probe").stdout.strip() == "count"
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'lode.probe'='parameters'")
    assert _git(scratch, "config", "lode.probe").stdout.strip() == "parameters"


def test_unscrubbed_git_would_hit_decoy(scratch: Path) -> None:
    """Non-vacuity: with the env inherited as-is, cwd=scratch lists the decoy."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=scratch, capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["decoy.txt"]


def test_os_env_keeps_every_variable_and_shows_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.environ`` is replaced for the call, not added to: a failing assert prints what it
    compared, and that must not be the real environment."""
    environ = {"LODE_PROBE_SECRET": "hunter2", "GIT_DIR": "/decoy"}
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", environ)
        env = os_env({"EXTRA": "x"})
    assert dict(env) == {**environ, "EXTRA": "x"}
    assert repr(env) == "<env: 3 variables>"
