"""Tests for scripts/harness-doctor.sh (lode-s9xe.14).

Ported from harness-export's template/scripts/harness-doctor.sh (tracked
there since before the epic's seed commit -- an adoption of a harness-export
invention, not a backport of lode's own prior work; owner confirmed in scope
via /challenge 2026-08-13, see lode-s9xe's description).

Read-only preflight check: inspects the repo for the things the pipeline
assumes -- prerequisites on PATH, guard scripts present+executable, agents
and skills present, `.claude/settings.json` shape, the beads `import.auto:
false` invariant, gate tests, and the worktree directory/`.gitignore`
coverage. It never mutates anything.

All tests run the ACTUAL `scripts/harness-doctor.sh` against a minimal fake
repo built in `tmp_path` with just enough of the required layout -- no
mocked subprocess -- sabotage-provable per the lode-verb bar: reverting any
one check in the script would turn the corresponding test here red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "harness-doctor.sh"

REQUIRED_SCRIPTS = [
    "scripts/isolation-guard.sh",
    "scripts/recycled-worktree-guard.sh",
    "scripts/assert-main-checkout.sh",
    "scripts/land-lock.sh",
    "scripts/land-merge-one.sh",
    "scripts/land-state-load.sh",
    "scripts/merge-precheck.sh",
    "scripts/validate-sha40.sh",
    "scripts/worktree-gc-classify.sh",
    "scripts/worktree-lock-stale.sh",
    "scripts/blocks-dependents.sh",
    "scripts/epic-children-closed.sh",
    "scripts/epic-completion-check.sh",
    "scripts/epic-debate-gate.sh",
    "scripts/sweep-digest-id.sh",
    "scripts/code-concurrency-cap.sh",
    "scripts/bd-dolt-push.sh",
    "scripts/python-init.sh",
]
REQUIRED_AGENTS = ["coding", "code-reviewer", "land-review"]
REQUIRED_SKILLS = ["code", "land", "challenge", "epic-audit", "sweep", "release"]


def _run(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _build_healthy_repo(tmp_path: Path) -> Path:
    """A minimal repo that passes every REQUIRED check (warnings are fine)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    for rel in REQUIRED_SCRIPTS:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/usr/bin/env bash\ntrue\n")
        p.chmod(0o755)

    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for a in REQUIRED_AGENTS:
        (agents_dir / f"{a}.md").write_text("stub\n")

    for s in REQUIRED_SKILLS:
        skill_dir = repo / ".claude" / "skills" / s
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("stub\n")

    (repo / ".claude" / "settings.json").write_text(
        '{"worktree": {"baseRef": "fresh"}, "hooks": {"PreToolUse": []}}\n'
    )

    beads_dir = repo / ".beads"
    beads_dir.mkdir(parents=True, exist_ok=True)
    (beads_dir / "config.yaml").write_text('issue-prefix: "lode"\nimport.auto: false\n')

    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_stub.py").write_text("def test_x():\n    assert True\n")

    gitignore = repo / ".gitignore"
    gitignore.write_text("venv/\n.nox/\n")

    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_not_a_git_repo_exits_2(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    result = _run(not_a_repo)
    assert result.returncode == 2, result.stdout + result.stderr


def test_healthy_repo_exits_0(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    assert "harness-doctor: all required checks passed" in result.stdout


def test_missing_guard_script_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / "scripts" / "land-lock.sh").unlink()
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "scripts/land-lock.sh missing" in result.stdout


def test_non_executable_guard_script_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / "scripts" / "land-lock.sh").chmod(0o644)
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT executable" in result.stdout


def test_missing_agent_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".claude/agents/coding.md").unlink()
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ".claude/agents/coding.md missing" in result.stdout


def test_missing_skill_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".claude/skills/land/SKILL.md").unlink()
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ".claude/skills/land/SKILL.md missing" in result.stdout


def test_missing_settings_json_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".claude" / "settings.json").unlink()
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ".claude/settings.json missing" in result.stdout


def test_invalid_settings_json_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text("{not valid json")
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not valid JSON" in result.stdout


def test_non_fresh_baseref_warns_not_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text(
        '{"worktree": {"baseRef": "head"}, "hooks": {"PreToolUse": []}}\n'
    )
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "warn" in result.stdout
    assert "worktree.baseRef = 'head'" in result.stdout


def test_no_hooks_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text(
        '{"worktree": {"baseRef": "fresh"}}\n'
    )
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "no hooks configured" in result.stdout


def test_missing_beads_dir_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    import shutil

    shutil.rmtree(repo / ".beads")
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ".beads/ missing" in result.stdout


def test_import_auto_true_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".beads" / "config.yaml").write_text(
        'issue-prefix: "lode"\nimport.auto: true\n'
    )
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "does not set import.auto: false" in result.stdout


def test_import_auto_false_nested_form_passes(tmp_path: Path) -> None:
    """beads also accepts a nested block form -- both must be recognized."""
    repo = _build_healthy_repo(tmp_path)
    (repo / ".beads" / "config.yaml").write_text(
        'issue-prefix: "lode"\nimport:\n  auto: false\n'
    )
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "import.auto is false" in result.stdout


def test_issue_prefix_with_double_hyphen_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".beads" / "config.yaml").write_text(
        'issue-prefix: "lo--de"\nimport.auto: false\n'
    )
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ref backstop would force-delete" in result.stdout


def test_no_gate_tests_warns_not_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / "tests" / "test_stub.py").unlink()
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no gate tests" in result.stdout


def test_missing_worktrees_dir_warns_not_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "does not exist yet" in result.stdout


def test_gitignore_missing_venv_warns_not_fails(tmp_path: Path) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".gitignore").write_text("*.log\n")
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "may not ignore venv" in result.stdout
