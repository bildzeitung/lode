"""Tests for lode.answer -- the verifiable answer schema (lode-1k3.1).

Acceptance: the schema validates a list of claims, each with
support(version_id|snapshot_id + verbatim quoted_span); malformed model output is
rejected. Inputs are raw dicts / JSON, the way the Q&A LLM emits them.
"""

import pytest
from pydantic import ValidationError

from lode.answer import Answer, Claim, Support


def _claim(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "text": "lode ships rerank OFF in the walking skeleton.",
        "support": [{"version_id": "v-abc", "quoted_span": "rerank OFF"}],
    }
    base.update(overrides)
    return base


def test_valid_answer_parses() -> None:
    answer = Answer.model_validate([_claim()])
    assert len(answer.claims) == 1
    claim = answer.claims[0]
    assert claim.text.startswith("lode ships")
    assert claim.support[0].version_id == "v-abc"
    assert claim.support[0].quoted_span == "rerank OFF"


def test_answer_parses_from_json_array() -> None:
    # docs/retrieval.md models `answer = [...]` as a bare list.
    answer = Answer.model_validate_json(
        '[{"text": "x", "support": [{"snapshot_id": "s-1", "quoted_span": "x"}]}]'
    )
    assert answer.claims[0].support[0].snapshot_id == "s-1"


def test_empty_answer_is_valid_abstention() -> None:
    assert Answer.model_validate([]).claims == []
    assert Answer().claims == []


def test_support_accepts_snapshot_id() -> None:
    s = Support(snapshot_id="s-9", quoted_span="hello")
    assert s.snapshot_id == "s-9"
    assert s.version_id is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quoted_span": "x"},  # neither version_id nor snapshot_id
        {"version_id": "v", "snapshot_id": "s", "quoted_span": "x"},  # both
    ],
)
def test_support_requires_exactly_one_target(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Support(**kwargs)


def test_target_id_returns_the_cited_target() -> None:
    assert Support(version_id="v-1", quoted_span="x").target_id == "v-1"
    assert Support(snapshot_id="s-1", quoted_span="x").target_id == "s-1"


@pytest.mark.parametrize(
    "support",
    [
        {"version_id": "v"},  # missing quoted_span
        {"version_id": "v", "quoted_span": ""},  # empty quoted_span (min_length=1)
        {"version_id": "", "quoted_span": "x"},  # empty id rejected (min_length=1)
        {"version_id": "v", "quoted_span": "x", "extra": 1},  # extra=forbid
    ],
)
def test_malformed_support_is_rejected(support: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Answer.model_validate([_claim(support=[support])])


@pytest.mark.parametrize(
    "claim",
    [
        {"support": [{"version_id": "v", "quoted_span": "x"}]},  # missing text
        {"text": "", "support": [{"version_id": "v", "quoted_span": "x"}]},  # empty
        {"text": "t", "support": []},  # claim with no evidence
        {"text": "t"},  # missing support
        _claim(extra=1),  # extra=forbid on the claim
    ],
)
def test_malformed_claim_is_rejected(claim: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Answer.model_validate([claim])


def test_claim_model_validates_directly() -> None:
    c = Claim.model_validate(_claim())
    assert isinstance(c.support[0], Support)
