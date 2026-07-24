"""Tests for scripts/release-bump.sh (lode-ns3r).

`/release` Section 2 (`.claude/skills/release/SKILL.md#2-derive-the-proposal`)
used to derive the conventional-commit SemVer bump with an inline shell
snippet -- ungated shell embedded directly in a SKILL.md, exactly the shape
of bug this repo has already shipped once before undetected
(`scripts/merge-precheck.sh`'s docstring, lode-mh9g).

THE BUG THIS EXTRACTION FIXES: the inline snippet read each commit's full
message via `git log RANGE --format='%B%x00'` + `while IFS= read -r -d ''
MSG`, then took `SUBJECT=$(printf '%s' "$MSG" | head -1)`. git inserts a
newline AFTER each record's `%B` expansion, BEFORE the `%x00` delimiter -- so
the NUL-delimited stream is actually "body1\n\x00body2\n\x00...", not
"body1\x00body2\x00...". Every record from the SECOND onward is therefore
captured WITH A LEADING NEWLINE, so `head -1` on it returns an EMPTY first
line, and the subject regexes never match anything but the newest commit in
the range. `test_two_feat_commits_neither_the_newest_yields_feat` below pins
the exact lode-905v repro shape (2 feat commits, neither the newest in the
range) and is the test the bug's fix must not regress: reverting
`scripts/release-bump.sh`'s subject-reading loop back to the old
`%B%x00` + `head -1` approach turns it red (verified by hand while writing
this suite -- BUMP comes back "none" instead of "feat").

All tests below run the ACTUAL `scripts/release-bump.sh` against real git
repositories built in `tmp_path` -- no fake git, no mocked subprocess. That is
what makes them sabotage-provable per the lode-verb bar (see
tests/test_merge_precheck.py for the same house style).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release-bump.sh"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit on `trunk`, isolated user config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("line1\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "chore: base")
    return repo


def _commit(repo: Path, message: str, filename: str | None = None) -> None:
    """One commit with the given (possibly multi-line) message. Each commit
    touches its own file so every commit is non-empty."""
    name = filename or f"f-{len(list(repo.glob('f-*.txt')))}.txt"
    (repo / name).write_text(f"content for {name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def _run(range_: str, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), range_],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_two_feat_commits_neither_the_newest_yields_feat(tmp_path: Path) -> None:
    """The exact lode-905v repro shape: v1.1.0..HEAD contains 2 feat(...)
    commits, neither of which is the newest commit in the range (a plain
    "chore:" commit is newest). The old inline snippet computed BUMP=none
    here because only the newest commit's subject survived the leading-
    newline bug intact."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v1.1.0")
    _commit(repo, "feat(retrieval): add reranker")
    _commit(repo, "feat(cli): add --json flag")
    _commit(repo, "chore: tidy up")  # newest -- carries no recognized prefix

    result = _run("v1.1.0..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "feat\n"
    assert result.stderr == ""


def test_single_fix_commit_yields_fix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "fix(auth): correct token refresh")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "fix\n"


def test_no_recognized_prefix_yields_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "tidy up formatting")
    _commit(repo, "another unrelated commit")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "none\n"


def test_bang_subject_yields_breaking_even_when_not_newest(tmp_path: Path) -> None:
    """A `!:` breaking-change subject must win regardless of position in the
    range -- the same leading-newline bug that broke feat/fix detection would
    also have broken this."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "feat(api)!: drop legacy endpoint")
    _commit(repo, "fix(cli): trivial typo")  # newest

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "breaking\n"


def test_breaking_change_in_body_of_non_newest_commit_is_detected(
    tmp_path: Path,
) -> None:
    """BREAKING CHANGE: in a commit BODY (not just the subject) must be
    detected even when that commit is not the newest in the range -- this is
    the acceptance criterion 'BREAKING-CHANGE-in-body detection still works
    under the chosen fix'."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(
        repo,
        "feat(storage): change chunk format\n\nBREAKING CHANGE: old chunks "
        "must be re-embedded.",
    )
    _commit(repo, "fix(cli): trivial typo")  # newest, no breaking marker

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "breaking\n"


def test_precedence_breaking_over_feat_over_fix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "fix(a): minor fix")
    _commit(repo, "feat(b): a feature")
    _commit(repo, "feat(c)!: a breaking feature")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "breaking\n"


def test_precedence_feat_over_fix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "fix(a): minor fix")
    _commit(repo, "feat(b): a feature")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "feat\n"


def test_unresolvable_range_is_a_machine_fault_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run("totally-bogus-tag-xyz..HEAD", repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr


def test_usage_without_args_is_exit_2() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr
    assert result.stdout == ""


def test_usage_with_two_args_is_also_exit_2() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "a..b", "extra"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr
