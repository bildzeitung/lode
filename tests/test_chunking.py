"""Tests for lode.chunking -- the structure-aware passage chunker (lode-x6r.1).

Covers the acceptance criteria: deterministic (identical input -> identical
passages); any structural block over N tokens is sub-split with overlap;
``parent_block`` is recorded for every passage; pure / no network / no LLM.
Also pins the invariants that make a passage usable downstream: ``char_range``
locates the exact ``text`` in the body, and ``ord`` runs ``0..k-1``.
"""

from lode.chunking import Passage, _count_tokens, chunk
from lode.config import load_settings

TV = "deadbeef"  # a stand-in target_version (a content-address hex digest)


def _small() -> object:
    """Settings with a tiny chunk budget so the token fallback is exercised."""
    return load_settings(chunk_threshold_tokens=5, chunk_overlap_tokens=2)


def _char_range(passage: Passage) -> tuple[int, int]:
    start, end = passage.char_range.split(":")
    return int(start), int(end)


# --- determinism ------------------------------------------------------------


def test_identical_input_yields_identical_passages():
    body = "# Title\nIntro line.\n\n## Section A\n- one\n- two\n"
    assert chunk(body, TV) == chunk(body, TV)


def test_passages_are_in_document_order_with_sequential_ord():
    body = "# A\nfirst\n\n## B\nsecond\n\n## C\nthird\n"
    passages = chunk(body, TV)
    assert [p.ord for p in passages] == list(range(len(passages)))
    # char ranges are non-decreasing -- passages follow body order.
    starts = [_char_range(p)[0] for p in passages]
    assert starts == sorted(starts)


# --- char_range / text invariant -------------------------------------------


def test_char_range_locates_exact_text_in_body():
    body = "# Heading\nA paragraph of prose.\n\n- a list item\n"
    for passage in chunk(body, TV):
        start, end = _char_range(passage)
        assert body[start:end] == passage.text


def test_passage_id_is_target_version_plus_ord():
    body = "alpha\n\nbeta\n"
    passages = chunk(body, TV)
    assert [p.passage_id for p in passages] == [f"{TV}:{p.ord}" for p in passages]
    assert all(p.target_version == TV for p in passages)


# --- structural splitting ---------------------------------------------------


def test_splits_on_headings_paragraphs_and_list_items():
    body = "# Title\nIntro.\n\n## Section\n- item one\n- item two\n"
    texts = [p.text for p in chunk(body, TV)]
    assert "# Title" in texts
    assert "Intro." in texts
    assert "## Section" in texts
    assert "- item one" in texts
    assert "- item two" in texts


def test_blank_lines_separate_paragraphs():
    body = "first paragraph\n\nsecond paragraph\n"
    texts = [p.text for p in chunk(body, TV)]
    assert texts == ["first paragraph", "second paragraph"]


def test_consecutive_ordinary_lines_form_one_paragraph():
    body = "line one\nline two\nline three\n"
    passages = chunk(body, TV)
    assert len(passages) == 1
    assert passages[0].text == "line one\nline two\nline three"


def test_empty_body_yields_no_passages():
    assert chunk("", TV) == []


def test_whitespace_only_body_yields_no_passages():
    assert chunk("\n   \n\t\n", TV) == []


# --- parent_block (small-to-big) -------------------------------------------


def test_parent_block_is_the_enclosing_section():
    body = "# Title\nIntro.\n\n## Section A\nAlpha.\n- item\n\n## Section B\nBeta.\n"
    passages = chunk(body, TV)
    by_text = {p.text: p for p in passages}
    # The list item under Section A expands to the whole Section A block.
    parent = by_text["- item"].parent_block
    assert "## Section A" in parent
    assert "Alpha." in parent
    assert "- item" in parent
    # ...and not into the next section.
    assert "Section B" not in parent


def test_parent_block_is_recorded_for_every_passage():
    body = "# T\nintro\n\n## S\nbody text here\n- a\n- b\n"
    passages = chunk(body, TV)
    assert passages  # non-empty
    assert all(p.parent_block for p in passages)


def test_unstructured_body_parent_block_is_the_whole_note():
    body = "no headings here\n\njust two paragraphs\n"
    whole = body.strip()
    assert all(p.parent_block == whole for p in chunk(body, TV))


# --- token fallback: sub-split oversized blocks with overlap ----------------


def test_block_over_n_tokens_is_subsplit():
    words = " ".join(f"w{i}" for i in range(12))  # 12 tokens, N=5
    passages = chunk(words, TV, settings=_small())
    assert len(passages) > 1
    # Every emitted passage respects the token budget.
    assert all(_count_tokens(p.text) <= 5 for p in passages)


def test_subsplit_windows_overlap():
    words = " ".join(f"w{i}" for i in range(12))  # N=5, overlap=2, step=3
    passages = chunk(words, TV, settings=_small())
    first = set(passages[0].text.split())
    second = set(passages[1].text.split())
    assert first & second  # consecutive windows share tokens


def test_subsplit_covers_every_token_in_order():
    words = " ".join(f"w{i}" for i in range(12))
    passages = chunk(words, TV, settings=_small())
    seen = []
    for passage in passages:
        for tok in passage.text.split():
            if tok not in seen:
                seen.append(tok)
    assert seen == [f"w{i}" for i in range(12)]


def test_block_at_or_under_n_tokens_is_a_single_passage():
    words = " ".join(f"w{i}" for i in range(5))  # exactly N=5
    passages = chunk(words, TV, settings=_small())
    assert len(passages) == 1
    assert passages[0].text == words


def test_overlap_not_under_threshold_still_terminates():
    # overlap >= N would zero/negate the step; the chunker clamps step to 1.
    settings = load_settings(chunk_threshold_tokens=3, chunk_overlap_tokens=5)
    words = " ".join(f"w{i}" for i in range(8))
    passages = chunk(words, TV, settings=settings)
    assert len(passages) > 1
    assert all(_count_tokens(p.text) <= 3 for p in passages)


def test_each_subsplit_passage_char_range_is_exact():
    words = " ".join(f"word{i}" for i in range(20))
    for passage in chunk(words, TV, settings=_small()):
        start, end = _char_range(passage)
        assert words[start:end] == passage.text


# --- structure-aware split keeps oversized blocks separate ------------------


def test_oversized_blocks_split_independently_of_siblings():
    # Two paragraphs, each over the budget, must not bleed into one another.
    para_a = " ".join(f"a{i}" for i in range(8))
    para_b = " ".join(f"b{i}" for i in range(8))
    body = f"{para_a}\n\n{para_b}\n"
    passages = chunk(body, TV, settings=_small())
    for passage in passages:
        toks = passage.text.split()
        assert not (
            any(t.startswith("a") for t in toks)
            and any(t.startswith("b") for t in toks)
        )
