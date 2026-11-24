"""The rebase-pickup cycle lives in a reference file the `coding` agent opens on a
`needs-rebase` dispatch (lode-w18a.7). These pins keep that dispatch path whole:
the pointer in coding.md, `/code`'s dispatch prompt, the file's place in the
gated corpus, and the guards its own blocks run before the checkout."""

from __future__ import annotations

from conftest import (
    CODING_AGENT_TEXT,
    REBASE_PICKUP_REFERENCE,
    SKILLS_DIR,
    markdown_corpus_blocks,
    markdown_corpus_files,
    markdown_corpus_text,
)


def test_every_markdown_file_beside_a_skill_is_in_the_gated_corpus() -> None:
    """A reference file an agent executes is gated only if the corpus lists it,
    and the corpus lists such files one by one. A new one that is NOT
    agent-executed (a human runbook, say) is exempted here, with the reason."""
    beside_a_skill = sorted(SKILLS_DIR.glob("*/*.md"))
    assert REBASE_PICKUP_REFERENCE in beside_a_skill
    assert set(beside_a_skill) <= set(markdown_corpus_files())


def test_coding_agent_points_a_needs_rebase_dispatch_at_the_reference() -> None:
    assert "](../skills/code/rebase-pickup.md)" in CODING_AGENT_TEXT
    # The cycle's own checkout can take the file out of the working tree, so
    # the pointer also carries the way to print it again.
    assert (
        "git show origin/trunk:.claude/skills/code/rebase-pickup.md"
        in CODING_AGENT_TEXT
    )


def test_code_skill_dispatch_prompt_names_the_reference() -> None:
    """The example prompt is what an orchestrator copies, so the file is named
    inside the blockquote, not only in the prose that introduces it."""
    code_skill = dict(markdown_corpus_text())[SKILLS_DIR / "code" / "SKILL.md"]
    assert any(
        "read `.claude/skills/code/rebase-pickup.md` whole first" in line
        for line in code_skill.splitlines()
        if line.lstrip().startswith(">")
    )


def test_the_rebase_pickup_cycle_is_no_longer_in_the_coding_agent() -> None:
    reference = dict(markdown_corpus_text())[REBASE_PICKUP_REFERENCE]
    assert "Merge current trunk in" not in CODING_AGENT_TEXT
    assert "Merge current trunk in" in reference


def test_reference_runs_both_guards_before_the_checkout_and_the_merge() -> None:
    """The recycled-worktree guard resets and cleans, so it runs only after the
    isolation guard has refused a main checkout; both precede the first command
    that changes the tree."""
    fenced = "\n".join(dict(markdown_corpus_blocks())[REBASE_PICKUP_REFERENCE])
    first_call = [
        fenced.find(command)
        for command in (
            "scripts/isolation-guard.sh",
            "scripts/recycled-worktree-guard.sh",
            "scripts/checkout-land-branch.sh",
            "git merge origin/trunk",
        )
    ]
    assert -1 not in first_call, first_call
    assert first_call == sorted(first_call), first_call
