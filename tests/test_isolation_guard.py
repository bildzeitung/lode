"""Tests for scripts/isolation-guard.sh (lode-ska2 / lode-jk44).

A `code-reviewer` dispatch was observed with its cwd pinned to the MAIN
CHECKOUT at the repo root, checked out on `trunk` -- `isolation: "worktree"`
never took at all. This is a DIFFERENT failure from
scripts/recycled-worktree-guard.sh's lode-nt98 (a *recycled* worktree still
on a previous ticket's branch -- a worktree, just the wrong one): here there
was no worktree whatsoever. Both documented `EnterWorktree` self-rescue
routes were refused by the harness, and nothing MECHANICAL then stopped the
dispatched agent from running Edit/Write/`nox -t fix` directly against the
main checkout on trunk -- only an English "if my cwd is trunk, STOP"
instruction held, and that same incident's agent went on to invent an
unsanctioned `git worktree add` + `git -C` workaround instead of actually
stopping.

This script closes the gap: a single, shellcheck'd, unit-tested precondition
-- "do I have an isolated worktree AT ALL" -- run as the first executable
action of the cycle, before anything else. Unlike recycled-worktree-guard.sh,
it never repairs anything on failure (there is no safe way to fabricate an
isolated worktree from a non-isolated context); the only sanctioned response
is a hard stop, which is exactly what exit 1 signals here.

All tests run the ACTUAL `scripts/isolation-guard.sh` against real git
repositories (with real `git worktree add` checkouts) built in `tmp_path` --
no fake git, no mocked subprocess -- sabotage-provable per the lode-verb bar:
reverting the script's `case` guard directly would turn the corresponding
test here red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "isolation-guard.sh"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway repo with one commit on `trunk`, isolated user config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _add_worktree(repo: Path, rel_path: str, branch: str) -> Path:
    wt = repo / rel_path
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "-b", branch, "trunk")
    return wt


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_wrong_argument_count_exits_2(tmp_path: Path) -> None:
    """Any argument at all is a usage error (exit 2) -- this is a pure,
    unparametrized precondition, distinct from a worktree problem (0/1)."""
    repo = _init_repo(tmp_path)
    result = _run(repo, "unexpected-arg")
    assert result.returncode == 2, result.stdout + result.stderr


def test_cwd_under_claude_worktrees_passes(tmp_path: Path) -> None:
    """The genuinely-isolated case: cwd is a worktree under
    `.claude/worktrees/` -- exit 0, nothing printed to stderr."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/agent-abc123", "worktree-agent-abc123")

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


def test_main_checkout_at_repo_root_is_refused(tmp_path: Path) -> None:
    """The EXACT lode-ska2 production repro: cwd is the main checkout itself
    (not any worktree at all), sitting on trunk. Must refuse (exit 1) and
    name lode-ska2 plus the hard-stop instruction in its diagnostic."""
    repo = _init_repo(tmp_path)

    result = _run(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "lode-ska2" in result.stderr
    assert "NOT DISPATCHED INTO AN ISOLATED WORKTREE" in result.stderr
    assert "STOP AND REPORT" in result.stderr
    # The whole point: the diagnostic must foreclose self-rescue, not just
    # describe the problem -- a caller reading only this message must not be
    # able to talk itself into EnterWorktree or `git worktree add` next.
    assert "EnterWorktree" in result.stderr
    assert "git worktree add" in result.stderr


def test_a_worktree_outside_claude_worktrees_is_also_refused(tmp_path: Path) -> None:
    """A real `git worktree add` checkout, just not under `.claude/worktrees/`,
    is still not an isolated launch worktree -- exit 1, same as the bare
    main-checkout case. `.claude/worktrees/` is the only path the harness's
    own dispatch is documented to use."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, "not-a-launch-worktree", "worktree-agent-elsewhere")

    result = _run(wt)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT DISPATCHED INTO AN ISOLATED WORKTREE" in result.stderr


def test_refusal_never_mutates_anything(tmp_path: Path) -> None:
    """Unlike recycled-worktree-guard.sh, this script never repairs on
    failure -- confirm HEAD, branches, and the working tree are untouched
    either way."""
    repo = _init_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "untracked.txt").write_text("must survive\n")

    result = _run(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (repo / "untracked.txt").exists()
    branches = _git(repo, "branch", "--list", "rescue/*").stdout
    assert branches.strip() == ""
