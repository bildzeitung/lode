"""Tests for scripts/recycled-worktree-guard.sh (lode-ivth).

The recycled-worktree guard (lode-nt98) used to be a ~15-line inline bash
block duplicated at FOUR sites (.claude/agents/coding.md x2,
.claude/agents/code-reviewer.md x1, .claude/agents/land-review.md x1) --
shell in a markdown fence gets neither shellcheck nor a unit test, and the
four copies had already started to drift from each other (lode-qv5t added a
`.claude/worktrees/` precondition and a rescue-branch line to two of them but
not the third). This extracts the guard to one script so it is covered by
`nox -s shellcheck` and this suite.

All tests run the ACTUAL `scripts/recycled-worktree-guard.sh` against real
git repositories (with real `git worktree add` checkouts) built in
`tmp_path` -- no fake git, no mocked subprocess -- sabotage-provable per the
lode-verb bar: reverting the script's `case` guard, its ancestor check, or
its rescue-branch line directly would turn the corresponding test here red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "recycled-worktree-guard.sh"


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


def _add_worktree(
    repo: Path, rel_path: str, branch: str, *, foreign_commit: bool = False
) -> Path:
    """A real `git worktree add`, branched off `trunk`.

    With `foreign_commit=True`, one extra commit is made in the new worktree
    -- simulating a previous ticket's build branch that had already
    committed work. That is exactly what makes `HEAD` fail
    `git merge-base --is-ancestor HEAD trunk`: trunk doesn't contain the
    extra commit, matching the production shape (lode-eshl) of a recycled
    worktree still sitting on a foreign ticket's branch.
    """
    wt = repo / rel_path
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "-b", branch, "trunk")
    if foreign_commit:
        (wt / "foreign.txt").write_text("someone else's ticket\n")
        _git(wt, "add", "foreign.txt")
        _git(wt, "commit", "-q", "-m", "foreign ticket's build commit")
    return wt


def _run(
    worktree: Path, context: str = "before doing any work"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), context],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "script_args", [[], ["one", "two"]], ids=["no-args", "too-many-args"]
)
def test_wrong_argument_count_exits_2(tmp_path: Path, script_args: list[str]) -> None:
    """Anything but exactly one positional arg is a usage error (exit 2) -- a
    caller bug, distinct from a worktree problem (exit 0/1)."""
    repo = _init_repo(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), *script_args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr


def test_clean_worktree_at_trunk_head_is_a_noop(tmp_path: Path) -> None:
    """HEAD == trunk exactly (a genuinely fresh launch worktree): exit 0,
    nothing rescued, nothing reset."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/fresh", "worktree-agent-fresh")
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    branches = _git(repo, "branch", "--list", "rescue/*").stdout
    assert branches.strip() == ""


def test_worktree_merely_behind_trunk_is_still_a_noop(tmp_path: Path) -> None:
    """`trunk` advancing after the worktree was created (a normal fan-out
    race) must NOT trip the guard: HEAD is still an ancestor of the new
    trunk tip."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/behind", "worktree-agent-behind")
    # Advance trunk in the main checkout after the worktree was branched.
    _git(repo, "checkout", "-q", "trunk")
    (repo / "g.txt").write_text("trunk moved on\n")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-q", "-m", "trunk advances")
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before


def test_contaminated_worktree_is_rescued_reset_and_cleaned(tmp_path: Path) -> None:
    """The core repro (lode-nt98/lode-eshl): a launch worktree recycled onto
    a foreign ticket's build branch. Must: tag the foreign HEAD as
    rescue/recycled-<sha>, reset HEAD to trunk, and remove untracked
    leftovers (git clean -fd)."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo,
        ".claude/worktrees/recycled",
        "worktree-agent-other-ticket",
        foreign_commit=True,
    )
    foreign_sha_short = _git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
    foreign_sha_full = _git(wt, "rev-parse", "HEAD").stdout.strip()
    # Untracked leftovers from the recycled worktree's own build.
    (wt / "leftover.tmp").write_text("scratch file from the previous ticket\n")

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    trunk_sha = _git(repo, "rev-parse", "trunk").stdout.strip()
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == trunk_sha
    # The rescue tag is the durable pointer to the foreign commit -- it must
    # exist and resolve to exactly the commit HEAD was on before the reset,
    # never to trunk.
    rescue_sha = _git(
        repo, "rev-parse", f"rescue/recycled-{foreign_sha_short}"
    ).stdout.strip()
    assert rescue_sha == foreign_sha_full
    assert rescue_sha != trunk_sha
    assert not (wt / "leftover.tmp").exists()
    assert not (wt / "foreign.txt").exists()  # trunk never had this file


def test_reported_reason_names_lode_nt98_and_the_context_message(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo,
        ".claude/worktrees/recycled2",
        "worktree-agent-other-ticket-2",
        foreign_commit=True,
    )

    result = _run(wt, context="before my own fetch+checkout")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "lode-nt98" in result.stderr
    assert "CONTAMINATED LAUNCH WORKTREE" in result.stderr
    assert "before my own fetch+checkout" in result.stderr


def test_outside_isolated_worktree_refuses_and_leaves_everything_untouched(
    tmp_path: Path,
) -> None:
    """A recycled/contaminated checkout OUTSIDE `.claude/worktrees/` must be
    refused, not repaired -- this is the guard that keeps a failed
    `isolation: "worktree"` dispatch from ever reaching the main checkout
    with `reset --hard`/`clean -fd`."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo, "not-a-launch-worktree", "worktree-agent-elsewhere", foreign_commit=True
    )
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(wt)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing to reset" in result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    branches = _git(repo, "branch", "--list", "rescue/*").stdout
    assert branches.strip() == ""


def test_known_gap_worktree_recycled_onto_an_already_landed_branch_leaks_dirt(
    tmp_path: Path,
) -> None:
    """Pins the documented lode-3v1p gap: a worktree recycled onto a
    `land/<id>` branch that has SINCE LANDED has a HEAD that is already an
    ancestor of trunk (trunk fast-forwarded past it), so the ancestor check
    trivially passes and the guard no-ops -- any untracked leftover from that
    prior build survives. Harmless on the ancestry axis (matches /land's own
    reclaim predicate); this test exists so nobody "fixes" the ancestor
    check in a way that silently starts wiping dirt it was never designed to
    catch, without also updating the script's documented gap."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/landed", "worktree-agent-landed-ticket")
    # Simulate that ticket's branch having landed: fast-forward trunk to it.
    _git(repo, "checkout", "-q", "trunk")
    _git(repo, "merge", "-q", "--ff-only", "worktree-agent-landed-ticket")
    (wt / "leftover.tmp").write_text("dirt surviving the ancestry check\n")

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""  # no-op: nothing was reported, nothing reset
    assert (wt / "leftover.tmp").exists()  # the documented gap: dirt survives
