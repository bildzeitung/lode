"""Tests for lode.faithfulness step 1 -- the verbatim-span check (lode-1k3.2).

Acceptance (docs/retrieval.md, faithfulness gate step 1): a ``quoted_span`` absent
from the cited version body fails the check (so the claim is dropped downstream); a
span differing only by whitespace is accepted via normalized-whitespace match; no
model is ever invoked -- these are pure string checks.
"""

from lode.answer import Claim, Support
from lode.faithfulness import (
    claim_spans_verified,
    normalize_whitespace,
    span_occurs,
    support_verified,
)

BODY = "lode ships rerank OFF in the walking skeleton; deepen it later."


def test_exact_span_occurs() -> None:
    assert span_occurs("rerank OFF", BODY)


def test_absent_span_does_not_occur() -> None:
    # A fabricated quote -- nowhere in the body.
    assert not span_occurs("rerank ON by default", BODY)


def test_whitespace_only_difference_is_accepted() -> None:
    # Reflowed whitespace (newline + collapsed spaces) still matches.
    assert span_occurs("rerank\n  OFF", BODY)
    assert span_occurs("rerank OFF", "rerank\tOFF\nin the skeleton")


def test_non_whitespace_difference_is_rejected() -> None:
    # Differs by a real character, not just whitespace -- must not match.
    assert not span_occurs("rerank OFFF", BODY)


def test_normalize_whitespace_collapses_and_strips() -> None:
    assert normalize_whitespace("  a\t b\n\nc  ") == "a b c"


def test_support_verified_uses_resolved_body() -> None:
    s = Support(version_id="v-1", quoted_span="walking skeleton")
    assert support_verified(s, BODY)
    assert not support_verified(s, "unrelated text")


def test_claim_dropped_when_a_span_is_absent() -> None:
    # One real span, one fabricated -- "every quoted_span must occur" fails.
    claim = Claim(
        text="rerank is off",
        support=[
            Support(version_id="v-1", quoted_span="rerank OFF"),
            Support(version_id="v-2", quoted_span="never written"),
        ],
    )
    bodies = {"v-1": BODY, "v-2": BODY}
    assert not claim_spans_verified(claim, bodies)


def test_claim_kept_when_all_spans_verify_across_targets() -> None:
    claim = Claim(
        text="rerank is off in the skeleton",
        support=[
            Support(version_id="v-1", quoted_span="rerank OFF"),
            Support(snapshot_id="s-9", quoted_span="walking\nskeleton"),
        ],
    )
    bodies = {"v-1": BODY, "s-9": BODY}
    assert claim_spans_verified(claim, bodies)


def test_claim_with_unresolved_target_is_not_verified() -> None:
    # A cited target the caller could not resolve fails closed, never crashes.
    claim = Claim(
        text="x",
        support=[Support(version_id="v-missing", quoted_span="rerank OFF")],
    )
    assert not claim_spans_verified(claim, {})
