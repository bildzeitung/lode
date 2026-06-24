"""Tests for the golden Q&A set (lode-5y8.3).

Acceptance (lode-5y8.3): ~20-50 Q&A with known-good citations, including
out-of-corpus questions that must abstain, referencing the seed fixture's
reproducible ``version_id``s. The load-bearing guarantees here are that every
citation is **verbatim-present** in the version it cites (so a citation can never
silently fabricate a quote) and that every cited ``version_id`` is a real seed
fixture id (so the set genuinely references the fixture, never a hardcoded hash).
"""

from lode.answer import Support
from lode.eval.golden import GoldenItem, golden_set
from lode.eval.seed import seed_notes
from lode.faithfulness import span_occurs


def _body_by_version_id() -> dict[str, str]:
    return {note.version_id: note.body for note in seed_notes()}


def test_golden_set_size_in_acceptance_range() -> None:
    # Acceptance: a small held-out set of ~20-50 questions.
    assert 20 <= len(golden_set()) <= 50


def test_set_has_both_answerable_and_abstain_items() -> None:
    items = golden_set()
    answerable = [item for item in items if not item.abstain]
    abstain = [item for item in items if item.abstain]
    # A meaningful eval needs a solid base of answerable questions and several
    # out-of-corpus ones to exercise abstention correctness.
    assert len(answerable) >= 15
    assert len(abstain) >= 5


def test_every_citation_span_is_verbatim_in_its_cited_version() -> None:
    # The "known-good citation" guarantee: each quoted_span must occur (exact or
    # normalized-whitespace) in the body of the version it cites -- the same check
    # the faithfulness gate runs. A bad span fails here, loudly.
    bodies = _body_by_version_id()
    for item in golden_set():
        for citation in item.citations:
            assert citation.version_id in bodies, citation.version_id
            assert span_occurs(citation.quoted_span, bodies[citation.version_id]), (
                f"span not verbatim in {citation.version_id!r}: "
                f"{citation.quoted_span!r}"
            )


def test_every_cited_version_id_is_a_fixture_id() -> None:
    # The set references the fixture's reproducible ids, never an invented hash.
    fixture_ids = {note.version_id for note in seed_notes()}
    for item in golden_set():
        for citation in item.citations:
            assert citation.version_id in fixture_ids


def test_answerable_items_are_well_formed() -> None:
    for item in golden_set():
        if item.abstain:
            continue
        assert item.citations, item.question
        assert item.relevant_version_ids
        # Recall@k relevance is exactly the set of cited versions.
        cited = {citation.version_id for citation in item.citations}
        assert item.relevant_version_ids == frozenset(cited)


def test_abstain_items_carry_no_evidence() -> None:
    # Out-of-corpus questions must have nothing to retrieve or cite -- that is what
    # makes "abstain" the only correct behaviour.
    abstain = [item for item in golden_set() if item.abstain]
    assert abstain
    for item in abstain:
        assert item.citations == ()
        assert item.relevant_version_ids == frozenset()


def test_includes_multi_note_synthesis_item() -> None:
    # At least one question must require retrieving more than one note, so recall@k
    # is exercised beyond the single-note case.
    assert any(len(item.relevant_version_ids) >= 2 for item in golden_set())


def test_questions_are_unique() -> None:
    questions = [item.question for item in golden_set()]
    assert len(set(questions)) == len(questions)


def test_citations_are_valid_support_schema() -> None:
    # Each golden citation is real, schema-valid faithfulness evidence: it builds a
    # lode.answer.Support, the exact shape a Q&A answer returns.
    for item in golden_set():
        for citation in item.citations:
            support = Support(
                version_id=citation.version_id,
                quoted_span=citation.quoted_span,
            )
            assert support.target_id == citation.version_id


def test_golden_set_is_deterministic() -> None:
    # Same authored data, same fixture -- no run-to-run drift.
    assert golden_set() == golden_set()
    first = golden_set()[0]
    assert isinstance(first, GoldenItem)
