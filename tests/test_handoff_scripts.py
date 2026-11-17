"""Tests for scripts/handoff-sha.sh, check-review-head.sh and
checkout-land-branch.sh (lode-qh1m): each derives a value and uses it in one
process, so a dispatched agent never copies a SHA or directory name by hand.

`bd` is stubbed with a PATH shim that records its argv, so no tracker is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from _gitrepo import _git
from conftest import fake_bd, fake_bin_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _out(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "commit", "-q", "--allow-empty", "-m", "one")
    return r


def _bd_log(bindir: Path) -> Path:
    return bindir.parent / "bd.log"


@pytest.fixture
def bd(tmp_path: Path) -> Path:
    """A bin dir holding a fake `bd` that logs its argv to :func:`_bd_log`,
    serves BD_SHOW_JSON for `show`, and succeeds at everything else."""
    bindir = tmp_path / "bin"
    return fake_bd(
        bindir,
        {"show": 'printf "%s" "${BD_SHOW_JSON-}"'},
        default="exit 0",
        call_log=_bd_log(bindir),
    )


def _run(
    script: str, cwd: Path, *args: str, bindir: Path | None = None, **env: str
) -> subprocess.CompletedProcess[str]:
    base = fake_bin_env(bindir) if bindir else dict(os.environ)
    return subprocess.run(
        [str(SCRIPTS / script), *args],
        cwd=cwd,
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def pushed(repo: Path, tmp_path: Path) -> Path:
    """`repo` with an origin whose land/lode-x is HEAD (the state the call
    sites guarantee before running handoff-sha.sh)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/land/lode-x")
    return repo


def test_handoff_writes_the_derived_head_and_passes_extra_args(
    pushed: Path, bd: Path
) -> None:
    head = _out(pushed, "rev-parse", "HEAD")
    r = _run(
        "handoff-sha.sh", pushed, "lode-x", "land_head", "--add-label", "l", bindir=bd
    )
    assert r.returncode == 0, r.stderr
    assert _bd_log(bd).read_text().strip() == (
        f"update lode-x --set-metadata land_head={head} --add-label l"
    )


def test_handoff_broken_call_is_exit_2_and_writes_nothing(
    pushed: Path, bd: Path
) -> None:
    assert _run("handoff-sha.sh", pushed, "lode-x", bindir=bd).returncode == 2
    assert _run("handoff-sha.sh", pushed, "lode-x", "nope", bindir=bd).returncode == 2
    assert not _bd_log(bd).exists()


def test_handoff_bd_failure_is_exit_3(pushed: Path, bd: Path) -> None:
    fake_bd(bd, default="exit 1")
    r = _run("handoff-sha.sh", pushed, "lode-x", "review_head", bindir=bd)
    assert r.returncode == 3
    assert "NOT written" in r.stderr


def test_handoff_validates_the_derived_value_before_writing(
    pushed: Path, bd: Path
) -> None:
    """The validator call is what stands between a malformed derive and the
    write: a `git` that prints a 39-character SHA must reach no `bd update`."""
    fake_git = bd / "git"
    fake_git.write_text(f"#!/usr/bin/env bash\necho {'a' * 39}\n")
    fake_git.chmod(0o755)
    r = _run("handoff-sha.sh", pushed, "lode-x", "land_head", bindir=bd)
    assert r.returncode == 1
    assert "MALFORMED" in r.stderr
    assert not _bd_log(bd).exists()


def test_handoff_refuses_when_origin_lacks_head(pushed: Path, bd: Path) -> None:
    _git(pushed, "commit", "-q", "--allow-empty", "-m", "late")
    r = _run("handoff-sha.sh", pushed, "lode-x", "review_head", bindir=bd)
    assert r.returncode == 4
    assert "push HEAD first" in r.stderr
    assert not _bd_log(bd).exists()


def test_handoff_refuses_when_land_branch_absent(pushed: Path, bd: Path) -> None:
    r = _run("handoff-sha.sh", pushed, "lode-y", "review_head", bindir=bd)
    assert r.returncode == 4
    assert "absent" in r.stderr
    assert not _bd_log(bd).exists()


def test_handoff_without_origin_is_exit_5(repo: Path, bd: Path) -> None:
    r = _run("handoff-sha.sh", repo, "lode-x", "review_head", bindir=bd)
    assert r.returncode == 5
    assert not _bd_log(bd).exists()


def test_handoff_outside_a_repo_never_writes(tmp_path: Path, bd: Path) -> None:
    r = _run(
        "handoff-sha.sh",
        tmp_path,
        "lode-x",
        "review_head",
        bindir=bd,
        GIT_CEILING_DIRECTORIES=str(tmp_path.parent),
    )
    assert r.returncode != 0
    assert not _bd_log(bd).exists()


def _show(review_head: str | None) -> str:
    meta = {} if review_head is None else {"review_head": review_head}
    return json.dumps([{"metadata": meta}])


def _check(repo: Path, bd: Path, show_json: str) -> subprocess.CompletedProcess[str]:
    return _run(
        "check-review-head.sh", repo, "lode-x", bindir=bd, BD_SHOW_JSON=show_json
    )


def test_check_review_head_forward_only_is_exit_0(repo: Path, bd: Path) -> None:
    old = _out(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    r = _check(repo, bd, _show(old))
    assert r.returncode == 0, r.stderr


def test_check_review_head_unreachable_is_exit_3(repo: Path, bd: Path) -> None:
    old = _out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--orphan", "other")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "rewritten")
    r = _check(repo, bd, _show(old))
    assert r.returncode == 3
    assert "UNREACHABLE" in r.stderr


@pytest.mark.parametrize("value", [None, "abc123"])
def test_check_review_head_missing_or_malformed_is_exit_1(
    repo: Path, bd: Path, value: str | None
) -> None:
    assert _check(repo, bd, _show(value)).returncode == 1


def test_check_review_head_truncated_prefix_is_not_accepted_as_ancestor(
    repo: Path, bd: Path
) -> None:
    """The lode-xdg3 hole: a prefix resolves in `merge-base --is-ancestor`."""
    head = _out(repo, "rev-parse", "HEAD")
    assert _check(repo, bd, _show(head[:-1])).returncode == 1


def test_check_review_head_broken_call_is_exit_2(repo: Path, bd: Path) -> None:
    assert _run("check-review-head.sh", repo, bindir=bd).returncode == 2


def test_check_review_head_failed_bd_show_is_exit_4(repo: Path, bd: Path) -> None:
    fake_bd(bd, default="exit 1")
    assert _run("check-review-head.sh", repo, "lode-x", bindir=bd).returncode == 4


def test_check_review_head_unparseable_bd_show_is_exit_4(repo: Path, bd: Path) -> None:
    """jq's own exit 2 on a parse error must not surface as this script's
    exit 2, which means a broken call."""
    assert _check(repo, bd, "warning: not json").returncode == 4


def _add_worktree(repo: Path, path: Path) -> Path:
    _git(repo, "worktree", "add", "-q", "--detach", str(path))
    return path


@pytest.fixture
def launch_worktree(tmp_path: Path, repo: Path) -> tuple[Path, str, str]:
    """A `.claude/worktrees/` worktree of ``repo``, plus the tips of ``main``
    and of a ``land/lode-x`` branch one commit ahead of it."""
    main_tip = _out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "land/lode-x")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "built")
    land_tip = _out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    wt = _add_worktree(repo, tmp_path / ".claude" / "worktrees" / "agent-abc123")
    return wt, main_tip, land_tip


def test_checkout_derives_suffix_from_worktree_dir(
    repo: Path, launch_worktree: tuple[Path, str, str]
) -> None:
    wt, _, land_tip = launch_worktree
    _git(wt, "fetch", "-q", str(repo), "land/lode-x")
    r = _run("checkout-land-branch.sh", wt, "lode-x")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "land/lode-x--agent-abc123"
    assert _out(wt, "rev-parse", "--abbrev-ref", "HEAD") == "land/lode-x--agent-abc123"
    assert _out(wt, "rev-parse", "HEAD") == land_tip


def test_checkout_takes_the_land_tip_when_the_fetch_listed_another_ref_first(
    repo: Path, launch_worktree: tuple[Path, str, str]
) -> None:
    """`FETCH_HEAD` as a rev is the FIRST fetched ref -- here `main`."""
    wt, main_tip, land_tip = launch_worktree
    _git(wt, "fetch", "-q", str(repo), "main", "land/lode-x")
    assert _out(wt, "rev-parse", "FETCH_HEAD") == main_tip
    r = _run("checkout-land-branch.sh", wt, "lode-x")
    assert r.returncode == 0, r.stderr
    assert _out(wt, "rev-parse", "HEAD") == land_tip


@pytest.mark.parametrize("fetched", [None, "main"])
def test_checkout_refuses_when_fetch_head_lacks_the_land_branch(
    repo: Path, launch_worktree: tuple[Path, str, str], fetched: str | None
) -> None:
    """No fetch at all, or a fetch of some other ref: exit 3, HEAD untouched."""
    wt, main_tip, _ = launch_worktree
    if fetched:
        _git(wt, "fetch", "-q", str(repo), fetched)
    r = _run("checkout-land-branch.sh", wt, "lode-x")
    assert r.returncode == 3
    assert "land/lode-x" in r.stderr
    assert _out(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _out(wt, "rev-parse", "HEAD") == main_tip


def test_checkout_refuses_a_non_worktree_cwd(repo: Path) -> None:
    assert _run("checkout-land-branch.sh", repo, "lode-x").returncode == 1


@pytest.mark.parametrize(
    "near_miss_dir",
    [".claude/worktrees-stale/agent-abc123", "x.claude/worktrees/agent-abc123"],
)
def test_checkout_refuses_a_near_miss_directory_name(
    tmp_path: Path, repo: Path, near_miss_dir: str
) -> None:
    """Pins both `/` anchors of the `.claude/worktrees/` segment glob, the
    same pair `tests/test_isolation_guard.py` pins for its copy."""
    wt = _add_worktree(repo, tmp_path / near_miss_dir)
    assert _run("checkout-land-branch.sh", wt, "lode-x").returncode == 1


def test_checkout_broken_call_is_exit_2(repo: Path) -> None:
    assert _run("checkout-land-branch.sh", repo).returncode == 2
