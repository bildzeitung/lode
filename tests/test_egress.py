"""Tests for lode.egress -- the no_egress tier (lode-fk8.1).

Asserts the acceptance criteria: a note marked no_egress is never routed into a
send (enrichment or Q&A), is surfaced as "present, withheld from cloud synthesis"
rather than silently dropped, and -- because the partition gates egress only --
the same item is still present for the caller to index/retrieve locally.
"""

from dataclasses import dataclass

from lode.egress import (
    WITHHELD_CITATION,
    EgressItem,
    WithheldCitation,
    partition_egress,
)


@dataclass(frozen=True)
class _Passage:
    """A duck-typed Withholdable -- proves a Q&A/enrichment caller's own object works."""

    target_id: str
    text: str
    no_egress: bool = False


def test_no_egress_item_is_withheld_not_sendable() -> None:
    secret = EgressItem("v-secret", no_egress=True)
    plain = EgressItem("v-plain", no_egress=False)
    decision = partition_egress([secret, plain])
    assert decision.sendable == (plain,)
    assert decision.withheld == (secret,)
    # Never appears in what is sent to Claude.
    assert secret not in decision.sendable


def test_egress_item_defaults_to_sendable() -> None:
    # New notes/sources only become no_egress when the flag (or its default) says so.
    assert EgressItem("v1").no_egress is False
    assert partition_egress([EgressItem("v1")]).sendable == (EgressItem("v1"),)


def test_same_precondition_at_enrichment_and_qa_send() -> None:
    # One function, called identically by both send paths -- a no_egress item is
    # withheld at BOTH sends, not just Q&A (the precondition's whole point).
    items = [EgressItem("v1", no_egress=True), EgressItem("v2")]
    enrichment_decision = partition_egress(items)
    qa_decision = partition_egress(items)
    assert enrichment_decision.withheld == qa_decision.withheld == (items[0],)
    assert enrichment_decision.sendable == qa_decision.sendable == (items[1],)


def test_withheld_item_is_cited_present_withheld_not_dropped() -> None:
    secret = EgressItem("v-secret", no_egress=True)
    decision = partition_egress([secret, EgressItem("v-plain")])
    citations = decision.withheld_citations
    assert citations == (WithheldCitation("v-secret", WITHHELD_CITATION),)
    assert citations[0].note == "present, withheld from cloud synthesis"
    # "rather than silently dropped": the withheld item is still reachable.
    assert decision.withheld == (secret,)


def test_no_egress_item_stays_locally_present_for_retrieval() -> None:
    # Gates egress only: a withheld item is routed aside, never discarded, so the
    # caller can still index and retrieve it locally (keyword + vector).
    secret = EgressItem("v-secret", no_egress=True)
    decision = partition_egress([secret])
    assert decision.sendable == ()
    assert secret in decision.withheld  # present, not lost


def test_order_is_preserved_within_each_side() -> None:
    items = [
        EgressItem("a"),
        EgressItem("b", no_egress=True),
        EgressItem("c"),
        EgressItem("d", no_egress=True),
    ]
    decision = partition_egress(items)
    assert [i.target_id for i in decision.sendable] == ["a", "c"]
    assert [i.target_id for i in decision.withheld] == ["b", "d"]


def test_empty_input_yields_empty_decision() -> None:
    decision = partition_egress([])
    assert decision.sendable == ()
    assert decision.withheld == ()
    assert decision.withheld_citations == ()


def test_partition_preserves_caller_object_type() -> None:
    # The caller's own content object (here a passage) survives the partition intact,
    # carrying its payload -- the send path sends decision.sendable directly.
    passages = [
        _Passage("v1", "public note", no_egress=False),
        _Passage("v2", "work secret", no_egress=True),
    ]
    decision = partition_egress(passages)
    assert decision.sendable == (passages[0],)
    assert decision.withheld == (passages[1],)
    assert decision.sendable[0].text == "public note"
    assert decision.withheld_citations == (WithheldCitation("v2"),)
