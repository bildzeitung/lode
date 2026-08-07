"""Tests for lode.faithfulness steps 1-2 -- span check + extractive coupling.

Step 1 (lode-1k3.2): a ``quoted_span`` absent from the cited version body fails
the check (so the claim is dropped downstream); a span differing only by
whitespace is accepted via normalized-whitespace match.

Step 2 (lode-1k3.3, docs/retrieval.md faithfulness gate): a claim whose
load-bearing payload lies inside one of its cited spans is extractively coupled
(verified outright); a claim whose payload is not inside any single span -- an
inverted quote, a drifted number, or a synthesis split across spans -- is not
coupled. Both steps are pure string checks; no model is ever invoked.

Step 3 (lode-1k3.4): a claim that passes step 1 but is not coupled is scored by a
local NLI / cross-encoder (the :class:`EntailmentScorer` seam) for whether its
cited spans jointly entail it. ``claim_entailed`` joins the spans into one premise
and compares the score to a threshold (fail-closed); these tests inject a stub so
they stay offline, and exercise the :func:`_sigmoid` squash + the lazy
FastEmbed-backed default's wiring without loading the real model.
"""

import math
from pathlib import Path

import pytest

from lode.answer import Claim, Support
from lode.config import Settings
from lode.faithfulness import (
    FastEmbedEntailmentScorer,
    _sigmoid,
    claim_entailed,
    claim_extractively_coupled,
    claim_spans_verified,
    locate_span,
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


def test_locate_span_returns_offsets_into_the_body_as_given() -> None:
    """``locate_span`` is the primitive ``span_occurs`` is derived from; the ask
    screen renders surrounding context from these offsets, so they must index the
    body as given, never a whitespace-normalized copy."""
    start, end = locate_span("rerank OFF", BODY)
    assert BODY[start:end] == "rerank OFF"


def test_locate_span_offsets_span_the_reflowed_region_of_the_body() -> None:
    body = "lead in\nrerank\t OFF\ntrailing"
    start, end = locate_span("rerank OFF", body)
    # Offsets bracket the body's own (differently whitespaced) text, so the
    # surrounding context either side stays contiguous with the highlight.
    assert body[start:end] == "rerank\t OFF"
    assert body[:start] == "lead in\n"
    assert body[end:] == "\ntrailing"


def test_locate_span_returns_none_for_an_absent_span() -> None:
    assert locate_span("rerank ON by default", BODY) is None


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


def test_compound_identifier_fragment_couples_known_fail_open_exposure() -> None:
    """Known, explicitly accepted exposure (lode-1qxy, docs/retrieval.md): ``_WORD``
    splits hyphenated compounds, so a span's compound identifier contributes bare
    fragments to the accepting set -- a claim naming only a fragment of the span's
    compound still couples. This is the fail-*open* direction (a spurious coupling
    HIT returns True and bypasses NLI), not the harmless miss direction.

    This test is a canary, not a specification: it pins *today's* accepted
    behavior. If ``_WORD`` is ever tightened -- here or as a side effect of
    unrelated tokenizer work -- this test will fail and must be updated
    deliberately, alongside the docs/retrieval.md decision note, not silently.
    """
    # The span's "maxmemory-policy" splits into a bare "policy" token, which
    # spuriously supplies the claim's payload.
    assert _coupled("the policy is allkeys-lru", "maxmemory-policy allkeys-lru")
    # Same mechanism: the claim names only the "DNS" fragment of the span's "DNS-01".
    assert _coupled("the check uses DNS", "the check uses DNS-01 validation")


def test_negated_span_is_not_coupled() -> None:
    # lode-w2y7: the span negates the claim ("isn't" vs "is") but pure
    # containment ignores the negation entirely -- the claim's payload
    # ("cache", "invalidated") is still a subset of the span's tokens. This is
    # the ticket's own motivating example: it must NOT couple.
    assert not _coupled("the cache is invalidated", "the cache isn't invalidated")


def test_dont_and_wont_spans_are_not_coupled() -> None:
    # The two contractions a bare-stem cue list cannot carry without colliding
    # with the real words "don" / "won" -- matched whole, so they block here.
    assert not _coupled(
        "rebuilds invalidate the cache", "rebuilds don't invalidate the cache"
    )
    assert not _coupled(
        "the worker retries", "the worker won't stop; retries are dropped"
    )


def test_typographic_apostrophe_negation_is_not_coupled() -> None:
    # Bodies harvested from the web carry U+2019, not the ASCII apostrophe.
    assert not _coupled("the cache is invalidated", "the cache isn’t invalidated")


def test_negation_lookalike_word_still_couples() -> None:
    # "won"/"don" as ordinary words must not raise a false cue and demote a
    # legitimate fast-path couple to NLI.
    assert _coupled(
        "don won the rerank bake-off", "don won the rerank bake-off outright"
    )


def test_negation_present_on_both_sides_still_couples() -> None:
    # The negation-asymmetry check only blocks a cue that's in the span but
    # *absent* from the claim -- a claim that itself states the negation still
    # couples normally.
    assert _coupled("the cache isn't invalidated", "the cache isn't invalidated")


# --- Step 3: NLI entailment ------------------------------------------------


class _StubScorer:
    """Offline stub EntailmentScorer: fixed score, records the (premise, hypothesis)."""

    def __init__(self, score: float) -> None:
        self._score = score
        self.calls: list[tuple[str, str]] = []

    def entailment(self, premise: str, hypothesis: str) -> float:
        self.calls.append((premise, hypothesis))
        return self._score


def _entailed(text: str, *spans: str, scorer, threshold: float = 0.9) -> bool:
    return claim_entailed(
        Claim(
            text=text,
            support=[Support(version_id="v-1", quoted_span=s) for s in spans],
        ),
        scorer,
        threshold=threshold,
    )


def test_entailed_above_threshold() -> None:
    assert _entailed("rerank is on", "rerank OFF", scorer=_StubScorer(0.95))


def test_not_entailed_below_threshold_fails_closed() -> None:
    assert not _entailed("rerank is on", "rerank OFF", scorer=_StubScorer(0.1))


def test_threshold_boundary_is_inclusive() -> None:
    # At/above threshold survives -- the comparison is ``>=``.
    assert _entailed("x", "y", scorer=_StubScorer(0.9), threshold=0.9)
    assert not _entailed("x", "y", scorer=_StubScorer(0.89), threshold=0.9)


def test_entailment_joins_spans_into_one_premise() -> None:
    # Jointly entail: the cited spans are concatenated into a single premise so the
    # scorer weighs them together; the claim text is the hypothesis.
    scorer = _StubScorer(0.95)
    _entailed("the combined claim", "first span", "second span", scorer=scorer)
    assert scorer.calls == [("first span second span", "the combined claim")]


def test_sigmoid_is_monotonic_and_bounded() -> None:
    assert _sigmoid(0.0) == 0.5
    assert math.isclose(_sigmoid(20.0), 1.0, abs_tol=1e-6)
    assert math.isclose(_sigmoid(-20.0), 0.0, abs_tol=1e-6)
    assert _sigmoid(-1.0) < _sigmoid(0.0) < _sigmoid(1.0)


def test_sigmoid_handles_large_magnitudes_without_overflow() -> None:
    # The two-branch stable form must not raise OverflowError on extreme logits.
    assert 0.0 < _sigmoid(1000.0) <= 1.0
    assert 0.0 <= _sigmoid(-1000.0) < 1.0


class _FakeCrossEncoder:
    """Stands in for fastembed's TextCrossEncoder: returns one fixed logit."""

    def __init__(self, logit: float) -> None:
        self._logit = logit
        self.seen: tuple[str, list[str]] | None = None

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.seen = (query, documents)
        return [self._logit]


def test_fastembed_scorer_construction_loads_no_model() -> None:
    # Lazy load: constructing the default scorer must not touch fastembed (mirrors
    # the rerank cross-encoder), so a gate run reaching no step-3 claim stays cheap.
    scorer = FastEmbedEntailmentScorer(Settings())
    assert scorer._model is None


def test_fastembed_scorer_squashes_logit_and_frames_pair() -> None:
    # With a fake cross-encoder injected, entailment() sigmoid's the raw logit and
    # frames (hypothesis=query, premise=document) -- no real model, no download.
    scorer = FastEmbedEntailmentScorer(Settings())
    scorer._model = _FakeCrossEncoder(2.0)
    score = scorer.entailment("the cited span", "the claim")
    assert math.isclose(score, _sigmoid(2.0))
    assert scorer._model.seen == ("the claim", ["the cited span"])


# --- FastEmbedEntailmentScorer._load: cache_dir under $LODE_HOME, never /tmp
# (lode-gmo) — mirrors the same test on FastEmbedEmbedder / FastEmbedCrossEncoder.
# Verified by patching the fastembed TextCrossEncoder constructor itself, so this
# stays offline.


def test_load_passes_durable_model_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastembed.rerank import cross_encoder

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))
    captured: dict[str, object] = {}

    class _FakeTextCrossEncoder:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(cross_encoder, "TextCrossEncoder", _FakeTextCrossEncoder)

    scorer = FastEmbedEntailmentScorer(Settings())
    scorer._load()

    assert captured["cache_dir"] == str(tmp_path / "root" / "models")
