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

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _gitrepo import _git
from _hookharness import SETTINGS

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "harness-doctor.sh"


def _required_scripts() -> list[str]:
    """The required-script list as the script itself declares it.

    Derived, not mirrored: a hand-copied list here would need its own
    anti-drift test, and the only drift it could uniquely catch is the
    harmless direction (an extra entry, which is inert in the fake repo).
    """
    parts = SCRIPT.read_text().split('required_scripts="', 1)
    assert len(parts) == 2, 'harness-doctor.sh no longer declares required_scripts="'
    return parts[1].split('"', 1)[0].split()


REQUIRED_SCRIPTS = _required_scripts()
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
    # `git init` only -- no commit. The doctor reads no history, index, or tree
    # (just `git rev-parse --show-toplevel` and `git worktree list`), so building
    # one would be ~30% of this module's runtime for zero coverage.
    _git(repo, "init", "-q", "-b", "trunk")

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

    (repo / ".gitignore").write_text("venv/\n.nox/\n")
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
    shutil.rmtree(repo / ".beads")
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ".beads/ missing" in result.stdout


@pytest.mark.parametrize(
    ("case", "config", "auto_is_false"),
    [
        # beads accepts a dotted single-line key and a nested block; both count.
        ("dotted false", "import.auto: false\n", True),
        ("nested false", "import:\n  auto: false\n", True),
        ("dotted true", "import.auto: true\n", False),
        # A false ALL-CLEAR on THE invariant is worse than no check at all. The
        # nested form must be read BLOCK-SCOPED: asking "is there an `import:`
        # block" and "is there an `auto: false` anywhere" as two independent
        # questions reports ok on this config, whose import.auto is TRUE.
        (
            "nested true, masked by export",
            "import:\n  auto: true\nexport:\n  auto: false\n",
            False,
        ),
        ("commented out", "# import.auto: false\n", False),
    ],
)
def test_import_auto_invariant(
    tmp_path: Path, case: str, config: str, auto_is_false: bool
) -> None:
    repo = _build_healthy_repo(tmp_path)
    (repo / ".beads" / "config.yaml").write_text(f'issue-prefix: "lode"\n{config}')
    result = _run(repo)
    detail = f"{case}: " + result.stdout + result.stderr
    if auto_is_false:
        assert result.returncode == 0, detail
        assert "import.auto is false" in result.stdout, detail
    else:
        assert result.returncode == 1, detail
        assert "does not set import.auto: false" in result.stdout, detail


def test_every_script_the_harness_invokes_by_path_is_checked() -> None:
    """The doctor's list must keep matching its own stated criterion.

    Both declaration sources are derived, never mirrored -- and both are
    declarations of INTENT, not the filesystem, so this stays a real check
    rather than the tautology that globbing `scripts/` would be.

    The hook half is the sharper one: the PreToolUse wrappers are written
    `[ -x "$SCRIPT" ] && bash "$SCRIPT"`, so a deleted guard FAILS OPEN --
    it silently stops guarding and nothing else in the harness notices. The
    agent/skill half is what caught `scripts/release.sh` and its two
    siblings missing from the first draft of the list.
    """
    sources = [SETTINGS, *sorted(REPO_ROOT.glob(".claude/skills/*/SKILL.md"))]
    sources += sorted(REPO_ROOT.glob(".claude/agents/*.md"))
    referenced: set[str] = set()
    for path in sources:
        referenced |= set(re.findall(r"scripts/[A-Za-z0-9._-]+\.sh", path.read_text()))
    missing = sorted(referenced - set(REQUIRED_SCRIPTS))
    assert not missing, (
        f"scripts referenced by the harness but not checked by harness-doctor: "
        f"{missing}. Add them to required_scripts in {SCRIPT.name}, or -- if a "
        f"reference is a mention rather than an invocation -- exempt it here with "
        f"a stated reason."
    )


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
