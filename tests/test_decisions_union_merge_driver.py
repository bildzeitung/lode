"""Proves the union merge driver for docs/decisions.md is actually IN EFFECT
(lode-4jtc.1), not merely declared in .gitattributes.

Every branch that records a decision appends at EOF, so any two concurrent
branches conflict there BY CONSTRUCTION, independent of content -- measured
in lode-4jtc.1's own filing: 35% of all merges touching the file conflicted
on it, tripling to 48% in early August. The fix is a committed root
.gitattributes line, 'docs/decisions.md merge=union' -- 'union' is one of
git's built-in merge drivers, so no per-machine merge.union.driver entry in
.git/config is needed anywhere; the committed .gitattributes is sufficient on
its own.

A .gitattributes line that silently does nothing is the realistic failure
mode (a typo in the attribute name, a path that doesn't match, a driver name
git doesn't recognize) -- so this module does NOT merely grep the file for
the string. It builds two REAL, diverging git commits that each append a
different new entry to docs/decisions.md, actually runs `git merge`, and
asserts the merge produces a clean commit (no conflict markers, no manual
resolution needed) containing BOTH appended entries. That is the only way to
prove the driver actually fired instead of falling back to the default
3-way text merge, which WOULD conflict here (both sides insert at the same
anchor -- end of file -- with no shared context line between them).

Uses the actual repo-root .gitattributes (not a synthesized one) by cloning
this repository's committed tree into tmp_path, so the test fails if the
attribute is ever removed, renamed, or the driver name is mistyped -- the
same house style as tests/test_decisions_no_silent_rewrite_guard.py: a real
throwaway git repo in tmp_path, no fake git, no mocked subprocess.
"""

from __future__ import annotations

from pathlib import Path

from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_TEXT = (
    "# Decisions\n\n"
    "- **Entry one.** Some settled fact, decided a while ago.\n"
    "- **Entry two.** Another fact, also settled.\n"
)


def _init_repo(tmp_path: Path, decisions_text: str) -> Path:
    """A throwaway repo carrying the REAL .gitattributes from this repo's
    root, plus a synthetic docs/decisions.md -- the attribute under test
    must come from the actual tracked file, not a copy re-typed here, or a
    typo in the real file would go undetected."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    gitattributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    (repo / ".gitattributes").write_text(gitattributes, encoding="utf-8")

    (repo / "docs").mkdir()
    (repo / "docs" / "decisions.md").write_text(decisions_text, encoding="utf-8")
    _git(repo, "add", ".gitattributes", "docs/decisions.md")
    _git(repo, "commit", "-q", "-m", "base decisions.md + .gitattributes")
    return repo


def _write_and_commit(repo: Path, decisions_text: str, message: str) -> None:
    (repo / "docs" / "decisions.md").write_text(decisions_text, encoding="utf-8")
    _git(repo, "add", "docs/decisions.md")
    _git(repo, "commit", "-q", "-m", message)


def test_union_driver_declared_in_gitattributes() -> None:
    """Cheap precondition, not the proof itself: the exact attribute this
    ticket's acceptance criteria specify must be present, with no
    per-machine .git/config counterpart required (git recognizes 'union'
    natively)."""
    text = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "docs/decisions.md merge=union" in text, (
        ".gitattributes no longer declares the union merge driver for "
        "docs/decisions.md (lode-4jtc.1)"
    )


def test_union_driver_merges_two_divergent_appends_without_conflict(
    tmp_path: Path,
) -> None:
    """The real proof: two branches each append a DIFFERENT new entry at
    EOF -- exactly the append-at-EOF collision lode-4jtc.1 measured -- and a
    plain `git merge` (no manual resolution) must succeed, with both
    appended entries present and no conflict markers left behind.

    Sabotage check: without the .gitattributes line (or with a typo in it),
    this exact scenario conflicts under git's default 3-way merge, since
    both sides insert at the same anchor with no shared trailing context
    line. Deleting the .gitattributes line from the repo built here turns
    this test red -- proving the assertion actually depends on the driver
    firing, not on some other property of the fixture.
    """
    repo = _init_repo(tmp_path, BASE_TEXT)

    _git(repo, "checkout", "-q", "-b", "ours")
    ours_text = BASE_TEXT + "- **Entry three (ours).** Appended on the ours branch.\n"
    _write_and_commit(repo, ours_text, "ours: append entry three")

    _git(repo, "checkout", "-q", "-b", "theirs", "trunk")
    theirs_text = (
        BASE_TEXT + "- **Entry four (theirs).** Appended on the theirs branch.\n"
    )
    _write_and_commit(repo, theirs_text, "theirs: append entry four")

    _git(repo, "checkout", "-q", "ours")
    merge_result = _git(repo, "merge", "--no-edit", "theirs")

    assert merge_result.returncode == 0, (
        "the union merge driver did not resolve two divergent EOF appends "
        "cleanly -- either .gitattributes no longer declares it or it is "
        "not taking effect:\n" + merge_result.stdout + merge_result.stderr
    )

    merged_text = (repo / "docs" / "decisions.md").read_text(encoding="utf-8")

    assert "Entry three (ours)" in merged_text
    assert "Entry four (theirs)" in merged_text
    assert "Entry one" in merged_text
    assert "Entry two" in merged_text
    assert "<<<<<<<" not in merged_text
    assert "=======" not in merged_text
    assert ">>>>>>>" not in merged_text

    # No lingering unmerged/conflicted paths -- a real, clean merge commit.
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""


def test_union_driver_still_permits_check_decisions_no_silent_rewrite_pass(
    tmp_path: Path,
) -> None:
    """Cross-check with scripts/check-decisions-no-silent-rewrite.sh
    (lode-rl6s): a union-merged result of two divergent appends must still
    pass that gate, since no pre-existing non-blank line disappeared -- a
    union merge is structurally incapable of removing a line. Verified here
    against a REAL merge rather than re-derived from the script's own
    contract."""
    import subprocess

    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "ours")
    _write_and_commit(
        repo,
        BASE_TEXT + "- **Entry three (ours).** Appended on the ours branch.\n",
        "ours: append entry three",
    )

    _git(repo, "checkout", "-q", "-b", "theirs", "trunk")
    _write_and_commit(
        repo,
        BASE_TEXT + "- **Entry four (theirs).** Appended on the theirs branch.\n",
        "theirs: append entry four",
    )

    _git(repo, "checkout", "-q", "ours")
    _git(repo, "merge", "--no-edit", "theirs")

    script = REPO_ROOT / "scripts" / "check-decisions-no-silent-rewrite.sh"
    result = subprocess.run(
        ["bash", str(script), base],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
