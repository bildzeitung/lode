"""The rebase-pickup cycle lives in a reference file the `coding` agent opens on a
`needs-rebase` dispatch (lode-w18a.7). These pins keep that dispatch path whole:
the pointer in coding.md, the file itself, and its place in the gated corpus."""

from __future__ import annotations

from conftest import (
    AGENTS_DIR,
    CODING_AGENT_TEXT,
    SKILLS_DIR,
    markdown_corpus_files,
)

REFERENCE = SKILLS_DIR / "code" / "rebase-pickup.md"


def test_reference_file_exists_and_is_in_the_gated_corpus() -> None:
    assert REFERENCE.is_file()
    assert REFERENCE in markdown_corpus_files()


def test_coding_agent_points_a_needs_rebase_dispatch_at_the_reference() -> None:
    assert "`needs-rebase`" in CODING_AGENT_TEXT
    assert "](../skills/code/rebase-pickup.md)" in CODING_AGENT_TEXT
    assert (AGENTS_DIR / "../skills/code/rebase-pickup.md").is_file()


def test_the_rebase_pickup_cycle_is_no_longer_in_the_coding_agent() -> None:
    assert "Merge current trunk in" not in CODING_AGENT_TEXT
    assert "Merge current trunk in" in REFERENCE.read_text(encoding="utf-8")
