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

from dataclasses import replace
from types import SimpleNamespace

import pytest

from lode.answer import Answer, Claim, Support
from lode.cited_answer import CitedAnswer, ask, gate_cited_answer
from lode.config import Settings
from lode.egress import WITHHELD_CITATION
from lode.llm_provider import AnthropicProvider
from lode.qa import SONNET_MODEL, QaResult
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


class _StubScorer:
    """Offline stub EntailmentScorer: returns a fixed entailment score.

    Keeps the step-3 NLI gate offline (no model download) so the ask path can be
    exercised with a known score against a configured ``entailment_threshold``.
    """

    def __init__(self, score: float) -> None:
        self._score = score

    def entailment(self, premise: str, hypothesis: str) -> float:
        return self._score


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


def _insert_external(
    conn, *, external_id, snapshot_id, body, no_egress=False, source_type="web"
) -> None:
    """Seed one external + its head snapshot, optionally no_egress."""
    conn.execute(
        "INSERT INTO externals (external_id, source_type, no_egress) VALUES (?, ?, ?)",
        (external_id, source_type, int(no_egress)),
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
        conn,
        "How is lode stored?",
        [_note_context("v1", body)],
        provider=AnthropicProvider(client),
    )

    assert not answer.abstained
    (claim,) = answer.claims
    assert claim.text == "lode is event-sourced."
    assert claim.support[0].version_id == "v1"
    assert claim.support[0].quoted_span == "lode is event-sourced"


def test_passages_sharing_a_parent_block_send_it_only_once(conn) -> None:
    # Two ContextItems chunked from the same parent_block (distinct passage_id /
    # char_range, same target + same parent_block text) must not duplicate that
    # text in the send -- the citation offset comes from the item's own
    # char_range, not from how many times its parent_block was sent (lode-ol2v).
    body = "lode is event-sourced and append-only."
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient([])
    dup_a = _note_context("v1", body)
    dup_b = replace(dup_a, passage_id="p-v1-second", char_range="5:10")

    ask(
        conn,
        "How is lode stored?",
        [dup_a, dup_b],
        provider=AnthropicProvider(client),
    )

    prompt = _user_prompt(client)
    assert prompt.count(body) == 1


def test_deduped_passage_still_contributes_its_char_range_to_the_offset(conn) -> None:
    # The dedup is only safe because body_offset stamping reads the UN-deduped
    # `context`. Here the item whose parent_block reaches the model does NOT
    # contain the cited span in its own char_range -- only the item dropped as a
    # duplicate does -- so the stamped offset proves the dropped item's range
    # still counts (lode-ol2v).
    second_block = "gamma OAuth delta"
    body = "alpha beta" + ("x" * 40) + second_block
    second_start = body.index(second_block)
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient([_note_claim("uses OAuth", "OAuth", "v1")])
    kept = _note_context("v1", body)  # char_range "0:5" -- does not contain the span
    dropped = replace(
        kept,
        passage_id="p-v1-2",
        char_range=f"{second_start}:{len(body)}",
        passage_text=second_block,
    )

    answer = ask(conn, "q", [kept, dropped], provider=AnthropicProvider(client))

    assert _user_prompt(client).count(body) == 1  # the duplicate was dropped
    (claim,) = answer.claims
    assert claim.support[0].body_offset == body.index("OAuth")


def test_surviving_claim_stamps_body_offset_from_its_own_retrieved_passage(
    conn,
) -> None:
    """``OAuth`` occurs twice in the body, but the only retrieved passage for this
    target covers the SECOND occurrence's section -- so the stamped
    ``Support.body_offset`` (lode-hruz) must point there, not the leftmost."""
    second_block = "gamma OAuth delta"
    body = "alpha OAuth beta" + ("x" * 40) + second_block
    second_offset = body.index("OAuth", body.index("OAuth") + 1)
    second_start = body.index(second_block)
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient([_note_claim("uses OAuth", "OAuth", "v1")])
    context = [
        ContextItem(
            tier=TrustTier.OWNED_NOTE,
            passage_id="p-v1-1",
            target_version="v1",
            char_range=f"{second_start}:{len(body)}",
            passage_text=second_block,
            parent_block=body,
            score=0.9,
        ),
    ]

    answer = ask(conn, "q", context, provider=AnthropicProvider(client))

    assert not answer.abstained
    (claim,) = answer.claims
    assert claim.support[0].body_offset == second_offset
    assert second_offset != body.index("OAuth")  # sanity: not merely the leftmost


def test_surviving_claim_leaves_body_offset_unset_when_no_passage_contains_it(
    conn,
) -> None:
    """A citation whose span isn't inside any single retrieved passage's own
    range (only the larger ``parent_block``) gets no offset -- the renderer
    falls back to its first-occurrence behavior, same as before lode-hruz."""
    body = "lode is event-sourced and append-only."
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient(
        [_note_claim("lode is event-sourced.", "lode is event-sourced", "v1")]
    )

    answer = ask(
        conn,
        "How is lode stored?",
        [_note_context("v1", body)],  # char_range="0:5" -- too narrow to contain it
        provider=AnthropicProvider(client),
    )

    assert not answer.abstained
    (claim,) = answer.claims
    assert claim.support[0].body_offset is None


def test_body_offset_disambiguates_a_whitespace_reflowed_occurrence(conn) -> None:
    """The gate accepts a whitespace-reflowed quote, so the stamping must too:
    the model quotes the span flat, but the passage it was actually retrieved
    from carries it reflowed -- an exact-substring containment test would blind
    the offset to exactly the class of citation lode-35nu.3 cared about."""
    second_block = " gamma rotates\nhourly delta"
    body = "alpha rotates hourly beta" + ("x" * 40) + second_block
    second_start = len(body) - len(second_block)
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient([_note_claim("rotates hourly.", "rotates hourly", "v1")])
    context = [
        ContextItem(
            tier=TrustTier.OWNED_NOTE,
            passage_id="p-v1-1",
            target_version="v1",
            char_range=f"{second_start}:{len(body)}",
            passage_text=second_block,
            parent_block=body,
            score=0.9,
        ),
    ]

    answer = ask(conn, "q", context, provider=AnthropicProvider(client))

    (claim,) = answer.claims
    assert claim.support[0].body_offset == body.index("rotates\nhourly")


def test_unparseable_char_range_is_skipped_rather_than_raising(conn) -> None:
    """``passages.char_range`` is nullable (``schema.sql``), and the stamping runs
    *after* the gate -- a range that doesn't parse must cost the offset, never
    the whole answer."""
    body = "lode is event-sourced and append-only."
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient(
        [_note_claim("lode is event-sourced.", "lode is event-sourced", "v1")]
    )
    item = replace(_note_context("v1", body), char_range="")

    answer = ask(conn, "q", [item], provider=AnthropicProvider(client))

    assert not answer.abstained
    (claim,) = answer.claims
    assert claim.support[0].body_offset is None


def test_fabricated_claim_is_dropped_and_abstains(conn) -> None:
    # A "quoted" span that is in no version body fails the gate -> dropped -> abstain.
    body = "lode is event-sourced and append-only."
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    client = _FakeClient(
        [_note_claim("lode mutates in place.", "mutates in place", "v1")]
    )

    answer = ask(
        conn, "q", [_note_context("v1", body)], provider=AnthropicProvider(client)
    )

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

    answer = ask(
        conn, "q", [_note_context("v1", body)], provider=AnthropicProvider(client)
    )

    assert not answer.abstained
    assert [c.text for c in answer.claims] == ["event-sourced", "append-only"]


def test_empty_answer_abstains(conn) -> None:
    # The model asserted nothing -- nothing survives, so the gate abstains.
    _insert_note(conn, note_id="n1", version_id="v1", body="some body")
    client = _FakeClient([])

    answer = ask(
        conn,
        "q",
        [_note_context("v1", "some body")],
        provider=AnthropicProvider(client),
    )

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
        provider=AnthropicProvider(client),
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
        conn,
        "q",
        [_note_context("v-secret", "the secret is 42")],
        provider=AnthropicProvider(client),
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

    answer = ask(
        conn, "q", [_external_context("s1", body)], provider=AnthropicProvider(client)
    )

    prompt = _user_prompt(client)
    assert '<source id="s1" kind="external">' in prompt
    assert not answer.abstained
    assert answer.claims[0].support[0].snapshot_id == "s1"
    assert answer.claims[0].support[0].version_id is None


def test_no_egress_external_kept_off_cloud_and_surfaced_as_withheld(conn) -> None:
    # Same enforcement path as the note case, exercised over an external
    # snapshot (lode-w0h.7): _resolve_target's externals join resolves
    # no_egress for a snapshot_id target exactly like it does for a note's
    # version_id, so a withheld external never reaches the cloud context and
    # is cited as present-but-withheld -- while staying locally retrievable
    # (this test only asserts the egress path; retrieval is untouched by the
    # flag, per lode.egress's "no_egress gates egress only").
    _insert_external(
        conn, external_id="EXT-open", snapshot_id="s-open", body="public runbook"
    )
    _insert_external(
        conn,
        external_id="EXT-secret",
        snapshot_id="s-secret",
        body="internal creds runbook",
        no_egress=True,
    )
    client = _FakeClient([])

    answer = ask(
        conn,
        "q",
        [
            _external_context("s-open", "public runbook"),
            _external_context("s-secret", "internal creds runbook"),
        ],
        provider=AnthropicProvider(client),
    )

    prompt = _user_prompt(client)
    assert "internal creds runbook" not in prompt  # withheld body never left the box
    assert "s-secret" not in prompt
    assert "public runbook" in prompt  # the sendable external did go out
    assert [c.target_id for c in answer.withheld_citations] == ["s-secret"]
    assert answer.withheld_citations[0].note == WITHHELD_CITATION


def test_no_egress_scope_withholds_already_captured_external_web_host(conn) -> None:
    """A URL-host scope rule withholds an already-captured 'web' external at
    its next send, with no per-row flag set and no migration/backfill
    (lode-35nu.11.8, cited_answer._resolve_target site).
    """
    from lode.no_egress_scope import NoEgressScopeRule

    settings = Settings(
        no_egress_scopes=[
            NoEgressScopeRule(source_type="web", match="internal.example.com")
        ]
    )
    _insert_external(
        conn,
        external_id="https://internal.example.com/secret-runbook",
        snapshot_id="s-secret",
        body="internal creds runbook",
    )
    client = _FakeClient([])

    answer = ask(
        conn,
        "q",
        [_external_context("s-secret", "internal creds runbook")],
        provider=AnthropicProvider(client),
        settings=settings,
    )

    prompt = _user_prompt(client)
    assert "internal creds runbook" not in prompt
    assert [c.target_id for c in answer.withheld_citations] == ["s-secret"]
    # No write performed to the row by the scope rule.
    row = conn.execute(
        "SELECT no_egress FROM externals WHERE "
        "external_id = 'https://internal.example.com/secret-runbook'"
    ).fetchone()
    assert row[0] == 0


def test_no_egress_scope_removed_immediately_unwithholds(conn) -> None:
    """Removing a scope rule un-withholds immediately -- no migration needed."""
    _insert_external(
        conn,
        external_id="https://internal.example.com/runbook",
        snapshot_id="s1",
        body="the runbook says rotate the certs.",
    )
    claim = Claim(
        text="rotate the certs.",
        support=[Support(snapshot_id="s1", quoted_span="rotate the certs")],
    )
    client = _FakeClient([claim])

    # No scope rule configured -- the previously-covered external now sends.
    answer = ask(
        conn,
        "q",
        [_external_context("s1", "the runbook says rotate the certs.")],
        provider=AnthropicProvider(client),
        settings=Settings(no_egress_scopes=[]),
    )

    prompt = _user_prompt(client)
    assert "rotate the certs" in prompt
    assert answer.withheld_citations == ()


def test_no_egress_scope_composes_with_per_row_flag(conn) -> None:
    """Per-row flag denies even when no scope rule matches (either denying denies)."""
    from lode.no_egress_scope import NoEgressScopeRule

    settings = Settings(
        no_egress_scopes=[NoEgressScopeRule(source_type="jira", match="OTHER")]
    )
    _insert_external(
        conn,
        external_id="https://internal.example.com/runbook",
        snapshot_id="s1",
        body="secret runbook",
        no_egress=True,
    )
    client = _FakeClient([])

    answer = ask(
        conn,
        "q",
        [_external_context("s1", "secret runbook")],
        provider=AnthropicProvider(client),
        settings=settings,
    )

    prompt = _user_prompt(client)
    assert "secret runbook" not in prompt
    assert [c.target_id for c in answer.withheld_citations] == ["s1"]


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


def test_ask_honors_configured_entailment_threshold(conn) -> None:
    # The configured entailment_threshold reaches the step-3 gate on the ask path:
    # a synthesis claim (span present, but not extractively coupled, so it reaches
    # NLI) scoring 0.5 survives under a laxer threshold and is dropped under a
    # stricter one -- proving the threaded Settings, not the Settings() default,
    # decides the gate. The stub scorer keeps step 3 offline.
    body = "lode ships rerank OFF in the walking skeleton; deepen it later."
    _insert_note(conn, note_id="n1", version_id="v1", body=body)
    claim = _note_claim("rerank is on", "rerank OFF", "v1")  # not coupled -> step 3

    strict = ask(
        conn,
        "q",
        [_note_context("v1", body)],
        provider=AnthropicProvider(_FakeClient([claim])),
        scorer=_StubScorer(0.5),
        settings=Settings(entailment_threshold=0.8),
    )
    assert strict.abstained

    lax = ask(
        conn,
        "q",
        [_note_context("v1", body)],
        provider=AnthropicProvider(_FakeClient([claim])),
        scorer=_StubScorer(0.5),
        settings=Settings(entailment_threshold=0.4),
    )
    assert not lax.abstained
    assert lax.claims[0].text == "rerank is on"
