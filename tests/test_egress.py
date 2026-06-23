"""Tests for lode.egress -- the no_egress tier (lode-fk8.1).

Asserts the acceptance criteria: a note marked no_egress is never routed into a
send (enrichment or Q&A), is surfaced as "present, withheld from cloud synthesis"
rather than silently dropped, and -- because the partition gates egress only --
the same item is still present for the caller to index/retrieve locally.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from lode.config import Settings
from lode.egress import (
    QA_PURPOSE,
    WITHHELD_CITATION,
    EgressItem,
    PassageItem,
    WithheldCitation,
    gate_qa_egress,
    log_egress,
    partition_egress,
)
from lode.storage import init_db


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


# --- Q&A egress gate (lode-az0.4): egress_log write + redact + no_egress exclude ---

# A redact-before-egress pattern set with one synthetic secret, so the gate's
# redaction is asserted without depending on the shipped seed set's exact regexes.
_SECRET = "TOPSECRET-42"
_REDACT_SETTINGS = Settings(
    redact_before_egress_patterns=[r"TOPSECRET-\d+"],
    redact_before_index_patterns=[],
)


def _new_db(tmp_path: Path):
    return init_db(tmp_path / "lode.db")


def _egress_rows(conn):
    return conn.execute(
        "SELECT purpose, model, sent_targets, redactions FROM egress_log ORDER BY id"
    ).fetchall()


def test_log_egress_writes_one_auditable_row(tmp_path: Path) -> None:
    conn = _new_db(tmp_path)
    try:
        row_id = log_egress(conn, QA_PURPOSE, "claude-sonnet", ["v1", "v2"], {"v1": 1})
        rows = _egress_rows(conn)
        assert len(rows) == 1
        purpose, model, sent_targets, redactions = rows[0]
        assert (purpose, model) == ("qa", "claude-sonnet")
        assert json.loads(sent_targets) == ["v1", "v2"]
        assert json.loads(redactions) == {"v1": 1}
        assert isinstance(row_id, int)
    finally:
        conn.close()


def test_log_egress_null_redactions_when_nothing_stripped(tmp_path: Path) -> None:
    conn = _new_db(tmp_path)
    try:
        log_egress(conn, QA_PURPOSE, "m", ["v1"], None)
        (_, _, _, redactions) = _egress_rows(conn)[0]
        assert redactions is None
    finally:
        conn.close()


def test_gate_logs_every_qa_send_with_purpose_model_and_targets(tmp_path: Path) -> None:
    # Acceptance: every Q&A send is recorded in egress_log (purpose, model, ids).
    conn = _new_db(tmp_path)
    try:
        result = gate_qa_egress(
            conn,
            "claude-sonnet-4.6",
            [PassageItem("v1", "public a"), PassageItem("v2", "public b")],
        )
        rows = _egress_rows(conn)
        assert len(rows) == 1
        purpose, model, sent_targets, _ = rows[0]
        assert (purpose, model) == ("qa", "claude-sonnet-4.6")
        assert json.loads(sent_targets) == ["v1", "v2"]
        assert result.egress_log_id == 1
        assert [s.target_id for s in result.sent] == ["v1", "v2"]
    finally:
        conn.close()


def test_gate_excludes_no_egress_from_send_and_log(tmp_path: Path) -> None:
    # Acceptance: no_egress content never appears in a send (nor in the audited ids).
    conn = _new_db(tmp_path)
    try:
        result = gate_qa_egress(
            conn,
            "m",
            [
                PassageItem("v-public", "shareable"),
                PassageItem("v-secret", "work secret", no_egress=True),
            ],
        )
        # Withheld, not sent: absent from sent payloads and from the egress_log row.
        assert [s.target_id for s in result.sent] == ["v-public"]
        (_, _, sent_targets, _) = _egress_rows(conn)[0]
        assert "v-secret" not in json.loads(sent_targets)
        # Surfaced to the user as present-but-withheld rather than dropped.
        assert result.withheld_citations == (WithheldCitation("v-secret"),)
        assert result.withheld_citations[0].note == WITHHELD_CITATION
    finally:
        conn.close()


def test_gate_redacts_secret_spans_before_egress_and_records_count(
    tmp_path: Path,
) -> None:
    conn = _new_db(tmp_path)
    try:
        result = gate_qa_egress(
            conn,
            "m",
            [PassageItem("v1", f"key is {_SECRET} ok"), PassageItem("v2", "clean")],
            settings=_REDACT_SETTINGS,
        )
        sent = {s.target_id: s for s in result.sent}
        # The secret never leaves the box; clean passage is untouched.
        assert _SECRET not in sent["v1"].text
        assert "[redacted]" in sent["v1"].text
        assert sent["v2"].text == "clean"
        assert (sent["v1"].redactions, sent["v2"].redactions) == (1, 0)
        # The audit row summarises which targets had redactions applied.
        (_, _, _, redactions) = _egress_rows(conn)[0]
        assert json.loads(redactions) == {"v1": 1}
    finally:
        conn.close()


def test_gate_with_no_redactions_logs_null_redactions(tmp_path: Path) -> None:
    conn = _new_db(tmp_path)
    try:
        gate_qa_egress(
            conn, "m", [PassageItem("v1", "all clean")], settings=_REDACT_SETTINGS
        )
        (_, _, _, redactions) = _egress_rows(conn)[0]
        assert redactions is None
    finally:
        conn.close()


def test_gate_all_withheld_logs_empty_send(tmp_path: Path) -> None:
    # Every candidate is no_egress: a row is still written (the send happened), with
    # no sent targets, and every item comes back as a withheld citation.
    conn = _new_db(tmp_path)
    try:
        result = gate_qa_egress(
            conn,
            "m",
            [PassageItem("v1", "secret", no_egress=True)],
        )
        assert result.sent == ()
        assert result.withheld_citations == (WithheldCitation("v1"),)
        (_, _, sent_targets, _) = _egress_rows(conn)[0]
        assert json.loads(sent_targets) == []
    finally:
        conn.close()


@pytest.mark.parametrize("purpose", ["qa", "enrich"])
def test_egress_log_accepts_both_purposes(tmp_path: Path, purpose: str) -> None:
    # log_egress is reused by the E7 enrichment send with purpose='enrich'.
    conn = _new_db(tmp_path)
    try:
        log_egress(conn, purpose, "m", ["v1"])
        assert _egress_rows(conn)[0][0] == purpose
    finally:
        conn.close()
