"""Tests for lode.docs_index_chunker -- the docs/ lookup index chunker (lode-t6o1.1).

Covers the acceptance criteria: max(unit_bytes) <= 16384 over the REAL docs/
corpus (not a fixture), fence-awareness, decisions.md's bullet-based split and
its 556-line/48.8 KB outlier entry actually splitting, line-range accuracy,
and the decision-record/reference-process file tag.
"""

from pathlib import Path

from lode.docs_index_chunker import (
    MAX_UNIT_BYTES,
    Unit,
    chunk_corpus,
    chunk_file,
    classify,
)

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"


def _bytes(u: Unit) -> int:
    return len(u.body.encode("utf-8"))


# --- the size invariant, over the real corpus -------------------------------


def test_real_corpus_units_never_exceed_max_bytes():
    units = chunk_corpus(DOCS_DIR)
    assert units, "expected the real docs/ corpus to yield units"
    offenders = [
        (u.path, u.line_lo, u.line_hi, _bytes(u))
        for u in units
        if _bytes(u) > MAX_UNIT_BYTES
    ]
    assert not offenders, f"units over {MAX_UNIT_BYTES} bytes: {offenders}"


def test_real_corpus_covers_all_15_docs_files():
    units = chunk_corpus(DOCS_DIR)
    seen = {Path(u.path).name for u in units}
    on_disk = {p.name for p in DOCS_DIR.glob("*.md")}
    assert seen == on_disk
    assert len(on_disk) == 15


def test_a_future_large_section_still_fails_the_invariant_if_unchunked():
    # Regression guard for the post-pass itself, not just today's corpus: an
    # oversized heading-delimited section, made of several paragraphs any one
    # of which is small, must get hard-split down to size via paragraphs.
    paragraphs = "\n\n".join(f"paragraph {i} " + "word " * 400 for i in range(10))
    text = f"# Title\n\n## Section\n{paragraphs}\n"
    units = chunk_file("hypothetical.md", text)
    assert all(_bytes(u) <= MAX_UNIT_BYTES for u in units)
    assert len(units) > 1


def test_a_single_unbroken_oversized_paragraph_is_emitted_as_is():
    # Nothing smaller than a paragraph to split by (docstring's documented
    # last resort): a single, unbroken oversized line is emitted whole rather
    # than dropped or corrupted.
    big_body = "x " * 20000  # well over 16 KB, all on one line
    text = f"## Section\n{big_body}\n"
    units = chunk_file("hypothetical.md", text)
    assert len(units) == 1
    assert units[0].body == f"## Section\n{big_body}"


# --- fence-awareness ---------------------------------------------------------


def test_heading_inside_fence_is_not_a_boundary():
    text = "## Real Section\nintro\n```\n## not a heading\nmore fenced text\n```\ntail\n## Real Section 2\nbody\n"
    units = chunk_file("f.md", text)
    # Only the two REAL '## ' headings (outside the fence) are boundaries.
    assert len(units) == 2
    assert units[0].first_line == "## Real Section"
    assert units[1].first_line == "## Real Section 2"
    # The fenced '## not a heading' line stays inside the first unit's body.
    assert "## not a heading" in units[0].body


def test_bullet_inside_fence_is_not_a_boundary():
    text = "no headings here\n- real bullet one\n```\n- not a bullet\n```\nstill in bullet one\n- real bullet two\n"
    units = chunk_file("no-headings.md", text)
    firsts = [u.first_line for u in units]
    assert "- real bullet one" in firsts
    assert "- real bullet two" in firsts
    assert not any(u.first_line == "- not a bullet" for u in units)
    fenced_unit = next(u for u in units if u.first_line == "- real bullet one")
    assert "- not a bullet" in fenced_unit.body


def test_tilde_fence_also_respected():
    text = "## A\nintro\n~~~\n## fenced heading\n~~~\nrest\n## B\nbody\n"
    units = chunk_file("f.md", text)
    assert len(units) == 2
    assert "## fenced heading" in units[0].body


# --- deepest-heading-level rule ----------------------------------------------


def test_splits_at_deepest_heading_level_when_h3_present():
    text = "# Title\n\n## Section\n\n### Sub A\nbody a\n\n### Sub B\nbody b\n"
    units = chunk_file("f.md", text)
    firsts = [u.first_line for u in units]
    assert "### Sub A" in firsts
    assert "### Sub B" in firsts
    # h1/h2 lines are not themselves boundaries at h3-split granularity --
    # they ride along inside the leading (preamble) unit's body instead.
    assert units[0].first_line == "# Title"
    assert "## Section" in units[0].body


def test_falls_back_to_h2_when_no_h3_present():
    text = "## A\nbody a\n\n## B\nbody b\n"
    units = chunk_file("f.md", text)
    assert [u.first_line for u in units] == ["## A", "## B"]


# --- no-recurring-heading -> bullet split (docs/decisions.md's own shape) ---


def test_no_heading_file_splits_by_top_level_bullet():
    text = "preamble line\n- first entry\ncontinuation\n- second entry\nmore\n"
    units = chunk_file("decisions.md", text)
    assert [u.first_line for u in units] == [
        "preamble line",
        "- first entry",
        "- second entry",
    ]


def test_real_decisions_md_chunks_by_bullet_not_heading():
    text = (DOCS_DIR / "decisions.md").read_text(encoding="utf-8")
    units = chunk_file(DOCS_DIR / "decisions.md", text)
    bullet_units = [u for u in units if u.first_line.startswith("- ")]
    assert bullet_units, "expected decisions.md to split on top-level '- ' bullets"


def test_real_decisions_md_outlier_entry_is_split_not_whole():
    # The decision record names a 556-line / 48.8 KB outlier bullet in this
    # file; the size invariant test above already proves nothing over 16 KB
    # survives, but this pins the mechanism: no single unit spans anywhere
    # near 556 lines / 48.8 KB, i.e. it was actually split, not truncated.
    text = (DOCS_DIR / "decisions.md").read_text(encoding="utf-8")
    units = chunk_file(DOCS_DIR / "decisions.md", text)
    assert all((u.line_hi - u.line_lo + 1) < 556 for u in units)
    assert all(
        _bytes(u) < 48_800 * 8 // 10 for u in units
    )  # well under the outlier's size


# --- line-range accuracy -----------------------------------------------------


def test_line_ranges_resolve_to_exact_body_for_real_corpus():
    for md_path in sorted(DOCS_DIR.glob("*.md")):
        raw_lines = md_path.read_text(encoding="utf-8").splitlines()
        for u in chunk_file(md_path, "\n".join(raw_lines)):
            assert "\n".join(raw_lines[u.line_lo - 1 : u.line_hi]) == u.body


def test_unit_line_lo_and_hi_are_1_based_and_ordered():
    text = "## A\nbody a\n\n## B\nbody b\n"
    units = chunk_file("f.md", text)
    for u in units:
        assert u.line_lo >= 1
        assert u.line_hi >= u.line_lo


# --- classification -----------------------------------------------------------


def test_classify_reference_process_files():
    for name in (
        "keybindings",
        "release",
        "onboarding",
        "tui",
        "editing",
        "test-suite-audit",
    ):
        assert classify(f"docs/{name}.md") == "reference/process"


def test_classify_decision_record_files():
    for name in (
        "decisions",
        "design",
        "storage",
        "retrieval",
        "externals",
        "stack",
        "configuration",
    ):
        assert classify(f"docs/{name}.md") == "decision-record"


def test_every_unit_carries_a_doc_class_tag():
    units = chunk_corpus(DOCS_DIR)
    assert all(u.doc_class in ("decision-record", "reference/process") for u in units)


# --- empty input --------------------------------------------------------------


def test_empty_file_yields_no_units():
    assert chunk_file("empty.md", "") == []
