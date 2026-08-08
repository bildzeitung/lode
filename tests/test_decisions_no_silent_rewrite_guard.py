"""Tests for scripts/check-decisions-no-silent-rewrite.sh (lode-rl6s).

tests/test_decisions_supersession_markers.py's KNOWN LIMITATION (lode-nlk6):
none of its scans can detect a SILENT IN-PLACE REWRITE of an existing
docs/decisions.md entry -- every check there keys on an artifact a
*marker* leaves behind, and a silent rewrite is the ABSENCE of a marker.
lode-hg49 confirmed this actually bites: a branch reworded an existing
entry's text in place while also using an off-pattern lead-in for its
correction, and only a reviewer's hand-restoration caught the rewrite --
nothing mechanical did.

This script closes that gap with git's own diff instead of a marker-shape
scan: between two refs, did any PRE-EXISTING, non-blank line of
docs/decisions.md disappear? That is exactly what "silently rewritten in
place" means for a dated, append-only log (docs/decisions.md's own
preamble: a correction is "a new entry, or a marker appended to the
existing one, never a silent rewrite").

SCOPE is base...head, not full repository history, and the exit-2 arm comes
from the shared gate_could_not_run (scripts/gate-lib.sh, lode-9i2p) -- both
are the script's own contract; see its header and docs/decisions.md
(search "lode-rl6s") rather than a third retelling here.

All tests below run the ACTUAL script against real throwaway git repos built
in `tmp_path` -- no fake git, no mocked subprocess -- per the lode-verb
sabotage-provable bar (see tests/test_merge_precheck.py for the same house
style). `_git` is the shared helper (lode-863q); `_init_repo` stays local
per that module's own guidance, since this fixture's shape (a single
docs/decisions.md file, no origin remote, no worktrees) is not shared with
any other module.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-decisions-no-silent-rewrite.sh"


def _init_repo(tmp_path: Path, decisions_text: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    (repo / "docs").mkdir()
    (repo / "docs" / "decisions.md").write_text(decisions_text, encoding="utf-8")
    _git(repo, "add", "docs/decisions.md")
    _git(repo, "commit", "-q", "-m", "base decisions.md")
    return repo


def _write_and_commit(repo: Path, decisions_text: str, message: str) -> None:
    (repo / "docs" / "decisions.md").write_text(decisions_text, encoding="utf-8")
    _git(repo, "add", "docs/decisions.md")
    _git(repo, "commit", "-q", "-m", message)


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


BASE_TEXT = (
    "# Decisions\n\n"
    "- **Entry one.** Some settled fact, decided a while ago.\n"
    "- **Entry two.** Another fact, also settled.\n"
)


def test_ordinary_append_is_allowed(tmp_path: Path) -> None:
    """Adding a brand-new entry at the end -- no pre-existing line touched --
    must pass."""
    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _write_and_commit(
        repo,
        BASE_TEXT + "- **Entry three.** A new decision, appended.\n",
        "append entry three",
    )

    result = _run(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_correctly_marked_update_appended_inside_an_entry_is_allowed(
    tmp_path: Path,
) -> None:
    """The sanctioned correction shape: an existing entry gains a NEW,
    appended '**Update (' line -- its own prior lines are untouched -- must
    pass."""
    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    updated = (
        "# Decisions\n\n"
        "- **Entry one.** Some settled fact, decided a while ago.\n"
        "  **Update (lode-zzzz):** narrowed to only apply on Tuesdays.\n"
        "- **Entry two.** Another fact, also settled.\n"
    )
    _write_and_commit(repo, updated, "append Update marker to entry one")

    result = _run(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_silent_in_place_rewrite_is_denied(tmp_path: Path) -> None:
    """The exact lode-hg49 shape: an existing entry's committed text is
    REWORDED in place, with no appended marker at all. This is the
    sabotage-test proof the scan actually fires."""
    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rewritten = (
        "# Decisions\n\n"
        "- **Entry one.** A DIFFERENTLY WORDED fact, silently reworded.\n"
        "- **Entry two.** Another fact, also settled.\n"
    )
    _write_and_commit(repo, rewritten, "silently reword entry one")

    result = _run(repo, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "REMOVED:" in result.stdout
    assert "Entry one" in result.stdout


def test_silent_deletion_of_an_entire_entry_is_denied(tmp_path: Path) -> None:
    """Deleting a previously-committed entry outright is a rewrite too --
    same non-vacuity proof, different shape."""
    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    deleted = (
        "# Decisions\n\n- **Entry one.** Some settled fact, decided a while ago.\n"
    )
    _write_and_commit(repo, deleted, "delete entry two")

    result = _run(repo, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Entry two" in result.stdout


def test_no_false_positive_on_a_pure_blank_line_removal(tmp_path: Path) -> None:
    """Removing only a blank line (no content lost) must not flag -- the
    guard keys on non-blank content, not line count."""
    text_with_blank = BASE_TEXT + "\n"
    repo = _init_repo(tmp_path, text_with_blank)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _write_and_commit(repo, BASE_TEXT, "trim a trailing blank line")

    result = _run(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_change_to_decisions_md_is_allowed(tmp_path: Path) -> None:
    """base == head, or a commit that never touches docs/decisions.md at
    all: nothing to scan, must pass."""
    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "unrelated.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-q", "-m", "unrelated change")

    result = _run(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_head_ref_defaults_to_head(tmp_path: Path) -> None:
    """Calling with only <base-ref> checks base..HEAD."""
    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rewritten = "# Decisions\n\n- **Entry one.** REWORDED.\n- **Entry two.** Another fact, also settled.\n"
    _write_and_commit(repo, rewritten, "reword entry one")

    result = _run(repo, base)  # no explicit head ref
    assert result.returncode == 1, result.stdout + result.stderr


def test_missing_base_ref_argument_is_a_usage_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, BASE_TEXT)
    result = _run(repo)
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_unknown_base_ref_is_a_machine_fault_not_a_verdict(tmp_path: Path) -> None:
    """lode-9i2p's rule (already honoured by validate-mermaid.sh,
    merge-precheck.sh): an unresolvable ref is a MACHINE fault (exit 2), not
    a silent "no rewrite found" (exit 0) nor a false "rewrite found"
    (exit 1)."""
    repo = _init_repo(tmp_path, BASE_TEXT)
    result = _run(repo, "not-a-real-ref")
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""


def test_unknown_head_ref_is_also_a_machine_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = _run(repo, base, "not-a-real-ref")
    assert result.returncode == 2, result.stdout + result.stderr


def test_not_a_git_repository_is_a_machine_fault(tmp_path: Path) -> None:
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    result = _run(non_repo, "HEAD~1")
    assert result.returncode == 2, result.stdout + result.stderr


def test_offenders_are_printed_one_per_line_without_the_diff_marker(
    tmp_path: Path,
) -> None:
    """Output hygiene: the leading '-' DIFF marker is stripped so a caller
    (or a human) reading the output sees the actual removed TEXT, not diff
    syntax. Deliberately uses text that does NOT itself start with a
    markdown bullet ('-'), so this only proves the diff marker is gone --
    not that every leading '-' vanished (a markdown-bullet entry legitimately
    starts with one after stripping)."""
    repo = _init_repo(tmp_path, BASE_TEXT)

    base_with_continuation = BASE_TEXT.replace(
        "- **Entry one.** Some settled fact, decided a while ago.\n",
        "- **Entry one.** Some settled fact, decided a while ago.\n"
        "  A continuation line with no leading bullet, original wording.\n",
    )
    _write_and_commit(repo, base_with_continuation, "add a continuation line")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rewritten = (
        "# Decisions\n\n"
        "- **Entry one.** Some settled fact, decided a while ago.\n"
        "  A continuation line with no leading bullet, REWORDED here.\n"
        "- **Entry two.** Another fact, also settled.\n"
    )
    _write_and_commit(repo, rewritten, "reword the continuation line")

    result = _run(repo, base)
    assert result.returncode == 1
    offending = [
        line for line in result.stdout.splitlines() if line.startswith("REMOVED:")
    ]
    assert offending
    assert any("original wording" in line for line in offending)
    for line in offending:
        assert not line.startswith("REMOVED: -"), (
            "the diff '-' marker leaked into the printed offender"
        )


def test_base_ahead_of_head_does_not_flag_the_bases_own_new_lines(
    tmp_path: Path,
) -> None:
    """The comparison is three-dot (merge base), not two-dot.

    At review/land time the branch under review is routinely BEHIND
    origin/trunk, which appends to docs/decisions.md on nearly every land. A
    two-dot `git diff <base> <head>` reports every line the BASE gained and
    the head lacks as REMOVED -- spurious offenders for entries the branch
    never touched. Sabotage proof: swapping the script's '...' back to a
    two-dot comparison turns this test red.
    """
    repo = _init_repo(tmp_path, BASE_TEXT)
    fork_point = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # The branch: an innocent append, nothing removed.
    _git(repo, "checkout", "-q", "-b", "branch")
    _write_and_commit(
        repo,
        BASE_TEXT + "- **Entry three.** Appended by the branch.\n",
        "branch appends entry three",
    )
    branch_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Meanwhile trunk moves ahead with an append of its own.
    _git(repo, "checkout", "-q", "trunk")
    _git(repo, "reset", "-q", "--hard", fork_point)
    _write_and_commit(
        repo,
        BASE_TEXT + "- **Entry four.** Appended by trunk after the fork.\n",
        "trunk appends entry four",
    )

    result = _run(repo, "trunk", branch_head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_removed_line_starting_with_two_dashes_is_still_flagged(
    tmp_path: Path,
) -> None:
    """A removed content line beginning with '-- ' renders as '--- ...' in a
    default diff and would be mistaken for the '--- a/<path>' file header by
    a naive '^-' scan -- failing OPEN on exactly what is being guarded.
    Sabotage proof: reverting to a '^-' scan with a '^--- ' skip turns this
    test red."""
    base_text = BASE_TEXT + "-- a continuation dash line, original wording.\n"
    repo = _init_repo(tmp_path, base_text)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _write_and_commit(repo, BASE_TEXT, "silently drop the dash line")

    result = _run(repo, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "original wording" in result.stdout


def test_too_many_arguments_is_a_usage_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, BASE_TEXT)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = _run(repo, base, "HEAD", "extra-arg")
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()
