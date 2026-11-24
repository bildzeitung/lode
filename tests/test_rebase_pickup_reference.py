"""The rebase-pickup cycle lives in a reference file the `coding` agent opens on a
`needs-rebase` dispatch (lode-w18a.7). These pins keep that dispatch path whole:
the pointer in coding.md, `/code`'s dispatch prompt, the file itself, its place
in the gated corpus, and the guards its own blocks run before the checkout."""

from __future__ import annotations

from conftest import (
    AGENTS_DIR,
    CODING_AGENT_TEXT,
    REBASE_PICKUP_REFERENCE,
    SKILLS_DIR,
    markdown_corpus_blocks,
    markdown_corpus_files,
)


def test_reference_file_exists_and_is_in_the_gated_corpus() -> None:
    assert REBASE_PICKUP_REFERENCE.is_file()
    assert REBASE_PICKUP_REFERENCE in markdown_corpus_files()


def test_coding_agent_points_a_needs_rebase_dispatch_at_the_reference() -> None:
    assert "`needs-rebase`" in CODING_AGENT_TEXT
    assert "](../skills/code/rebase-pickup.md)" in CODING_AGENT_TEXT
    assert (AGENTS_DIR / "../skills/code/rebase-pickup.md").samefile(
        REBASE_PICKUP_REFERENCE
    )


def test_code_skill_dispatch_prompt_names_the_reference() -> None:
    """The example prompt is what an orchestrator copies, so the file is named
    inside the blockquote, not only in the prose that introduces it."""
    quoted = [
        line
        for line in (SKILLS_DIR / "code" / "SKILL.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.lstrip().startswith(">")
    ]
    assert any(
        "read `.claude/skills/code/rebase-pickup.md` whole first" in line
        for line in quoted
    )


def test_the_rebase_pickup_cycle_is_no_longer_in_the_coding_agent() -> None:
    assert "Merge current trunk in" not in CODING_AGENT_TEXT
    assert "Merge current trunk in" in REBASE_PICKUP_REFERENCE.read_text(
        encoding="utf-8"
    )


def test_reference_runs_both_guards_before_the_checkout_and_the_merge() -> None:
    """The recycled-worktree guard resets and cleans, so it runs only after the
    isolation guard has refused a main checkout; both precede the first command
    that changes the tree."""
    blocks = dict(markdown_corpus_blocks())[REBASE_PICKUP_REFERENCE]
    commands = (
        "scripts/isolation-guard.sh",
        "scripts/recycled-worktree-guard.sh",
        "scripts/checkout-land-branch.sh",
        "git merge origin/trunk",
    )
    first_block = [
        next((i for i, block in enumerate(blocks) if command in block), len(blocks))
        for command in commands
    ]
    found = dict(zip(commands, first_block, strict=True))
    assert len(blocks) not in first_block, found
    assert first_block == sorted(set(first_block)), found
