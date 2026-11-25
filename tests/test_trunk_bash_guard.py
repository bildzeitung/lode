"""Tests for scripts/trunk-bash-guard.sh and its committed PreToolUse(Bash) wrapper (lode-21sxk).

Extends lode-p8zl's Edit|Write trunk guard to Bash calls that run `git commit`, `git merge`,
`git push` or `nox` on a cwd whose branch is trunk. The maintainer rulings (ask never deny,
gate on the branch, the /land lock exemption and its accepted limit) are recorded in
docs/decisions.md (lode-8uh71) and the script header, not restated here.

Two layers, like tests/test_trunk_write_guard.py: SCRIPT-LEVEL tests drive the script directly;
HOOK-LEVEL tests drive the one-liner from the committed `.claude/settings.json` through
`/bin/sh -c`.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from _gitrepo import _git
from _hookharness import SH, pretooluse_hook, run_hook

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "trunk-bash-guard.sh"
LAND_LOCK = REPO_ROOT / "scripts" / "land-lock.sh"

ASKING = [
    "git commit -m x",
    "git commit --no-verify -m x",
    "git merge --no-ff land/lode-abc",
    "git push origin trunk",
    "git -C /some/where push",
    "git -c user.name=x commit -m x",
    "nox -t fix",
    "uv run --frozen nox -t fix",
    "uv run --frozen nox -s unit",
    ".venv/bin/nox -s tests",
    "python -m nox -t fix",
    "git add a && git commit -m x",
    "git status; git push",
    "echo hi | git commit -F -",
    "FOO=1 git commit -m x",
    "cat x || nox -t fix",
    "(git push)",
    "x=$(git merge foo)",
    "bash -c 'git commit -m x'",
    "git add a &&\ngit commit -m x",
]

SILENT = [
    "git status",
    "git log --oneline",
    "git diff",
    "git add a",
    "git fetch origin",
    "ls -la",
    "bd update lode-1 --notes 'git commit later'",
    "echo git push",
    "uv run --frozen python scripts/x.py",
    "uv run --frozen pytest -m 'not slow'",
    "scripts/handoff-sha.sh lode-1 review_head",
]


def _init_repo(path: Path, *, branch: str) -> None:
    _git(path.parent, "init", "-q", "-b", branch, str(path))
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")


def _run(command: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), command],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _live_lock(repo: Path) -> None:
    proc = subprocess.run(
        ["bash", str(LAND_LOCK), "acquire"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.fixture
def trunk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="trunk")
    return repo


@pytest.fixture
def feature_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="worktree-agent-abc123")
    return repo


# --- script-level -----------------------------------------------------------------------------


@pytest.mark.parametrize("command", ASKING)
def test_script_asks_on_trunk(trunk_repo: Path, command: str) -> None:
    proc = _run(command, trunk_repo)
    assert proc.returncode == 0, proc.stderr
    assert '"permissionDecision": "ask"' in proc.stdout
    assert "lode-21sxk" in proc.stdout


@pytest.mark.parametrize("command", ASKING)
def test_script_silent_off_trunk(feature_repo: Path, command: str) -> None:
    proc = _run(command, feature_repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize("command", SILENT)
def test_script_silent_for_unrelated_calls_on_trunk(trunk_repo: Path, command: str) -> None:
    proc = _run(command, trunk_repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_script_silent_outside_any_git_repo(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    proc = _run("git commit -m x", empty)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_script_silent_on_empty_command(trunk_repo: Path) -> None:
    proc = _run("", trunk_repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_leading_cd_into_trunk_checkout_asks_from_a_feature_cwd(
    tmp_path: Path, feature_repo: Path
) -> None:
    main = tmp_path / "main"
    _init_repo(main, branch="trunk")
    proc = _run(f"cd {main} && git commit -m x", feature_repo)
    assert '"permissionDecision": "ask"' in proc.stdout


def test_leading_cd_into_feature_checkout_is_silent_from_a_trunk_cwd(
    tmp_path: Path, trunk_repo: Path
) -> None:
    wt = tmp_path / "wt"
    _init_repo(wt, branch="worktree-agent-xyz")
    proc = _run(f'cd "{wt}" && git commit -m x', trunk_repo)
    assert proc.stdout.strip() == ""


def test_live_land_lock_exempts_trunk(trunk_repo: Path) -> None:
    _live_lock(trunk_repo)
    for command in ("git merge --no-ff land/x", "git push origin trunk", "nox -t fix"):
        proc = _run(command, trunk_repo)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", command


def test_stale_land_lock_does_not_exempt(trunk_repo: Path) -> None:
    old = int(time.time()) - 100_000
    (trunk_repo / ".git" / "land.lock").write_text(f"1 h {old} 2020-01-01T00:00:00Z\n")
    proc = _run("git commit -m x", trunk_repo)
    assert '"permissionDecision": "ask"' in proc.stdout


def test_live_lock_exempts_from_a_linked_worktree_that_is_on_trunk_only(
    tmp_path: Path, trunk_repo: Path
) -> None:
    # The lock lives under the common git dir, so it is visible from any checkout of the repo.
    _live_lock(trunk_repo)
    _git(trunk_repo, "branch", "side")
    wt = tmp_path / "wt"
    _git(trunk_repo, "worktree", "add", "-q", str(wt), "side")
    proc = _run("git commit -m x", wt)
    assert proc.stdout.strip() == ""


# --- land-lock.sh held ------------------------------------------------------------------------


def _held(repo: Path) -> int:
    return subprocess.run(
        ["bash", str(LAND_LOCK), "held"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).returncode


def test_held_is_1_with_no_lock(trunk_repo: Path) -> None:
    assert _held(trunk_repo) == 1


def test_held_is_0_for_a_live_lock_and_does_not_modify_it(trunk_repo: Path) -> None:
    _live_lock(trunk_repo)
    lock = trunk_repo / ".git" / "land.lock"
    before = lock.read_text()
    assert _held(trunk_repo) == 0
    assert lock.read_text() == before


def test_held_is_1_for_a_stale_lock_and_leaves_it_in_place(trunk_repo: Path) -> None:
    old = int(time.time()) - 100_000
    lock = trunk_repo / ".git" / "land.lock"
    lock.write_text(f"1 h {old} 2020-01-01T00:00:00Z\n")
    assert _held(trunk_repo) == 1
    assert lock.exists()


def test_held_treats_a_malformed_lock_as_held(trunk_repo: Path) -> None:
    (trunk_repo / ".git" / "land.lock").write_text("garbage\n")
    assert _held(trunk_repo) == 0


def test_held_outside_a_repo_is_1(tmp_path: Path) -> None:
    assert _held(tmp_path) == 1


# --- hook-level (through the committed .claude/settings.json wrapper, via /bin/sh) -------------

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="the wrapper shells out to jq")


@needs_jq
def test_hook_asks_on_trunk(trunk_repo: Path) -> None:
    hook = pretooluse_hook("trunk-bash-guard.sh")
    out = run_hook(hook, "uv run --frozen nox -t fix", cwd=trunk_repo, project_dir=str(REPO_ROOT))
    assert out is not None
    assert out["permissionDecision"] == "ask"
    assert "lode-21sxk" in out["permissionDecisionReason"]


@needs_jq
def test_hook_silent_off_trunk(feature_repo: Path) -> None:
    hook = pretooluse_hook("trunk-bash-guard.sh")
    out = run_hook(hook, "git commit -m x", cwd=feature_repo, project_dir=str(REPO_ROOT))
    assert out is None


@needs_jq
def test_hook_silent_for_unrelated_call_on_trunk(trunk_repo: Path) -> None:
    hook = pretooluse_hook("trunk-bash-guard.sh")
    out = run_hook(hook, "git status", cwd=trunk_repo, project_dir=str(REPO_ROOT))
    assert out is None


@needs_jq
def test_hook_falls_through_when_script_missing(tmp_path: Path, trunk_repo: Path) -> None:
    empty = tmp_path / "no-scripts-here"
    empty.mkdir()
    hook = pretooluse_hook("trunk-bash-guard.sh")
    out = run_hook(hook, "git commit -m x", cwd=trunk_repo, project_dir=str(empty))
    assert out is None


def test_hook_denies_everything_when_jq_is_missing(tmp_path: Path, trunk_repo: Path) -> None:
    hook = pretooluse_hook("trunk-bash-guard.sh")
    nojq = tmp_path / "bin"
    nojq.mkdir()
    for tool in ("git", "bash", "cat", "tr", "dirname", "sh"):
        found = shutil.which(tool)
        if found:
            (nojq / tool).symlink_to(found)
    proc = subprocess.run(
        [SH, "-c", hook],
        input='{"tool_input": {"command": "ls"}}',
        cwd=trunk_repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"PATH": str(nojq), "CLAUDE_PROJECT_DIR": str(REPO_ROOT)},
    )
    assert proc.returncode == 0
    assert '"permissionDecision":"deny"' in proc.stdout.replace(" ", "")


def test_wrapper_is_posix_shell_compatible() -> None:
    hook = pretooluse_hook("trunk-bash-guard.sh")
    proc = subprocess.run(
        [SH, "-n", "-c", hook], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
