"""Tests for lode.gate -- the faithfulness gate's drop/abstain orchestration (lode-1k3.5).

Acceptance (docs/retrieval.md, faithfulness gate steps 4-5): claims that fail the
verbatim-span check are dropped, never displayed; surviving claims are returned in
order with their citations intact; and when zero claims survive, the gate abstains
("your notes don't answer this") and returns no claims.
"""

import dataclasses

import pytest

from lode.answer import Answer, Claim, Support
from lode.gate import ABSTENTION_MESSAGE, GateResult, apply_gate

BODY = "lode ships rerank OFF in the walking skeleton; deepen it later."


def _claim(text: str, span: str, target: str = "v-1") -> Claim:
    return Claim(text=text, support=[Support(version_id=target, quoted_span=span)])


def test_surviving_claim_is_returned_with_citations() -> None:
    answer = Answer.model_validate([_claim("rerank is off", "rerank OFF").model_dump()])
    result = apply_gate(answer, {"v-1": BODY})
    assert not result.abstained
    assert len(result.surviving_claims) == 1
    survivor = result.surviving_claims[0]
    assert survivor.text == "rerank is off"
    # Citation data travels through unchanged for the CLI/Q&A layer to render.
    assert survivor.support[0].version_id == "v-1"
    assert survivor.support[0].quoted_span == "rerank OFF"


def test_failing_claim_is_dropped() -> None:
    answer = Answer([_claim("rerank is on", "rerank ON by default")])
    result = apply_gate(answer, {"v-1": BODY})
    assert result.abstained
    assert result.surviving_claims == ()


def test_zero_survivors_abstains() -> None:
    # Every claim fabricates its quote -- nothing verifies, so the gate abstains.
    answer = Answer(
        [
            _claim("a", "never written"),
            _claim("b", "also fabricated"),
        ]
    )
    result = apply_gate(answer, {"v-1": BODY})
    assert result.abstained
    assert result.surviving_claims == ()


def test_empty_answer_abstains() -> None:
    # The model asserted nothing -- nothing to verify, so abstain.
    result = apply_gate(Answer(), {})
    assert result.abstained
    assert result.surviving_claims == ()


def test_partial_survival_drops_only_the_failures_and_keeps_order() -> None:
    answer = Answer(
        [
            _claim("first", "rerank OFF"),  # verifies
            _claim("second", "fabricated"),  # dropped
            _claim("third", "walking skeleton"),  # verifies
        ]
    )
    result = apply_gate(answer, {"v-1": BODY})
    assert not result.abstained
    assert [c.text for c in result.surviving_claims] == ["first", "third"]


def test_claim_with_one_failing_support_is_dropped() -> None:
    # "every quoted_span must occur" -- one fabricated support drops the whole claim.
    claim = Claim(
        text="mixed",
        support=[
            Support(version_id="v-1", quoted_span="rerank OFF"),
            Support(version_id="v-2", quoted_span="never written"),
        ],
    )
    result = apply_gate(Answer([claim]), {"v-1": BODY, "v-2": BODY})
    assert result.abstained


def test_unresolved_body_fails_closed() -> None:
    # A cited target the caller could not resolve drops the claim, never crashes.
    answer = Answer([_claim("x", "rerank OFF", target="v-missing")])
    result = apply_gate(answer, {})
    assert result.abstained


def test_abstention_message_is_the_honest_failure_string() -> None:
    assert ABSTENTION_MESSAGE == "your notes don't answer this"


def test_gate_result_is_immutable() -> None:
    result = GateResult(surviving_claims=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.surviving_claims = (_claim("x", "rerank OFF"),)  # type: ignore[misc]
