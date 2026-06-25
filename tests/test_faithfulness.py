"""Tests for lode.faithfulness steps 1-2 -- span check + extractive coupling.

Step 1 (lode-1k3.2): a ``quoted_span`` absent from the cited version body fails
the check (so the claim is dropped downstream); a span differing only by
whitespace is accepted via normalized-whitespace match.

Step 2 (lode-1k3.3, docs/retrieval.md faithfulness gate): a claim whose
load-bearing payload lies inside one of its cited spans is extractively coupled
(verified outright); a claim whose payload is not inside any single span -- an
inverted quote, a drifted number, or a synthesis split across spans -- is not
coupled. Both steps are pure string checks; no model is ever invoked.
"""

from lode.answer import Claim, Support
from lode.faithfulness import (
    claim_extractively_coupled,
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


def _coupled(text: str, *spans: str) -> bool:
    return claim_extractively_coupled(
        Claim(
            text=text,
            support=[Support(version_id="v-1", quoted_span=s) for s in spans],
        )
    )


def test_payload_inside_span_is_coupled() -> None:
    # Glue ("is") is dropped; the load-bearing "rerank"/"off" both lie in the span.
    assert _coupled("rerank is off", "lode ships rerank OFF in the skeleton")


def test_inverted_quote_is_not_coupled() -> None:
    # Real but inverted quote: "on" is nowhere in a span that says "off".
    assert not _coupled("rerank is on", "lode ships rerank OFF in the skeleton")


def test_drifted_number_is_not_coupled() -> None:
    # The claim's number is not the span's number -- the drifted digit breaks coupling.
    assert not _coupled("the limit is 5000 requests", "limit is 3000 requests")


def test_correct_number_is_coupled() -> None:
    assert _coupled("the limit is 3000 requests", "the limit is 3000 requests/min")


def test_number_token_is_word_bounded_not_substring() -> None:
    # "5000" must not couple just because it appears inside "150000".
    assert not _coupled("exactly 5000", "the ceiling is 150000 rows")


def test_payload_split_across_spans_is_synthesis_not_coupled() -> None:
    # Each token is in *a* span, but no *single* span holds the whole payload --
    # that is synthesis (step 3's job), not extractive coupling.
    assert not _coupled("rerank off skeleton", "rerank OFF here", "walking skeleton")


def test_payload_inside_one_of_several_spans_is_coupled() -> None:
    # Coupling needs only one span to contain the full payload; extra spans are fine.
    assert _coupled("walking skeleton", "unrelated quote", "the walking skeleton ships")


def test_all_glue_claim_is_not_coupled() -> None:
    # Every token is grammatical glue -- no load-bearing payload survives, so there
    # is nothing to find inside a span and the claim fails closed (not coupled).
    assert not _coupled("the is a", "the is a verbatim span")


def test_coupling_is_case_insensitive() -> None:
    assert _coupled("RERANK OFF", "rerank off in the skeleton")
