"""Pins `scripts/land-merge-one.sh`'s passive-export restore onto the canonical list (lode-2nw5).

Same shape as `tests/test_beads_passive_exports.py`: the fix this ticket made was replacing a
hardcoded `.beads/issues.jsonl` literal in the merge-retry's `git restore --staged --worktree`
call with a loop over `scripts/beads-passive-exports.txt` (the canonical list `lode-do3q`
established) -- so both `.beads/issues.jsonl` and `.beads/interactions.jsonl` are protected
against the same re-staging trap, not just the first. This module pins that the script no longer
re-inlines either literal, and still names the canonical list by filename, so a future edit can't
silently regress back to a hardcoded single path.

The other three sites in the same cluster (two `.claude/skills/land/SKILL.md` bash blocks, one
`.claude/skills/release/SKILL.md` bash block, and `tests/test_land_skill_guard_coverage.py`'s
allowlist entry) are a deliberate WONTFIX -- see `docs/decisions.md`, search "lode-2nw5" -- and are
not touched or pinned here.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_LIST = REPO_ROOT / "scripts" / "beads-passive-exports.txt"
MERGE_ONE = REPO_ROOT / "scripts" / "land-merge-one.sh"


def _canonical_entries() -> list[str]:
    return CANONICAL_LIST.read_text(encoding="utf-8").splitlines()


def test_land_merge_one_does_not_re_inline_a_canonical_relpath() -> None:
    """The anti-drift invariant this ticket bought: no literal copy of either passive-export
    relpath survives in the script's own text -- both must come from the canonical list."""
    text = MERGE_ONE.read_text(encoding="utf-8")
    for rel in _canonical_entries():
        assert rel not in text, (
            f"scripts/land-merge-one.sh re-inlines the canonical relpath {rel!r} -- it should "
            "restore --staged over scripts/beads-passive-exports.txt's entries instead"
        )


def test_land_merge_one_names_the_canonical_list() -> None:
    """Cheap proof the script actually reads the list this module is asserting about, so a
    rename of the list cannot leave this test green while the script reads nothing."""
    text = MERGE_ONE.read_text(encoding="utf-8")
    assert CANONICAL_LIST.name in text, (
        f"{MERGE_ONE} no longer reads {CANONICAL_LIST.name}"
    )


def test_land_merge_one_restores_staged_and_worktree_for_every_canonical_entry() -> (
    None
):
    """The retry loop must use `--staged --worktree` (matching the original single-file
    call's semantics) rather than a narrower restore that would leave a dirty worktree copy."""
    text = MERGE_ONE.read_text(encoding="utf-8")
    assert "git restore --staged --worktree" in text
