"""Tests for lode.config — the typed settings module (lode-txh.3).

Asserts the acceptance criteria: every knob has a kind tag (runtime/tune/build),
documented defaults load, and invalid values fail validation at load.
"""

import pytest
from pydantic import ValidationError

from lode.config import Kind, Settings, knob_kinds, load_settings

VALID_KINDS = {k.value for k in Kind}


def test_every_knob_has_a_valid_kind_tag() -> None:
    kinds = knob_kinds()
    # Every declared field is tagged, and every tag is one of runtime/tune/build.
    assert set(kinds) == set(Settings.model_fields)
    assert all(kind in VALID_KINDS for kind in kinds.values())


def test_documented_defaults_load() -> None:
    s = load_settings()
    assert s.retrieval_top_k == 20
    assert s.rrf_k == 60
    assert s.rerank_enabled is True
    assert s.rerank_model == "bge-reranker-v2-m3"
    assert s.drawdown_hop_limit == 1
    assert s.content_hash == "xxh3-128"
    assert s.no_egress_default is False


def test_model_ids_are_pinned() -> None:
    s = Settings()
    assert s.enrichment_llm == "claude-haiku-4-5"
    assert s.qa_llm == "claude-sonnet-4-6"
    assert s.qa_think_harder_llm == "claude-opus-4-8"


def test_entailment_gate_ships_fail_closed() -> None:
    assert Settings().entailment_threshold == 0.9


@pytest.mark.parametrize(
    "overrides",
    [
        {"retrieval_top_k": 0},  # gt=0
        {"rrf_k": -1},  # gt=0
        {"entailment_threshold": 1.5},  # le=1.0
        {"entailment_threshold": -0.1},  # ge=0.0
        {"drawdown_hop_limit": -1},  # ge=0
        {"retry_max_attempts": 0},  # ge=1
        {"unknown_knob": 1},  # extra="forbid"
    ],
)
def test_invalid_values_fail_at_load(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        load_settings(**overrides)
