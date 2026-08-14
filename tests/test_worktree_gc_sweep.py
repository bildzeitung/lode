"""scripts/worktree-gc-sweep.sh -- the destructive end-of-pass reclaim, on real repos.

This was ~80 lines of `git worktree remove --force` / `git branch -D` fenced inside
land/SKILL.md, where nothing lint-checked it and only a markdown scanner could say
anything about it. These are the most dangerous operations in the harness, so the
cases below assert the *refusals* as hard as the reclaims:

  * a DIRTY worktree is kept, whatever its ancestry
  * a NOT-MERGED worktree is kept
  * a LOCKED worktree is kept -- the "rip a worktree out from under a running
    agent" harm the whole locked branch of the loop exists to prevent
  * the `dir-only` arm removes the DIRECTORY but deliberately KEEPS the ref
  * backstop 3 keeps a `worktree-agent-*` ref that is still checked out or not
    yet merged -- that namespace is never pushed (lode-yrtu), so a wrong
    deletion there has no origin copy to recover from
  * running from a worktree instead of the main checkout refuses outright
  * the summary distinguishes "0 of 0" (idle) from "0 of N" (everything skipped)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from _gitrepo import _git
from conftest import _CHECKOUT_ROOT as REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "worktree-gc-sweep.sh"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk", ".")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
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


def _sweep(
    repo: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # `env` OVERLAYS the real environment rather than replacing it -- the sweep
    # shells out to git, which needs HOME/PATH to behave at all.
    # `args` exists ONLY so the rejection test below can launch the sweep the
    # same way every other test does; the script itself takes none (lode-0867).
    return subprocess.run(
        ["bash", str(repo / "scripts" / "worktree-gc-sweep.sh"), *(args or [])],
        cwd=cwd or repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _worktrees(repo: Path) -> str:
    return _git(repo, "worktree", "list").stdout.strip()


def _branches(repo: Path) -> str:
    return _git(repo, "branch", "--list").stdout.strip()


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
    assert "agent-a" not in _worktrees(repo)
    assert "worktree-agent-a" not in _branches(repo)


def test_dirty_worktree_is_kept_even_though_it_is_merged(tmp_path: Path) -> None:
    """The invariant that matters most: leak a directory rather than destroy work.
    Zero divergence alone reads TRUE for a live, uncommitted build."""
    repo = _repo(tmp_path)
    wt = _add_wt(repo, "agent-b", "worktree-agent-b")
    (wt / "uncommitted.txt").write_text("work in progress")
    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "dirty=1" in r.stdout and "full=0" in r.stdout
    assert "agent-b" in _worktrees(repo)
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
    assert "agent-c" in _worktrees(repo)


def test_a_live_lock_keeps_the_worktree(tmp_path: Path) -> None:
    """A lock the sweep cannot positively prove dead must fail CLOSED. This is the
    only path that turns a LOCKED worktree into a destroyable one, and getting the
    signal wrong rips a directory out from under a running agent -- the harm the
    locked branch of the loop exists to prevent."""
    repo = _repo(tmp_path)
    _add_wt(repo, "agent-f", "worktree-agent-f")
    # A reason string worktree-lock-stale.sh cannot parse as a proven-dead session.
    _git(
        repo,
        "worktree",
        "lock",
        "--reason",
        "held by a live session",
        ".claude/worktrees/agent-f",
    )
    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "locked=1" in r.stdout and "full=0" in r.stdout
    assert "agent-f" in _worktrees(repo)
    assert "worktree-agent-f" in _branches(repo)


def test_dir_only_reclaim_removes_the_directory_but_keeps_the_ref(
    tmp_path: Path,
) -> None:
    """The `dir-only` arm's DEFINING property: a builder branch is never pushed
    (lode-yrtu), so its commits stay reachable only through the local ref. A
    copy-paste of `git branch -D` from the full-reclaim arm below it would destroy
    exactly what this arm exists to preserve."""
    repo = _repo(tmp_path)
    wt = _add_wt(repo, "agent-g", "worktree-agent-g")
    (wt / "f.txt").write_text("x")
    _git(wt, "add", "f.txt")
    _git(wt, "commit", "-q", "-m", "unpushed builder work")
    # Age floor to 0 so the just-made commit clears it; without this the candidate
    # is (correctly) kept as not-merged.
    r = _sweep(repo, env={"LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS": "0"})
    assert r.returncode == 0, r.stderr
    assert "dir-only=1" in r.stdout
    assert "agent-g" not in _worktrees(repo)
    assert "worktree-agent-g" in _branches(repo), (
        "the un-pushed builder ref was deleted -- its commits are unrecoverable"
    )


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
    assert "side" in _worktrees(repo)


def test_running_from_a_worktree_refuses_with_exit_2(tmp_path: Path) -> None:
    """It would otherwise enumerate that worktree's own siblings and reclaim them."""
    repo = _repo(tmp_path)
    wt = _add_wt(repo, "agent-e", "worktree-agent-e")
    r = _sweep(repo, cwd=wt)
    assert r.returncode == 2, r.stdout
    assert "agent-e" in _worktrees(repo)


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
    assert "land/t1--agent-xyz" in _branches(repo), (
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
    assert "land/gone" not in _branches(repo)


def test_backstop3_deletes_a_merged_unattached_builder_ref(tmp_path: Path) -> None:
    """The `worktree-agent-*` namespace accumulates orphan refs invisible to both
    nets above (17 confirmed on one machine)."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "worktree-agent-orphan", "trunk")  # merged, no worktree
    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "backstop3" in r.stdout
    assert "worktree-agent-orphan" not in _branches(repo)


def test_default_base_ref_is_trunk_with_no_flag_passed(tmp_path: Path) -> None:
    """Pins the script's IMPLICIT base ref as `trunk` now that --base-ref has been
    removed entirely (lode-0867). The fixture is what makes this test able to fail
    on its own rather than only in lockstep with the backstop-3 test above: it
    plants a SECOND branch, `main` -- the upstream export's base ref, the exact
    value a careless re-port would reintroduce -- at an EARLIER commit, so the
    candidate ref is merged into `trunk` but NOT into `main`. Judge against
    `trunk` and it is reclaimed; judge against `main` and it survives."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "main", "trunk~1")  # does NOT contain trunk's tip
    _git(repo, "branch", "worktree-agent-default-check", "trunk")  # merged into trunk
    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "worktree-agent-default-check" not in _branches(repo)


def test_a_base_ref_argument_is_rejected(tmp_path: Path) -> None:
    """The flag was removed outright (lode-0867), not merely defaulted -- passing
    one must fail loudly rather than being silently ignored."""
    repo = _repo(tmp_path)
    r = _sweep(repo, args=["--base-ref", "trunk"])
    assert r.returncode == 2
    assert "GATE COULD NOT RUN" in r.stderr


def test_backstop3_keeps_an_unmerged_builder_ref(tmp_path: Path) -> None:
    """The guard that matters: this namespace is NEVER pushed to origin (lode-yrtu),
    so unlike backstop 2 there is no remote copy to recover from. A ref whose
    commits are not yet in trunk must survive even with no worktree attached --
    that is exactly the state the `dir-only` arm above deliberately leaves behind."""
    repo = _repo(tmp_path)
    wt = _add_wt(repo, "agent-h", "worktree-agent-h")
    (wt / "f.txt").write_text("x")
    _git(wt, "add", "f.txt")
    _git(wt, "commit", "-q", "-m", "unpushed builder work")
    _git(repo, "worktree", "remove", "--force", ".claude/worktrees/agent-h")
    r = _sweep(repo)
    assert r.returncode == 0, r.stderr
    assert "worktree-agent-h" in _branches(repo), (
        "an un-merged, never-pushed builder ref was force-deleted"
    )
