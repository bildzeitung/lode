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
from unittest import mock

import pytest

from lode.answer import Answer, Claim, Support
from lode.cited_answer import CitedAnswer, ask, gate_cited_answer
from lode.config import Settings
from lode.egress import WITHHELD_CITATION
from lode.llm_provider import AnthropicProvider
from lode.qa import SONNET_MODEL, QaResult
from lode.retrieval import ContextItem, TrustTier
from lode.storage import init_db
from lode.tool_dispatch import FETCH
from lode.webfetch import RawResponse


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
    # snapshot (lode-w0h.7): _resolve_targets' externals join resolves
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
    (lode-35nu.11.8, cited_answer._resolve_targets site).
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


def test_resolve_targets_batches_distinct_targets_into_two_queries(conn) -> None:
    """cited_answer._resolve_targets resolves every distinct note target and every
    distinct external target in one round trip each -- at most two DB queries for
    target resolution regardless of how many context items (or repeated targets)
    are in play (lode-ekqh)."""
    from lode.cited_answer import _resolve_targets

    _insert_note(conn, note_id="n1", version_id="v1", body="alpha body")
    _insert_note(conn, note_id="n2", version_id="v2", body="beta body")
    _insert_external(conn, external_id="EXT-1", snapshot_id="s1", body="gamma body")
    _insert_external(conn, external_id="EXT-2", snapshot_id="s2", body="delta body")
    context = [
        _note_context("v1", "alpha body"),
        _note_context("v1", "alpha body"),  # repeated target -- no extra round trip
        _note_context("v2", "beta body"),
        _external_context("s1", "gamma body"),
        _external_context("s2", "delta body"),
        _external_context("s2", "delta body"),  # repeated target -- no extra round trip
    ]
    # sqlite3.Connection.execute is a read-only C-level attribute (can't be
    # monkeypatched directly), so count round trips via the trace callback
    # instead -- it fires once per statement actually sent to the engine.
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        resolved = _resolve_targets(conn, context)
    finally:
        conn.set_trace_callback(None)

    # Count EVERY statement the call issued, not just the IN(...) ones: a filter
    # keyed on "IN" would silently drop a reintroduced per-target
    # "WHERE version_id = ?" fallback and still pass, which is the exact
    # regression this test exists to catch.
    assert len(statements) <= 2, statements
    assert resolved["v1"] == ("alpha body", False)
    assert resolved["v2"] == ("beta body", False)
    assert resolved["s1"] == ("gamma body", False)
    assert resolved["s2"] == ("delta body", False)


def test_ask_treats_a_target_absent_from_the_store_as_no_egress_false(conn) -> None:
    """A cited target with no matching row (deleted, or never captured) must still
    resolve to a ``None`` body and ``no_egress=False`` -- the same safe default a
    per-target lookup returned before batching, so the gate fails the claim closed
    without ever treating the missing target as withheld."""
    client = _FakeClient([_note_claim("ghost claim", "ghost body", "missing-v")])

    answer = ask(
        conn,
        "q",
        [_note_context("missing-v", "ghost body")],
        provider=AnthropicProvider(client),
    )

    assert answer.abstained  # span can't verify against a body that was never resolved
    assert answer.withheld_citations == ()  # not withheld -- simply unresolved


def test_batched_resolution_composes_no_egress_per_target_not_across_the_batch(
    conn,
) -> None:
    """Batching resolves many targets in one query but must still compose no_egress
    from EACH target's OWN row (lode-ekqh over lode-35nu.11.8): one scope-matched
    external, one per-row-flagged external, one clean external of the same
    source_type, and a clean note all ride the same two IN(...) queries -- the two
    denials must withhold only themselves, and must not leak onto their neighbours.
    """
    from lode.cited_answer import _resolve_targets
    from lode.no_egress_scope import NoEgressScopeRule

    settings = Settings(
        no_egress_scopes=[
            NoEgressScopeRule(source_type="web", match="internal.example.com")
        ]
    )
    _insert_external(
        conn,
        external_id="https://internal.example.com/runbook",
        snapshot_id="s-scoped",
        body="scoped secret",
    )
    _insert_external(
        conn,
        external_id="https://public.example.com/flagged",
        snapshot_id="s-flagged",
        body="flagged secret",
        no_egress=True,
    )
    _insert_external(
        conn,
        external_id="https://public.example.com/open",
        snapshot_id="s-open",
        body="public external body",
    )
    _insert_note(conn, note_id="n-open", version_id="v-open", body="open note body")

    resolved = _resolve_targets(
        conn,
        [
            _external_context("s-scoped", "scoped secret"),
            _external_context("s-flagged", "flagged secret"),
            _external_context("s-open", "public external body"),
            _note_context("v-open", "open note body"),
        ],
        settings.no_egress_scopes,
    )

    assert resolved["s-scoped"] == ("scoped secret", True)  # host rule, no row flag
    assert resolved["s-flagged"] == ("flagged secret", True)  # row flag, no host rule
    assert resolved["s-open"] == ("public external body", False)  # neither
    assert resolved["v-open"] == ("open note body", False)  # notes have no scope


class _QueueWebFetcher:
    """Stub Fetcher (lode.webfetch.Fetcher protocol) returning one canned response."""

    def __init__(self, response: RawResponse) -> None:
        self._response = response
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        return self._response


def test_ask_passes_tools_enabled_through_to_answer_question(conn) -> None:
    """lode-8vvp layer 1: cited_answer.ask must pass tools_enabled=True to
    qa.answer_question -- this is the fix for the bug that made
    ask_tools_enabled inert on a real 'lode ask'. A regression that drops the
    argument (or reverts to the old default of False) fails this test."""
    with mock.patch("lode.cited_answer.answer_question") as mocked:
        mocked.return_value = QaResult(
            answer=Answer([]),
            withheld_citations=(),
            model=SONNET_MODEL,
            egress_log_id=1,
        )
        ask(conn, "q", [], provider=AnthropicProvider(_FakeClient([])))

    (_call,) = mocked.call_args_list
    assert _call.kwargs["tools_enabled"] is True


def test_end_to_end_tool_turn_cites_a_fetched_snapshot_at_the_ask_layer(
    conn,
) -> None:
    """lode-8vvp's headline acceptance criterion, demonstrated at the
    cited_answer.ask layer (not only lode.qa.answer_question, lode-8hsk's own
    end-to-end test).

    A real 'lode ask' with settings.ask_tools_enabled=True answers a question
    requiring a live lookup: a stub provider drives a free tool turn (fetch)
    -> tool_result -> final forced-schema turn, producing a claim whose
    support cites the snapshot the fetch tool persisted. That snapshot has NO
    entry in the context-derived bodies map (nothing in ``context`` cites it)
    -- it only reaches the gate because cited_answer.ask resolves
    QaResult.tool_snapshot_ids into the bodies map. The UNMODIFIED
    faithfulness gate then verifies the claim against those bytes and lets it
    survive -- the answer does not abstain.
    """
    settings = Settings(ask_tools_enabled=True)
    url = "https://example.com/live-incident"
    html = (
        "<html><body><article><p>"
        + ("Prod incident postmortem details. " * 20)
        + "</p></article></body></html>"
    )
    web_fetcher = _QueueWebFetcher(
        RawResponse(final_url=url, status_code=200, text=html)
    )

    fetch_block = mock.MagicMock()
    fetch_block.type = "tool_use"
    fetch_block.name = FETCH
    fetch_block.input = {"source_type": "web", "external_id": url}
    fetch_block.id = "toolu_1"
    free_turn_response = mock.MagicMock()
    free_turn_response.content = [fetch_block]
    free_turn_response.stop_reason = "tool_use"

    quoted_span = "Prod incident postmortem details."

    text_block = mock.MagicMock()
    text_block.type = "text"
    second_free_turn_response = mock.MagicMock()
    second_free_turn_response.content = [text_block]
    second_free_turn_response.stop_reason = "end_turn"

    _responses = [free_turn_response, second_free_turn_response]

    def _create_side_effect(**_kwargs):
        if _responses:
            return _responses.pop(0)
        snapshot_id, body = conn.execute(
            "SELECT snapshot_id, body FROM snapshots WHERE external_id = ?",
            (url,),
        ).fetchone()
        assert quoted_span in body
        claim_block = mock.MagicMock()
        claim_block.type = "tool_use"
        claim_block.name = "_ClaimsEnvelope"
        claim_block.input = {
            "claims": [
                {
                    "text": quoted_span,
                    "support": [
                        {"snapshot_id": snapshot_id, "quoted_span": quoted_span}
                    ],
                }
            ]
        }
        claim_block.id = "toolu_2"
        response = mock.MagicMock()
        response.content = [claim_block]
        response.stop_reason = "tool_use"
        return response

    client = mock.MagicMock()
    client.messages.create.side_effect = _create_side_effect

    answer = ask(
        conn,
        "What happened in the prod incident?",
        [],  # no retrieved context at all -- the citation is purely tool-sourced
        provider=AnthropicProvider(client),
        settings=settings,
        web_fetcher=web_fetcher,
    )

    assert web_fetcher.calls == [url]
    assert not answer.abstained
    (claim,) = answer.claims
    (support,) = claim.support
    assert support.snapshot_id
    assert support.version_id is None


def test_ask_tools_enabled_false_reproduces_notes_only_prompt_byte_for_byte(
    conn,
) -> None:
    """settings.ask_tools_enabled=False (the default) must send the exact same
    system prompt as before lode-8vvp/lode-8hsk -- lode.qa._SYSTEM_PROMPT --
    even though cited_answer.ask now always passes tools_enabled=True: the
    knob alone must decide, via lode.tool_dispatch.build_ask_tools collapsing
    to (), never a caller-side conditional.

    This pins the ASK layer's half: the wire's system prompt is qa._SYSTEM_PROMPT
    and the call went through the empty-tools structured_call path at all (a
    non-empty tool set routes through messages.create, so _FakeMessages.parse
    would never be called and the single-call unpack below would fail). That
    _SYSTEM_PROMPT is itself byte-for-byte the pre-lode-8hsk notes-only prompt is
    pinned separately, against a frozen literal, by test_qa.py."""
    from lode.qa import _SYSTEM_PROMPT

    body = "lode ships rerank OFF in the walking skeleton."
    client = _FakeClient([_note_claim("rerank is off", "rerank OFF", "v1")])

    ask(
        conn,
        "q",
        [_note_context("v1", body)],
        provider=AnthropicProvider(client),
        settings=Settings(ask_tools_enabled=False),
    )

    (call,) = client.messages.calls
    assert call["system"] == _SYSTEM_PROMPT
