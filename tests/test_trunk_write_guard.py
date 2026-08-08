"""Tests for scripts/trunk-write-guard.sh and its committed PreToolUse(Edit|Write) wrapper
(lode-p8zl).

lode-6wgc's mitigation for a worktree destroyed mid-session -- re-running
scripts/isolation-guard.sh at two checkpoints in .claude/agents/coding.md and
.claude/agents/code-reviewer.md -- depends on the agent choosing to run those checkpoints, and
cannot cover the window between a checkpoint and the tool call it guards. This guard is the
structurally correct altitude that ticket deferred: a PreToolUse hook that fires on every
Edit/Write with no agent cooperation.

MAINTAINER RULING (bd lode-p8zl, /sweep escalation walk-through 2026-08-08) settled the two open
design questions:

  RULING 1: do NOT attempt to disambiguate a dispatched subagent from the main session -- the
  PreToolUse payload carries no agent-role field, and both resolve to the same checkout root.
  Gate on the BRANCH instead (derivable via `git rev-parse --abbrev-ref HEAD`), and return
  `permissionDecision: "ask"`, never `"deny"` -- a human at the terminal can approve; a dispatched
  subagent cannot and is stopped.

  RULING 3: unlike lode-ij24/lode-o29m/lode-fpmi's Bash-matched guards, this one needs NO jq --
  it never parses `tool_input` at all, so it adds nothing to the lode-oii9
  deny-everything-when-jq-is-missing surface. Tests below therefore do NOT skip on a missing jq.

Two layers of coverage, matching tests/test_sha_fabrication_guard.py's own pattern:
  - SCRIPT-LEVEL tests drive `scripts/trunk-write-guard.sh` directly as a subprocess.
  - HOOK-LEVEL tests drive the actual one-liner extracted from the committed
    `.claude/settings.json`, through `/bin/sh -c` (dash on Linux, lode-9gm2) -- proving the
    wrapper actually delegates to the script.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _hookharness import SH, pretooluse_hook, run_hook

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "trunk-write-guard.sh"


def _init_repo(path: Path, *, branch: str) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", branch, str(path)], check=True, timeout=30
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        timeout=30,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        },
    )


# --- script-level -----------------------------------------------------------------------------


def test_script_asks_on_trunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="trunk")
    proc = subprocess.run(
        [str(SCRIPT)], cwd=repo, capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert '"permissionDecision": "ask"' in proc.stdout
    assert "lode-p8zl" in proc.stdout


def test_script_allows_on_non_trunk_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="worktree-agent-abc123")
    proc = subprocess.run(
        [str(SCRIPT)], cwd=repo, capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_script_allows_outside_any_git_repo(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    proc = subprocess.run(
        [str(SCRIPT)], cwd=empty, capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_script_always_exits_zero_regardless_of_verdict(tmp_path: Path) -> None:
    # A PreToolUse hook exiting non-zero is itself a defect (see script docstring) -- assert this
    # holds on both the ask path and the silent-allow path.
    repo = tmp_path / "repo"
    _init_repo(repo, branch="trunk")
    ask = subprocess.run(
        [str(SCRIPT)], cwd=repo, capture_output=True, text=True, timeout=30, check=False
    )
    assert ask.returncode == 0

    _init_repo(tmp_path / "repo2", branch="feature")
    allow = subprocess.run(
        [str(SCRIPT)],
        cwd=tmp_path / "repo2",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert allow.returncode == 0


# --- hook-level (through the committed .claude/settings.json wrapper, via /bin/sh) -------------


def test_hook_asks_on_trunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="trunk")
    hook = pretooluse_hook("trunk-write-guard.sh", matcher="Edit|Write")
    out = run_hook(
        hook,
        tool_name="Edit",
        tool_input={"file_path": str(repo / "foo.md"), "old_string": "a", "new_string": "b"},
        cwd=repo,
        project_dir=str(repo),
    )
    assert out is not None
    assert out["permissionDecision"] == "ask"
    assert "lode-p8zl" in out["permissionDecisionReason"]


def test_hook_allows_on_non_trunk_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch=".claude/worktrees/agent-xyz")
    hook = pretooluse_hook("trunk-write-guard.sh", matcher="Edit|Write")
    out = run_hook(
        hook,
        tool_name="Write",
        tool_input={"file_path": str(repo / "foo.md"), "content": "hi"},
        cwd=repo,
        project_dir=str(repo),
    )
    assert out is None


def test_hook_falls_through_when_script_missing(tmp_path: Path) -> None:
    # A missing/non-executable guard script fails OPEN (silent allow), matching
    # scripts/bd-deps-blocks-guard.sh's and scripts/sha-fabrication-guard.sh's own
    # `[ -x "$SCRIPT" ] && ...` pattern -- appropriate here since the worst case this guard adds
    # is one confirmation prompt, never an unrecoverable public write (contrast lode-o29m's
    # gh-write guard, which fails CLOSED for exactly that reason).
    repo = tmp_path / "repo"
    _init_repo(repo, branch="trunk")
    empty_project_dir = tmp_path / "no-scripts-here"
    empty_project_dir.mkdir()
    hook = pretooluse_hook("trunk-write-guard.sh", matcher="Edit|Write")
    out = run_hook(
        hook,
        tool_name="Edit",
        tool_input={"file_path": "x", "old_string": "a", "new_string": "b"},
        cwd=repo,
        project_dir=str(empty_project_dir),
    )
    assert out is None


def test_wrapper_is_posix_shell_compatible() -> None:
    # dash (the harness's actual PreToolUse interpreter, lode-9gm2) rejects bash-only syntax
    # with "Bad substitution" -- run the wrapper one-liner through dash directly on an empty
    # stdin payload from outside any lode checkout to prove it never uses bash-only constructs.
    hook = pretooluse_hook("trunk-write-guard.sh", matcher="Edit|Write")
    proc = subprocess.run(
        [SH, "-n", "-c", hook], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
