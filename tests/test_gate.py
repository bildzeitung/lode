"""Tests for lode.gate -- the faithfulness gate's drop/abstain orchestration (lode-1k3.5).

Acceptance (docs/retrieval.md, faithfulness gate steps 4-5): claims that fail the
staged per-claim decision are dropped, never displayed; surviving claims are
returned in order with their citations intact; and when zero claims survive, the
gate abstains ("your notes don't answer this") and returns no claims.

The decision is staged: verbatim-span check (step 1), then the extractive-coupling
fast path (step 2, lode-1k3.3), then NLI entailment (step 3, lode-1k3.4). A claim
that passes the span check but is *not* extractively coupled is handed to a local
NLI scorer; it survives only if its cited spans jointly entail it at or above the
conservative threshold, else is dropped (fail-closed). The scorer is an injectable
seam -- these tests pass a stub so they stay offline and deterministic, and the
deterministic-drop cases assert a *non-entailing* span (stub below threshold) is
still rejected.
"""

import dataclasses

import pytest

from lode.answer import Answer, Claim, Support
from lode.gate import ABSTENTION_MESSAGE, GateResult, apply_gate

BODY = "lode ships rerank OFF in the walking skeleton; deepen it later."

# The default entailment_threshold (Settings) ships conservative at 0.9; the stub
# scores below sit clearly above/below it so the gate decision is unambiguous.
_ENTAILED = 0.99
_NOT_ENTAILED = 0.1


class _StubScorer:
    """Offline stub EntailmentScorer: a fixed score, recording every call.

    Keeps the step-3 gate offline (no model download) and the decision trivial to
    assert. ``calls`` records the (premise, hypothesis) pairs it was handed, so a
    test can prove the scorer was *bypassed* -- never called -- when a claim is
    resolved by step 1 or step 2.
    """

    def __init__(self, score: float) -> None:
        self._score = score
        self.calls: list[tuple[str, str]] = []

    def entailment(self, premise: str, hypothesis: str) -> float:
        self.calls.append((premise, hypothesis))
        return self._score


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
            _claim("rerank off", "rerank OFF"),  # span ok + coupled -> survives
            _claim("second", "fabricated"),  # span absent -> dropped
            _claim("walking skeleton", "walking skeleton"),  # span ok + coupled
        ]
    )
    result = apply_gate(answer, {"v-1": BODY})
    assert not result.abstained
    assert [c.text for c in result.surviving_claims] == [
        "rerank off",
        "walking skeleton",
    ]


def test_real_quote_not_containing_payload_is_rejected() -> None:
    # Inverted quote: the span is verbatim-present (passes step 1) but the claim's
    # payload "on" is not inside it -- not coupled, so it reaches step-3 NLI. The
    # span does not entail the claim (stub below threshold), so it is dropped
    # fail-closed (acceptance: a real quote lacking the payload is rejected).
    scorer = _StubScorer(_NOT_ENTAILED)
    answer = Answer([_claim("rerank is on", "rerank OFF")])
    result = apply_gate(answer, {"v-1": BODY}, scorer=scorer)
    assert result.abstained
    assert scorer.calls == [("rerank OFF", "rerank is on")]  # reached step 3


def test_drifted_number_is_rejected() -> None:
    # The wrong number is both quoted-verbatim (step 1 passes) and not the claim's
    # number -- coupling fails on the drifted digit, so it reaches step-3 NLI; the
    # span does not entail the drifted claim (stub below threshold) -> dropped.
    body = "the cache holds 3000 entries before eviction."
    answer = Answer(
        [_claim("the cache holds 5000 entries", "cache holds 3000 entries")]
    )
    result = apply_gate(answer, {"v-1": body}, scorer=_StubScorer(_NOT_ENTAILED))
    assert result.abstained


def test_coupled_claim_is_verified_without_nli() -> None:
    # Payload lies inside the span -> verified outright on the deterministic fast
    # path (acceptance: a claim whose payload is inside the span survives, no NLI).
    body = "the cache holds 3000 entries before eviction."
    scorer = _StubScorer(_NOT_ENTAILED)  # would drop if step 3 ran
    answer = Answer([_claim("cache holds 3000", "cache holds 3000 entries")])
    result = apply_gate(answer, {"v-1": body}, scorer=scorer)
    assert not result.abstained
    assert result.surviving_claims[0].text == "cache holds 3000"
    assert scorer.calls == []  # step 2 short-circuited; the NLI seam was bypassed


def test_synthesis_claim_above_threshold_survives() -> None:
    # A genuine synthesis claim: span verbatim-present (step 1) but payload not
    # inside any single span (not coupled), so it falls to step-3 NLI. The cited
    # spans jointly entail it (stub above threshold) -> it survives, with citation.
    scorer = _StubScorer(_ENTAILED)
    # payload {rerank, on} -- "on" is absent from the span, so step 2 fails.
    answer = Answer([_claim("rerank is on", "rerank OFF")])
    result = apply_gate(answer, {"v-1": BODY}, scorer=scorer)
    assert not result.abstained
    assert result.surviving_claims[0].text == "rerank is on"
    assert scorer.calls == [("rerank OFF", "rerank is on")]


def test_entailment_at_threshold_survives() -> None:
    # The boundary is inclusive: a score equal to the threshold survives (>=).
    from lode.config import Settings

    threshold = Settings().entailment_threshold
    answer = Answer([_claim("rerank is on", "rerank OFF")])
    result = apply_gate(answer, {"v-1": BODY}, scorer=_StubScorer(threshold))
    assert not result.abstained


def test_entailment_premise_joins_all_cited_spans() -> None:
    # "Jointly entail": the cited spans are joined into one premise handed to the
    # scorer, so multi-span synthesis is weighed together, not span-by-span.
    body_a = "lode ships rerank OFF in the walking skeleton."
    body_b = "the cache holds 3000 entries before eviction."
    claim = Claim(
        text="rerank is off and the cache holds 3000",
        support=[
            Support(version_id="v-a", quoted_span="rerank OFF"),
            Support(version_id="v-b", quoted_span="cache holds 3000 entries"),
        ],
    )
    scorer = _StubScorer(_ENTAILED)
    result = apply_gate(Answer([claim]), {"v-a": body_a, "v-b": body_b}, scorer=scorer)
    assert not result.abstained
    assert scorer.calls == [
        (
            "rerank OFF cache holds 3000 entries",
            "rerank is off and the cache holds 3000",
        )
    ]


def test_custom_threshold_from_settings_is_honored() -> None:
    # The acceptance threshold reads from settings; a stricter threshold drops a
    # mid-confidence synthesis claim that a laxer one would admit.
    from lode.config import Settings

    answer = Answer([_claim("rerank is on", "rerank OFF")])
    strict = apply_gate(
        answer,
        {"v-1": BODY},
        scorer=_StubScorer(0.5),
        settings=Settings(entailment_threshold=0.8),
    )
    assert strict.abstained
    lax = apply_gate(
        answer,
        {"v-1": BODY},
        scorer=_StubScorer(0.5),
        settings=Settings(entailment_threshold=0.4),
    )
    assert not lax.abstained


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
