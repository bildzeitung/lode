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

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "commit", "-q", "--allow-empty", "-m", "one")
    return r


@pytest.fixture
def bd_shim(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `bd` on PATH: appends argv to a log, serves BD_SHOW_JSON for `show`,
    and fails every call when BD_FAIL is set."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "bd.log"
    shim = bindir / "bd"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        '[ -n "${BD_FAIL:-}" ] && exit 1\n'
        'if [ "$1" = show ]; then printf "%s" "$BD_SHOW_JSON"; fi\n'
        "exit 0\n"
    )
    shim.chmod(0o755)
    return bindir, log


def _run(
    script: str, cwd: Path, bindir: Path, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", **env}
    return subprocess.run(
        [str(SCRIPTS / script), *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_handoff_writes_the_derived_head_and_passes_extra_args(
    repo: Path, bd_shim: tuple[Path, Path]
) -> None:
    bindir, log = bd_shim
    head = _git(repo, "rev-parse", "HEAD")
    r = _run("handoff-sha.sh", repo, bindir, "lode-x", "land_head", "--add-label", "l")
    assert r.returncode == 0, r.stderr
    assert log.read_text().strip() == (
        f"update lode-x --set-metadata land_head={head} --add-label l"
    )


def test_handoff_broken_call_is_exit_2_and_writes_nothing(
    repo: Path, bd_shim: tuple[Path, Path]
) -> None:
    bindir, log = bd_shim
    assert _run("handoff-sha.sh", repo, bindir, "lode-x").returncode == 2
    assert _run("handoff-sha.sh", repo, bindir, "lode-x", "nope").returncode == 2
    assert not log.exists()


def test_handoff_bd_failure_is_exit_3(repo: Path, bd_shim: tuple[Path, Path]) -> None:
    bindir, _ = bd_shim
    r = _run("handoff-sha.sh", repo, bindir, "lode-x", "review_head", BD_FAIL="1")
    assert r.returncode == 3
    assert "NOT written" in r.stderr


def test_handoff_validates_the_derived_value_before_writing(
    repo: Path, bd_shim: tuple[Path, Path]
) -> None:
    """The validator call is what stands between a malformed derive and the
    write: a `git` that prints a 39-character SHA must reach no `bd update`."""
    bindir, log = bd_shim
    fake_git = bindir / "git"
    fake_git.write_text(f"#!/usr/bin/env bash\necho {'a' * 39}\n")
    fake_git.chmod(0o755)
    r = _run("handoff-sha.sh", repo, bindir, "lode-x", "land_head")
    assert r.returncode == 1
    assert "MALFORMED" in r.stderr
    assert not log.exists()


def test_handoff_outside_a_repo_never_writes(
    tmp_path: Path, bd_shim: tuple[Path, Path]
) -> None:
    bindir, log = bd_shim
    r = _run(
        "handoff-sha.sh",
        tmp_path,
        bindir,
        "lode-x",
        "review_head",
        GIT_CEILING_DIRECTORIES=str(tmp_path.parent),
    )
    assert r.returncode != 0
    assert not log.exists()


def _show(review_head: str | None) -> str:
    meta = {} if review_head is None else {"review_head": review_head}
    return json.dumps([{"metadata": meta}])


def test_check_review_head_forward_only_is_exit_0(
    repo: Path, bd_shim: tuple[Path, Path]
) -> None:
    bindir, _ = bd_shim
    old = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    r = _run("check-review-head.sh", repo, bindir, "lode-x", BD_SHOW_JSON=_show(old))
    assert r.returncode == 0, r.stderr


def test_check_review_head_unreachable_is_exit_3(
    repo: Path, bd_shim: tuple[Path, Path]
) -> None:
    bindir, _ = bd_shim
    old = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--orphan", "other")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "rewritten")
    r = _run("check-review-head.sh", repo, bindir, "lode-x", BD_SHOW_JSON=_show(old))
    assert r.returncode == 3
    assert "UNREACHABLE" in r.stderr


@pytest.mark.parametrize("value", [None, "abc123"])
def test_check_review_head_missing_or_malformed_is_exit_1(
    repo: Path, bd_shim: tuple[Path, Path], value: str | None
) -> None:
    bindir, _ = bd_shim
    r = _run("check-review-head.sh", repo, bindir, "lode-x", BD_SHOW_JSON=_show(value))
    assert r.returncode == 1


def test_check_review_head_truncated_prefix_is_not_accepted_as_ancestor(
    repo: Path, bd_shim: tuple[Path, Path]
) -> None:
    """The lode-xdg3 hole: a prefix resolves in `merge-base --is-ancestor`."""
    bindir, _ = bd_shim
    head = _git(repo, "rev-parse", "HEAD")
    r = _run(
        "check-review-head.sh", repo, bindir, "lode-x", BD_SHOW_JSON=_show(head[:-1])
    )
    assert r.returncode == 1


def test_check_review_head_broken_call_is_exit_2(
    repo: Path, bd_shim: tuple[Path, Path]
) -> None:
    bindir, _ = bd_shim
    assert _run("check-review-head.sh", repo, bindir).returncode == 2


@pytest.mark.parametrize(
    "env", [{"BD_FAIL": "1"}, {"BD_SHOW_JSON": "warning: not json"}]
)
def test_check_review_head_unreadable_bd_show_is_exit_4(
    repo: Path, bd_shim: tuple[Path, Path], env: dict[str, str]
) -> None:
    """A failed or unparseable `bd show` is neither a broken call (2) nor a
    verdict about the field (1, 3) -- jq's own exit 2 must not leak through."""
    bindir, _ = bd_shim
    assert _run("check-review-head.sh", repo, bindir, "lode-x", **env).returncode == 4


@pytest.fixture
def launch_worktree(tmp_path: Path, repo: Path) -> tuple[Path, str, str]:
    """A `.claude/worktrees/` worktree of ``repo``, plus the tips of ``main``
    and of a ``land/lode-x`` branch one commit ahead of it."""
    main_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "land/lode-x")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "built")
    land_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    wt = tmp_path / ".claude" / "worktrees" / "agent-abc123"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    return wt, main_tip, land_tip


def test_checkout_derives_suffix_from_worktree_dir(
    repo: Path, bd_shim: tuple[Path, Path], launch_worktree: tuple[Path, str, str]
) -> None:
    bindir, _ = bd_shim
    wt, _, land_tip = launch_worktree
    _git(wt, "fetch", "-q", str(repo), "land/lode-x")
    r = _run("checkout-land-branch.sh", wt, bindir, "lode-x")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "land/lode-x--agent-abc123"
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "land/lode-x--agent-abc123"
    assert _git(wt, "rev-parse", "HEAD") == land_tip


def test_checkout_takes_the_land_tip_when_the_fetch_listed_another_ref_first(
    repo: Path, bd_shim: tuple[Path, Path], launch_worktree: tuple[Path, str, str]
) -> None:
    """`FETCH_HEAD` as a rev is the FIRST fetched ref -- here `main`."""
    bindir, _ = bd_shim
    wt, main_tip, land_tip = launch_worktree
    _git(wt, "fetch", "-q", str(repo), "main", "land/lode-x")
    assert _git(wt, "rev-parse", "FETCH_HEAD") == main_tip
    r = _run("checkout-land-branch.sh", wt, bindir, "lode-x")
    assert r.returncode == 0, r.stderr
    assert _git(wt, "rev-parse", "HEAD") == land_tip


@pytest.mark.parametrize("fetched", [None, "main"])
def test_checkout_refuses_when_fetch_head_lacks_the_land_branch(
    repo: Path,
    bd_shim: tuple[Path, Path],
    launch_worktree: tuple[Path, str, str],
    fetched: str | None,
) -> None:
    """No fetch at all, or a fetch of some other ref: exit 3, HEAD untouched."""
    bindir, _ = bd_shim
    wt, main_tip, _ = launch_worktree
    if fetched:
        _git(wt, "fetch", "-q", str(repo), fetched)
    r = _run("checkout-land-branch.sh", wt, bindir, "lode-x")
    assert r.returncode == 3
    assert "land/lode-x" in r.stderr
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _git(wt, "rev-parse", "HEAD") == main_tip


def test_checkout_refuses_a_non_worktree_cwd(
    repo: Path, bd_shim: tuple[Path, Path]
) -> None:
    bindir, _ = bd_shim
    assert _run("checkout-land-branch.sh", repo, bindir, "lode-x").returncode == 1


def test_checkout_broken_call_is_exit_2(repo: Path, bd_shim: tuple[Path, Path]) -> None:
    bindir, _ = bd_shim
    assert _run("checkout-land-branch.sh", repo, bindir).returncode == 2
