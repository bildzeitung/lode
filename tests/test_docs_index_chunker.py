"""Tests for lode.docs_index_chunker -- the docs/ lookup index chunker (lode-t6o1.1).

Covers the acceptance criteria: max(unit_bytes) <= 16384 over the REAL docs/
corpus (not a fixture), fence-awareness, decisions.md's bullet-based split with
its oversized outlier entry actually hard-splitting, line-range accuracy, and
the decision-record/reference-process file tag.

The real-corpus assertions deliberately pin MECHANISMS, never today's file
count or a specific entry's dimensions -- docs/ is append-only and grows, and a
gate that fails on the next doc added teaches nothing.
"""

import functools
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


@functools.cache
def _corpus_units() -> tuple[Unit, ...]:
    """Chunk the real docs/ corpus once per session, not once per test.

    Mirrors the cached-corpus-read pattern tests/conftest.py established in
    lode-es1i; that cache globs .claude/skills + .claude/agents only, so docs/
    needs its own.
    """
    return tuple(chunk_corpus(DOCS_DIR))


@functools.cache
def _per_file() -> tuple[tuple[Path, tuple[str, ...], tuple[Unit, ...]], ...]:
    """``(path, raw_lines, units)`` per real docs/ file, chunked once."""
    out = []
    for md_path in sorted(DOCS_DIR.glob("*.md")):
        raw_lines = tuple(md_path.read_text(encoding="utf-8").splitlines())
        out.append(
            (md_path, raw_lines, tuple(chunk_file(md_path, "\n".join(raw_lines))))
        )
    return tuple(out)


@functools.cache
def _decisions_units() -> tuple[Unit, ...]:
    path = DOCS_DIR / "decisions.md"
    return tuple(chunk_file(path, path.read_text(encoding="utf-8")))


# --- the size invariant, over the real corpus -------------------------------


def test_real_corpus_units_never_exceed_max_bytes():
    units = _corpus_units()
    assert units, "expected the real docs/ corpus to yield units"
    offenders = [
        (u.path, u.line_lo, u.line_hi, _bytes(u))
        for u in units
        if _bytes(u) > MAX_UNIT_BYTES
    ]
    assert not offenders, f"units over {MAX_UNIT_BYTES} bytes: {offenders}"


def test_real_corpus_covers_every_docs_file():
    # Set equality, not a pinned count: docs/ gains files routinely (this very
    # epic adds some), and a `len(...) == 15` pin would fail on the next one
    # while proving nothing the set comparison does not already prove.
    seen = {Path(u.path).name for u in _corpus_units()}
    on_disk = {p.name for p in DOCS_DIR.glob("*.md")}
    assert seen == on_disk
    assert on_disk, "expected docs/ to contain markdown files"


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


def test_mismatched_fence_marker_does_not_close_the_fence():
    # A '~~~' shown as an example inside a ``` block must NOT close it. A bare
    # toggle closes here, then re-opens on the real ``` closer, leaving the
    # whole rest of the file flagged in-fence -- no further boundaries at all.
    text = "## A\n```\n~~~\n## still fenced\n```\n## B\nbody\n"
    units = chunk_file("f.md", text)
    assert [u.first_line for u in units] == ["## A", "## B"]
    assert "## still fenced" in units[0].body


def test_longer_fence_wraps_a_shorter_one():
    # ````-wrapped markdown examples containing ``` blocks are routine in these
    # docs; the inner ``` must not close the outer ```` fence.
    text = "## A\n````\n```\n## inner\n```\n````\n## B\nbody\n"
    units = chunk_file("f.md", text)
    assert [u.first_line for u in units] == ["## A", "## B"]
    assert "## inner" in units[0].body


def test_indented_fence_is_still_a_fence():
    # Fences nested in a list item are indented but real (the docs/ corpus has
    # several). Missing the opener leaves its content unmasked; missing only
    # the *closer* would leave the fence open for the rest of the file.
    # Bullets are boundaries only in a file with no headings, so use that shape
    # -- it makes the masking observable rather than incidentally irrelevant.
    text = "- real one\n  ```\n- fenced, not a bullet\n  ```\n- real two\n"
    units = chunk_file("no-headings.md", text)
    assert [u.first_line for u in units] == ["- real one", "- real two"]
    assert "- fenced, not a bullet" in units[0].body


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


def test_real_decisions_md_oversized_entry_is_hard_split():
    # The decision record names a 556-line / 48.8 KB outlier bullet here. Pin
    # the MECHANISM, not that entry's dimensions: decisions.md splits at
    # top-level '- ' bullets, so every unit would begin with '- ' (bar the
    # leading preamble) if the post-pass never fired. A later unit that does
    # NOT begin with '- ' can only be a hard-split continuation of an oversized
    # bullet entry. Non-brittle as this append-only file grows.
    units = _decisions_units()
    assert [u for u in units if u.first_line.startswith("- ")], (
        "expected decisions.md to split on top-level '- ' bullets"
    )
    continuations = [u for u in units[1:] if not u.first_line.startswith("- ")]
    assert continuations, "expected an oversized bullet entry to be hard-split"


# --- line-range accuracy -----------------------------------------------------


def test_units_tile_each_real_file_exactly_once():
    # Round-trippability is only half the contract: a unit whose body matches
    # its range still loses content if the units leave a gap, and duplicates it
    # if they overlap. Assert the units partition the file line-for-line.
    for md_path, raw_lines, units in _per_file():
        assert units, f"{md_path.name} yielded no units"
        expected_next = 1
        for u in units:
            assert u.line_lo == expected_next, f"gap/overlap in {md_path.name} at {u!r}"
            expected_next = u.line_hi + 1
        assert expected_next - 1 == len(raw_lines), f"{md_path.name} truncated"


def test_line_ranges_resolve_to_exact_body_for_real_corpus():
    for md_path, raw_lines, units in _per_file():
        for u in units:
            assert "\n".join(raw_lines[u.line_lo - 1 : u.line_hi]) == u.body, (
                f"{md_path.name}:{u.line_lo}-{u.line_hi} body/range mismatch"
            )


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


def test_real_corpus_units_inherit_their_file_class():
    # Not "doc_class is one of the two literals" -- that is true by
    # construction of classify() and cannot fail. Pin the path -> stem -> class
    # plumbing on real files of each class instead.
    by_name: dict[str, set[str]] = {}
    for u in _corpus_units():
        by_name.setdefault(Path(u.path).name, set()).add(u.doc_class)
    assert by_name["release.md"] == {"reference/process"}
    assert by_name["decisions.md"] == {"decision-record"}


# --- empty input --------------------------------------------------------------


def test_empty_file_yields_no_units():
    assert chunk_file("empty.md", "") == []
