"""Tests for scripts/trunk-bash-guard.sh and its committed PreToolUse(Bash) wrapper (lode-21sxk).

Extends lode-p8zl's Edit|Write trunk guard to Bash calls that run `git commit`, `git merge`,
`git push` or `nox` on a cwd whose branch is trunk. The maintainer rulings (ask never deny,
gate on the branch, the /land lock exemption and its accepted limit) are recorded in
docs/decisions.md (lode-21sxk) and the script header, not restated here. `land-lock.sh held`
itself is tested in tests/test_land_lock.py.

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
from _gitrepo import _git, os_env
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
    "if ! git diff --quiet; then git commit -m x; fi",
    "for b in a b; do git push origin $b; done",
    "! git push",
    "timeout 600 uv run --frozen nox -t fix",
    "python3.12 -m nox -t fix",
    "uv run --frozen \\\n  nox -t fix",
    "git \\\n  commit -m x",
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
    # Real shapes from the lode-21sxk replay that the quote-aware matcher silences.
    'bd update lode-1 --append-notes "gates: fix green; nox -s unit green; git push later"',
    "bd update lode-1 --notes 'MAINTAINER RULING: run nox -t fix\nthen git commit; git push'",
    'NEW=$(bd create --type=task --title="x" --description="Why.\n\n- git commit then nox -t fix;\n  git push origin trunk\n")',
    'cat > "$S/t0.md" <<\'EOF\'\n## Goal\n\nRecord it; git commit; nox -t fix; git push origin trunk\nEOF\nbd update lode-1 --description "$(cat $S/t0.md)"',
    "cat <<-EOF\n\tgit push\n\tEOF\nls",
    "cat > a <<'A'\ngit push\nA\ncat > b <<'B'\nnox\nB",
    "python3 -I - <<'E'\nimport subprocess\nsubprocess.run(['git', 'commit'])\nE",
    "git status # git push",
    "echo $((1<<2)) # nox",
    "grep -nF 'git push origin trunk' notes.txt",
    'grep -rn "nox -s tests" docs | head -3',
]


def _init_repo(path: Path, *, branch: str) -> Path:
    _git(path.parent, "init", "-q", "-b", branch, str(path))
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def _asks(command: str, cwd: Path, *, home: Path | None = None) -> bool:
    """Whether the script asks about `command` run from `cwd`. It must exit 0 either way, and
    print the ask or nothing."""
    proc = subprocess.run(
        [str(SCRIPT), command],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=os_env({"HOME": str(home)} if home else None),
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return False
    assert '"permissionDecision": "ask"' in proc.stdout
    assert "lode-21sxk" in proc.stdout
    return True


def _live_lock(repo: Path) -> None:
    # A successful acquire deletes land-lock.sh's machine-wide fault counter under $TMPDIR, so
    # point it at a throwaway directory.
    fault_tmp = repo.parent / "fault-tmpdir"
    fault_tmp.mkdir(exist_ok=True)
    proc = subprocess.run(
        ["bash", str(LAND_LOCK), "acquire"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=os_env({"TMPDIR": str(fault_tmp)}),
    )
    assert proc.returncode == 0, proc.stderr


@pytest.fixture(scope="module")
def ro_trunk(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A checkout on trunk shared by the tests that only read it. Never lock it."""
    return _init_repo(tmp_path_factory.mktemp("ro-trunk") / "repo", branch="trunk")


@pytest.fixture(scope="module")
def ro_feature(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A checkout off trunk shared by the tests that only read it."""
    return _init_repo(
        tmp_path_factory.mktemp("ro-feature") / "repo", branch="worktree-agent-abc123"
    )


@pytest.fixture
def trunk_repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "repo", branch="trunk")


# --- script-level -----------------------------------------------------------------------------


@pytest.mark.parametrize("command", ASKING)
def test_script_asks_on_trunk(ro_trunk: Path, command: str) -> None:
    assert _asks(command, ro_trunk)


@pytest.mark.parametrize("command", ASKING)
def test_script_silent_off_trunk(ro_feature: Path, command: str) -> None:
    assert not _asks(command, ro_feature)


@pytest.mark.parametrize("command", [*SILENT, ""])
def test_script_silent_for_unrelated_calls_on_trunk(
    ro_trunk: Path, command: str
) -> None:
    assert not _asks(command, ro_trunk)


def test_script_silent_outside_any_git_repo(tmp_path: Path) -> None:
    assert not _asks("git commit -m x", tmp_path)


@pytest.mark.parametrize(
    "template",
    ["cd {main} && git commit -m x", "cd {main}\ngit commit -m x"],
    ids=["and", "newline"],
)
def test_leading_cd_into_trunk_checkout_asks_from_a_feature_cwd(
    tmp_path: Path, ro_feature: Path, template: str
) -> None:
    main = _init_repo(tmp_path / "main", branch="trunk")
    assert _asks(template.format(main=main), ro_feature)


def test_leading_cd_with_a_tilde_target_is_followed(
    tmp_path: Path, ro_feature: Path
) -> None:
    _init_repo(tmp_path / "main", branch="trunk")
    assert _asks("cd ~/main && git commit -m x", ro_feature, home=tmp_path)


def test_leading_cd_into_feature_checkout_is_silent_from_a_trunk_cwd(
    ro_trunk: Path, ro_feature: Path
) -> None:
    assert not _asks(f'cd "{ro_feature}" && git commit -m x', ro_trunk)


def test_leading_cd_to_an_unresolvable_target_falls_back_to_the_hook_cwd(
    ro_trunk: Path,
) -> None:
    assert _asks('cd "$NOWHERE" && git commit -m x', ro_trunk)


def test_live_land_lock_exempts_trunk(trunk_repo: Path) -> None:
    _live_lock(trunk_repo)
    for command in ("git merge --no-ff land/x", "git push origin trunk", "nox -t fix"):
        assert not _asks(command, trunk_repo), command


def test_stale_land_lock_does_not_exempt(trunk_repo: Path) -> None:
    old = int(time.time()) - 100_000
    (trunk_repo / ".git" / "land.lock").write_text(f"1 h {old} 2020-01-01T00:00:00Z\n")
    assert _asks("git commit -m x", trunk_repo)


def test_live_lock_taken_in_the_main_checkout_exempts_a_linked_worktree_on_trunk(
    tmp_path: Path, trunk_repo: Path
) -> None:
    # The lock lives under the common git dir, so it is visible from any checkout of the repo.
    _git(trunk_repo, "checkout", "-q", "-b", "side")
    wt = tmp_path / "wt"
    _git(trunk_repo, "worktree", "add", "-q", str(wt), "trunk")
    assert _asks("git commit -m x", wt)
    _live_lock(trunk_repo)
    assert not _asks("git commit -m x", wt)


# --- hook-level (through the committed .claude/settings.json wrapper, via /bin/sh) -------------

needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the wrapper shells out to jq"
)


def _hook(command: str, cwd: Path, **kwargs: str) -> dict | None:
    kwargs.setdefault("project_dir", str(REPO_ROOT))
    return run_hook(pretooluse_hook("trunk-bash-guard.sh"), command, cwd=cwd, **kwargs)


@needs_jq
def test_hook_asks_on_trunk(ro_trunk: Path) -> None:
    out = _hook("uv run --frozen nox -t fix", ro_trunk)
    assert out is not None
    assert out["permissionDecision"] == "ask"
    assert "lode-21sxk" in out["permissionDecisionReason"]


@needs_jq
def test_hook_silent_off_trunk(ro_feature: Path) -> None:
    assert _hook("git commit -m x", ro_feature) is None


@needs_jq
def test_hook_silent_for_unrelated_call_on_trunk(ro_trunk: Path) -> None:
    assert _hook("git status", ro_trunk) is None


@needs_jq
def test_hook_falls_through_when_script_missing(tmp_path: Path, ro_trunk: Path) -> None:
    assert _hook("git commit -m x", ro_trunk, project_dir=str(tmp_path)) is None


def test_hook_denies_everything_when_jq_is_missing(ro_trunk: Path) -> None:
    out = _hook("ls", ro_trunk, path="/nonexistent")
    assert out is not None
    assert out["permissionDecision"] == "deny"
    assert "lode-21sxk" in out["permissionDecisionReason"]


def test_wrapper_is_posix_shell_compatible() -> None:
    hook = pretooluse_hook("trunk-bash-guard.sh")
    proc = subprocess.run(
        [SH, "-n", "-c", hook], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr


# Known false asks the quote-aware matcher keeps on purpose (script header, KNOWN LIMITS).
@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'o = \"`uv run nox -t fix`\"'",
        'bd update x --notes "$(echo; nox)"',
    ],
)
def test_script_keeps_the_documented_false_asks(ro_trunk: Path, command: str) -> None:
    assert _asks(command, ro_trunk)


@pytest.mark.parametrize(
    "command",
    [
        "echo '",
        'echo "',
        "cat <<",
        "cat <<'",
        "x <<<",
        "# ",
        "\\",
        "git commit \\",
        "<<EOF\n",
    ],
)
def test_script_exits_zero_on_malformed_shell_text(
    ro_trunk: Path, command: str
) -> None:
    _asks(command, ro_trunk)
