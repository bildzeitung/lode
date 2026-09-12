"""scripts/code-reclaim-launch-worktree.sh -- lode-7ndi.

`/code`'s per-ticket reclaim of a finished reviewer/rebase-pickup launch
worktree, extracted from `.claude/skills/code/SKILL.md`'s inline reclaim
block so the destructive calls (`git worktree unlock`, `git worktree remove
--force`, `git branch -D`) are shellcheck'd and pytest-covered.

Cases:
  * unlocked-and-clean                 -> reclaimed (worktree + branch gone)
  * harness-locked, reason names own dirname -> unlocked then reclaimed
  * locked, reason names something else (foreign / human lock) -> kept, reported
  * a `worktree-agent-*` builder worktree is never touched
  * no matching `land/<id>--*` worktree at all -> no-op, exit 0
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from _gitrepo import _git
from conftest import _CHECKOUT_ROOT as REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "code-reclaim-launch-worktree.sh"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk", ".")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    return repo


def _add_wt(repo: Path, name: str, branch: str, start: str = "trunk") -> Path:
    _git(
        repo, "worktree", "add", "-q", "-b", branch, f".claude/worktrees/{name}", start
    )
    return repo / ".claude" / "worktrees" / name


def _reclaim(
    repo: Path, ticket_id: str, *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), ticket_id],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )


def _worktrees(repo: Path) -> str:
    return _git(repo, "worktree", "list").stdout.strip()


def _branches(repo: Path) -> str:
    return _git(repo, "branch", "--list").stdout.strip()


def test_no_matching_worktree_is_a_noop(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _reclaim(repo, "lode-abc", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "reclaimed=0" in r.stdout


def test_unlocked_and_clean_is_reclaimed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_wt(repo, "agent-a", "land/lode-abc--agent-a")
    r = _reclaim(repo, "lode-abc", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "reclaimed=1" in r.stdout
    assert "agent-a" not in _worktrees(repo)
    assert "land/lode-abc--agent-a" not in _branches(repo)


def test_harness_locked_finished_is_unlocked_and_reclaimed(tmp_path: Path) -> None:
    """The core fix: a lock whose reason names the worktree's OWN directory is
    positive evidence the harness's launch lock never cleared on exit -- explicit
    unlock, not an indefinite wait, then the ordinary single-force remove."""
    repo = _repo(tmp_path)
    _add_wt(repo, "agent-b", "land/lode-abc--agent-b")
    _git(
        repo,
        "worktree",
        "lock",
        "--reason",
        "claude agent agent-b (pid 12345 start 999)",
        ".claude/worktrees/agent-b",
    )
    r = _reclaim(repo, "lode-abc", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "reclaimed=1" in r.stdout
    assert "agent-b" not in _worktrees(repo)
    assert "land/lode-abc--agent-b" not in _branches(repo)


def test_foreign_reason_lock_is_kept_and_reported(tmp_path: Path) -> None:
    """A lock reason that does NOT name this worktree's own directory -- a human's
    own lock, or any other reason -- must never be unlocked, even for a matching
    land/<id>--* branch."""
    repo = _repo(tmp_path)
    _add_wt(repo, "agent-c", "land/lode-abc--agent-c")
    _git(
        repo,
        "worktree",
        "lock",
        "--reason",
        "held by a human, do not touch",
        ".claude/worktrees/agent-c",
    )
    r = _reclaim(repo, "lode-abc", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "kept-foreign-lock=1" in r.stdout
    assert "reclaimed=0" in r.stdout
    assert "agent-c" in _worktrees(repo)
    assert "land/lode-abc--agent-c" in _branches(repo)


def test_builder_worktree_is_never_touched(tmp_path: Path) -> None:
    """A worktree-agent-* branch never matches the land/<id>--* glob at all --
    excluded by construction, not by a separate predicate."""
    repo = _repo(tmp_path)
    _add_wt(repo, "agent-d", "worktree-agent-d")
    r = _reclaim(repo, "lode-abc", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "reclaimed=0" in r.stdout
    assert "agent-d" in _worktrees(repo)
    assert "worktree-agent-d" in _branches(repo)


def test_only_matches_the_named_ticket(tmp_path: Path) -> None:
    """A land/<other-id>--* worktree must not be swept up by a different ticket's
    reclaim -- the glob is anchored on the exact ticket id, not a prefix match."""
    repo = _repo(tmp_path)
    _add_wt(repo, "agent-e", "land/lode-other--agent-e")
    r = _reclaim(repo, "lode-abc", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "reclaimed=0" in r.stdout
    assert "agent-e" in _worktrees(repo)


def test_usage_error_on_missing_ticket_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _reclaim(repo, "", cwd=repo)
    assert r.returncode == 2
