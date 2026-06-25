"""Tests for lode.cited_answer -- the E5 gate before display + cited render (lode-az0.3).

Asserts this ticket's acceptance offline (the Anthropic client is mocked, so the
faithfulness gate and egress precondition run with no network and no credentials):

- the E5 faithfulness gate runs on the model output BEFORE the answer is returned
  for display -- a claim whose cited span is not verbatim-present is dropped;
- surviving claims render WITH their citations (version_id/snapshot_id + span);
- the system ABSTAINS when no claim survives the gate;
- the trust-ranked ``ContextItem`` -> ``QaPassage`` adaptation keeps a
  store-resolved no_egress target off-cloud (surfaced as present-but-withheld), and
  the gate verifies only against the egress-cleared targets' stored bodies.
"""

from types import SimpleNamespace

import pytest

from lode.answer import Answer, Claim, Support
from lode.cited_answer import CitedAnswer, ask, gate_cited_answer
from lode.egress import WITHHELD_CITATION
from lode.qa import QaResult, SONNET_MODEL
from lode.retrieval import ContextItem, TrustTier
from lode.storage import init_db


class _FakeMessages:
    """Records every parse() call and returns a fixed parsed claims envelope."""

    def __init__(self, claims: list[Claim]) -> None:
        self._claims = claims
        self.calls: list[dict] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=SimpleNamespace(claims=self._claims))


class _FakeClient:
    """Stand-in for anthropic.Anthropic -- no network, just records the call."""

    def __init__(self, claims: list[Claim]) -> None:
        self.messages = _FakeMessages(claims)


def _user_prompt(client: _FakeClient) -> str:
    """The user-message text of the single recorded parse() call."""
    (call,) = client.messages.calls
    (message,) = call["messages"]
    return message["content"]


@pytest.fixture
def conn():
    connection = init_db(":memory:")
    yield connection
    connection.close()


def _insert_note(conn, *, note_id, version_id, body, no_egress=False) -> None:
    """Seed one note + its create version (head), optionally no_egress."""
    conn.execute(
        "INSERT INTO notes (note_id, head_version_id, no_egress) VALUES (?, NULL, ?)",
        (note_id, int(no_egress)),
    )
    conn.execute(
        "INSERT INTO versions (version_id, note_id, body, op) VALUES (?, ?, ?, 'create')",
        (version_id, note_id, body),
    )
    conn.execute(
        "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
        (version_id, note_id),
    )
    conn.commit()


def _insert_external(conn, *, external_id, snapshot_id, body, no_egress=False) -> None:
    """Seed one external + its head snapshot, optionally no_egress."""
    conn.execute(
        "INSERT INTO externals (external_id, source_type, no_egress) "
        "VALUES (?, 'web', ?)",
        (external_id, int(no_egress)),
    )
    conn.execute(
        "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
        "VALUES (?, ?, ?, 'ok')",
        (snapshot_id, external_id, body),
    )
    conn.execute(
        "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
        (snapshot_id, external_id),
    )
    conn.commit()


def _note_context(version_id: str, text: str) -> ContextItem:
    """A trust-ranked owned-note context item citing ``version_id``."""
    return ContextItem(
        tier=TrustTier.OWNED_NOTE,
        passage_id=f"p-{version_id}",
        target_version=version_id,
        char_range="0:5",
        passage_text=text,
        parent_block=text,
        score=0.9,
    )


def _external_context(
    snapshot_id: str, text: str, *, stale: bool = False
) -> ContextItem:
    """A trust-ranked external context item citing ``snapshot_id``."""
    return ContextItem(
        tier=TrustTier.STALE_EXTERNAL if stale else TrustTier.CURRENT_EXTERNAL,
        passage_id=f"p-{snapshot_id}",
        target_version=snapshot_id,
        char_range="0:5",
        passage_text=text,
        parent_block=text,
        score=0.8,
    )


def _note_claim(text: str, span: str, version_id: str) -> Claim:
    return Claim(text=text, support=[Support(version_id=version_id, quoted_span=span)])


def test_surviving_claim_renders_with_its_citation(conn) -> None:
    # The gate runs before display; a verbatim-supported, extractively coupled claim
    # (its payload lies inside the span) survives and carries its version_id + span
    # citation through for the CLI to render.
    body = "lode is event-sourced and append-only."
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient(
        [_note_claim("lode is event-sourced.", "lode is event-sourced", "v1")]
    )

    answer = ask(
        conn, "How is lode stored?", [_note_context("v1", body)], client=client
    )

    assert not answer.abstained
    (claim,) = answer.claims
    assert claim.text == "lode is event-sourced."
    assert claim.support[0].version_id == "v1"
    assert claim.support[0].quoted_span == "lode is event-sourced"


def test_fabricated_claim_is_dropped_and_abstains(conn) -> None:
    # A "quoted" span that is in no version body fails the gate -> dropped -> abstain.
    body = "lode is event-sourced and append-only."
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient(
        [_note_claim("lode mutates in place.", "mutates in place", "v1")]
    )

    answer = ask(conn, "q", [_note_context("v1", body)], client=client)

    assert answer.abstained
    assert answer.claims == ()


def test_partial_survival_drops_only_the_failures(conn) -> None:
    body = "lode is event-sourced and append-only."
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient(
        [
            _note_claim("event-sourced", "event-sourced", "v1"),  # span ok + coupled
            _note_claim("second", "fabricated quote", "v1"),  # span absent -> dropped
            _note_claim("append-only", "append-only", "v1"),  # span ok + coupled
        ]
    )

    answer = ask(conn, "q", [_note_context("v1", body)], client=client)

    assert not answer.abstained
    assert [c.text for c in answer.claims] == ["event-sourced", "append-only"]


def test_empty_answer_abstains(conn) -> None:
    # The model asserted nothing -- nothing survives, so the gate abstains.
    _insert_note(conn, note_id="n1", version_id="v1", body="some body")
    client = _FakeClient([])

    answer = ask(conn, "q", [_note_context("v1", "some body")], client=client)

    assert answer.abstained
    assert answer.claims == ()


def test_no_egress_note_kept_off_cloud_and_surfaced_as_withheld(conn) -> None:
    # The ContextItem -> QaPassage adaptation resolves no_egress from the store, so a
    # withheld note never reaches the cloud context and is cited as present-but-withheld.
    _insert_note(conn, note_id="n-open", version_id="v-open", body="shareable body")
    _insert_note(
        conn,
        note_id="n-secret",
        version_id="v-secret",
        body="secret body",
        no_egress=True,
    )
    client = _FakeClient([])

    answer = ask(
        conn,
        "q",
        [
            _note_context("v-open", "shareable body"),
            _note_context("v-secret", "secret body"),
        ],
        client=client,
    )

    prompt = _user_prompt(client)
    assert "secret body" not in prompt  # the no_egress body never left the box
    assert "v-secret" not in prompt
    assert "shareable body" in prompt  # the sendable one did
    assert [c.target_id for c in answer.withheld_citations] == ["v-secret"]
    assert answer.withheld_citations[0].note == WITHHELD_CITATION


def test_claim_citing_a_no_egress_target_fails_closed(conn) -> None:
    # A claim whose span is verbatim in a withheld body is still dropped: the gate
    # verifies only against egress-cleared bodies, so content the model never saw
    # cannot support a survivor.
    _insert_note(
        conn,
        note_id="n-secret",
        version_id="v-secret",
        body="the secret is 42",
        no_egress=True,
    )
    client = _FakeClient(
        [_note_claim("the secret is 42", "the secret is 42", "v-secret")]
    )

    answer = ask(
        conn, "q", [_note_context("v-secret", "the secret is 42")], client=client
    )

    assert answer.abstained


def test_external_snapshot_cited_via_snapshot_id(conn) -> None:
    # An external-tier context is labelled external (cite via snapshot_id) and a
    # supported claim survives carrying its snapshot_id citation.
    body = "the runbook says rotate the certs."
    _insert_external(conn, external_id="EXT-1", snapshot_id="s1", body=body)
    claim = Claim(
        text="rotate the certs.",
        support=[Support(snapshot_id="s1", quoted_span="rotate the certs")],
    )
    client = _FakeClient([claim])

    answer = ask(conn, "q", [_external_context("s1", body)], client=client)

    prompt = _user_prompt(client)
    assert '<source id="s1" kind="external">' in prompt
    assert not answer.abstained
    assert answer.claims[0].support[0].snapshot_id == "s1"
    assert answer.claims[0].support[0].version_id is None


def test_gate_cited_answer_composes_survivors_with_withheld() -> None:
    # The pure gate step: survivors from apply_gate plus the result's withheld set.
    body = "lode abstains rather than hallucinate."
    answer = Answer([_note_claim("lode abstains", "lode abstains", "v1")])
    result = QaResult(
        answer=answer, withheld_citations=(), model=SONNET_MODEL, egress_log_id=1
    )

    cited = gate_cited_answer(result, {"v1": body})

    assert isinstance(cited, CitedAnswer)
    assert not cited.abstained
    assert cited.claims[0].text == "lode abstains"


def test_gate_cited_answer_abstains_when_nothing_survives() -> None:
    answer = Answer([_note_claim("wrong", "not in the body", "v1")])
    result = QaResult(
        answer=answer, withheld_citations=(), model=SONNET_MODEL, egress_log_id=1
    )

    cited = gate_cited_answer(result, {"v1": "some other text"})

    assert cited.abstained
    assert cited.claims == ()
