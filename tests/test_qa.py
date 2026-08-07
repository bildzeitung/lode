"""Tests for lode.qa -- the Q&A structured-claims call (lode-az0.2).

Asserts the acceptance criteria offline (the Anthropic client is mocked, so no
network call is ever made and the gates run without credentials):

- structured claims are parsed via the anthropic ``messages.parse`` + Pydantic path;
- Claude Sonnet 4.6 is the default model, Opus 5 the "think harder" toggle;
- ``no_egress`` passages are EXCLUDED from the cloud context (and surfaced as
  present-but-withheld);
- redaction is applied to the context BEFORE it is sent;
- the send is recorded in the ``egress_log``.
"""

from types import SimpleNamespace

import pytest

from lode.answer import Answer, Claim, Support
from lode.config import Settings
from lode.llm_provider import AnthropicProvider, ModelTier
from lode.qa import (
    MAX_TOKENS,
    OPUS_MODEL,
    SONNET_MODEL,
    QaPassage,
    _ClaimsEnvelope,
    _RequestClaim,
    _RequestSupport,
    answer_question,
)
from lode.storage import init_db


class _FakeMessages:
    """Records every parse() call and returns a fixed parsed envelope."""

    def __init__(self, parsed: object) -> None:
        self._parsed = parsed
        self.calls: list[dict] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self._parsed)


class _FakeClient:
    """Stand-in for anthropic.Anthropic -- no network, just records the call."""

    def __init__(self, parsed: object) -> None:
        self.messages = _FakeMessages(parsed)


def _envelope(claims: list[Claim]) -> SimpleNamespace:
    """A minimal _ClaimsEnvelope stand-in (only .claims is read)."""
    return SimpleNamespace(claims=claims)


@pytest.fixture
def conn():
    """An in-memory lode database with the egress_log table."""
    connection = init_db(":memory:")
    yield connection
    connection.close()


def _user_prompt(client: _FakeClient) -> str:
    """The user-message text of the single recorded parse() call."""
    (call,) = client.messages.calls
    (message,) = call["messages"]
    return message["content"]


def test_returns_parsed_structured_claims(conn) -> None:
    # The structured response is parsed into answer.Answer, claims preserved.
    claims = [
        Claim(
            text="lode is event-sourced.",
            support=[Support(version_id="v1", quoted_span="event-sourced")],
        ),
    ]
    client = _FakeClient(_envelope(claims))
    result = answer_question(
        conn,
        "How is lode stored?",
        [QaPassage("v1", "lode is event-sourced")],
        provider=AnthropicProvider(client),
    )
    assert isinstance(result.answer, Answer)
    assert [c.text for c in result.answer.claims] == ["lode is event-sourced."]
    assert result.answer.claims[0].support[0].version_id == "v1"


def test_sonnet_is_the_default_model(conn) -> None:
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn, "q", [QaPassage("v1", "text")], provider=AnthropicProvider(client)
    )
    assert result.model == SONNET_MODEL
    assert client.messages.calls[0]["model"] == SONNET_MODEL


def test_opus_when_think_harder(conn) -> None:
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        think_harder=True,
        provider=AnthropicProvider(client),
    )
    assert result.model == OPUS_MODEL
    assert client.messages.calls[0]["model"] == OPUS_MODEL


def test_thinking_not_disabled_for_think_harder_call(conn) -> None:
    # lode-3dlt (supersedes lode-d1sr's disabled pin): end-to-end companion to
    # test_llm_provider.py's provider-level assertion -- confirms the
    # think-harder (Opus 5 by default) path never sends an explicit
    # thinking={"type": "disabled"}, since that value 400s on Fable-class
    # models at any effort and on Opus 5 at effort xhigh/max. Opus 5 now runs
    # adaptive thinking instead; MAX_TOKENS was raised to give it headroom.
    client = _FakeClient(_envelope([]))
    answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        think_harder=True,
        provider=AnthropicProvider(client),
    )
    assert "thinking" not in client.messages.calls[0]


def test_think_harder_override_to_a_fable_class_model_works(conn) -> None:
    # lode-3dlt: the regression this ticket exists to fix. Before the fix, a
    # Kind.RUNTIME override of qa_think_harder_llm to a Fable-class model
    # raised an unhandled anthropic.BadRequestError from deep in the provider
    # (thinking={"type": "disabled"} is illegal on Fable-class models at any
    # effort). The call must now succeed and must never send that value.
    settings = Settings(qa_think_harder_llm="claude-fable-5")
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        think_harder=True,
        provider=AnthropicProvider(client),
        settings=settings,
    )
    assert result.model == "claude-fable-5"
    assert client.messages.calls[0]["model"] == "claude-fable-5"
    assert "thinking" not in client.messages.calls[0]


def test_qa_llm_override_reaches_the_call(conn) -> None:
    # lode-obms: settings.qa_llm was declared but never consulted -- an
    # override must actually change which model gets called.
    settings = Settings(qa_llm="claude-custom-qa-model")
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        provider=AnthropicProvider(client),
        settings=settings,
    )
    assert result.model == "claude-custom-qa-model"
    assert client.messages.calls[0]["model"] == "claude-custom-qa-model"


def test_qa_think_harder_llm_override_reaches_the_call(conn) -> None:
    # lode-obms: same for the think-harder tier's knob.
    settings = Settings(qa_think_harder_llm="claude-custom-opus-model")
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        think_harder=True,
        provider=AnthropicProvider(client),
        settings=settings,
    )
    assert result.model == "claude-custom-opus-model"
    assert client.messages.calls[0]["model"] == "claude-custom-opus-model"


def test_qa_llm_default_max_tokens_is_the_source_constant(conn) -> None:
    # lode-d70n: an unset tier.max_tokens falls back to qa.MAX_TOKENS,
    # unchanged from before this ticket.
    client = _FakeClient(_envelope([]))
    answer_question(
        conn, "q", [QaPassage("v1", "text")], provider=AnthropicProvider(client)
    )
    assert client.messages.calls[0]["max_tokens"] == MAX_TOKENS


def test_qa_llm_max_tokens_override_reaches_the_call(conn) -> None:
    # lode-d70n: a Kind.RUNTIME override of qa_llm.max_tokens must actually
    # change the budget sent on the wire, not just the model/effort.
    settings = Settings(qa_llm=ModelTier(model="claude-sonnet-4-6", max_tokens=1234))
    client = _FakeClient(_envelope([]))
    answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        provider=AnthropicProvider(client),
        settings=settings,
    )
    assert client.messages.calls[0]["max_tokens"] == 1234


def test_qa_think_harder_llm_max_tokens_override_reaches_the_call(conn) -> None:
    # lode-d70n: same for the think-harder tier.
    settings = Settings(
        qa_think_harder_llm=ModelTier(model="claude-opus-5", max_tokens=4321)
    )
    client = _FakeClient(_envelope([]))
    answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        think_harder=True,
        provider=AnthropicProvider(client),
        settings=settings,
    )
    assert client.messages.calls[0]["max_tokens"] == 4321


def test_qa_call_timeout_s_default_reaches_the_call(conn) -> None:
    # lode-wfyx: with no Settings passed at all, the Q&A synthesis call is
    # timed by its own qa_call_timeout_s knob, split off the shared
    # enrich_call_timeout_s. The default's *value* is pinned by test_config.py's
    # test_documented_defaults_load, so derive it rather than retype it.
    client = _FakeClient(_envelope([]))
    answer_question(
        conn, "q", [QaPassage("v1", "text")], provider=AnthropicProvider(client)
    )
    assert client.messages.calls[0]["timeout"] == Settings().qa_call_timeout_s


def test_qa_call_timeout_s_override_reaches_the_call(conn) -> None:
    # lode-wfyx: a Kind.RUNTIME override of qa_call_timeout_s must actually
    # change the timeout sent on the wire.
    settings = Settings(qa_call_timeout_s=45.0)
    client = _FakeClient(_envelope([]))
    answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        provider=AnthropicProvider(client),
        settings=settings,
    )
    assert client.messages.calls[0]["timeout"] == 45.0


def test_enrich_call_timeout_s_does_not_reach_the_qa_call(conn) -> None:
    # lode-wfyx: enrich_call_timeout_s bounds only
    # enrich.py's call sites -- overriding it must NOT change the Q&A call's
    # timeout, which stays on qa_call_timeout_s's own (unrelated) default.
    # The mirror direction (the qa knob not leaking into enrich's three
    # sites) needs no test of its own: test_enrich.py's three
    # *_call_timeout_* tests pin enrich_call_timeout_s=42 and assert 42 on
    # the wire, so they already fail (300 != 42) if any of those sites reads
    # qa_call_timeout_s -- verified by sabotage.
    settings = Settings(enrich_call_timeout_s=999.0)
    client = _FakeClient(_envelope([]))
    answer_question(
        conn,
        "q",
        [QaPassage("v1", "text")],
        provider=AnthropicProvider(client),
        settings=settings,
    )
    assert client.messages.calls[0]["timeout"] == settings.qa_call_timeout_s


def test_no_egress_passage_excluded_from_context(conn) -> None:
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn,
        "q",
        [
            QaPassage("v-open", "shareable note body"),
            QaPassage("v-secret", "private note body", no_egress=True),
        ],
        provider=AnthropicProvider(client),
    )
    prompt = _user_prompt(client)
    # The no_egress body and id never reach the cloud context...
    assert "private note body" not in prompt
    assert "v-secret" not in prompt
    # ...the sendable one does...
    assert "shareable note body" in prompt
    # ...and the withheld item is surfaced, not dropped.
    assert [c.target_id for c in result.withheld_citations] == ["v-secret"]


def test_redaction_applied_before_send(conn) -> None:
    settings = Settings(
        redact_before_egress_patterns=[r"TOPSECRET-\d+"],
        redact_before_index_patterns=[],
    )
    client = _FakeClient(_envelope([]))
    answer_question(
        conn,
        "q",
        [QaPassage("v1", "the key is TOPSECRET-42 keep it safe")],
        provider=AnthropicProvider(client),
        settings=settings,
    )
    prompt = _user_prompt(client)
    # The secret was stripped from the context the model received.
    assert "TOPSECRET-42" not in prompt
    assert "[redacted]" in prompt


def test_external_and_note_sources_are_labelled(conn) -> None:
    # The prompt tells Claude which support field to cite per source kind.
    client = _FakeClient(_envelope([]))
    answer_question(
        conn,
        "q",
        [
            QaPassage("v-note", "note text"),
            QaPassage("s-ext", "external text", is_external=True),
        ],
        provider=AnthropicProvider(client),
    )
    prompt = _user_prompt(client)
    assert '<source id="v-note" kind="note">' in prompt
    assert '<source id="s-ext" kind="external">' in prompt


def test_send_is_recorded_in_egress_log(conn) -> None:
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn,
        "q",
        [QaPassage("v1", "text"), QaPassage("v-secret", "x", no_egress=True)],
        provider=AnthropicProvider(client),
    )
    row = conn.execute(
        "SELECT id, purpose, model, sent_targets FROM egress_log WHERE id = ?",
        (result.egress_log_id,),
    ).fetchone()
    assert row is not None
    log_id, purpose, model, sent_targets = row
    assert log_id == result.egress_log_id
    assert purpose == "qa"
    assert model == SONNET_MODEL
    # Only the sendable target is recorded as sent; the no_egress one is not.
    assert "v1" in sent_targets
    assert "v-secret" not in sent_targets


def test_egress_log_records_external_sends_and_excludes_withheld_ones(
    conn,
) -> None:
    # lode-w0h.7: confirm egress_log coverage extends to externals, not just
    # notes -- a sendable external's snapshot_id is recorded as sent; a
    # no_egress external's is excluded entirely (never appears as a "sent"
    # target, mirroring the note case above).
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn,
        "q",
        [
            QaPassage("s-open", "public runbook", is_external=True),
            QaPassage("s-secret", "internal creds", is_external=True, no_egress=True),
        ],
        provider=AnthropicProvider(client),
    )
    (sent_targets,) = conn.execute(
        "SELECT sent_targets FROM egress_log WHERE id = ?",
        (result.egress_log_id,),
    ).fetchone()
    assert "s-open" in sent_targets
    assert "s-secret" not in sent_targets
    assert [c.target_id for c in result.withheld_citations] == ["s-secret"]


def test_empty_answer_is_valid(conn) -> None:
    # The model asserting nothing is a valid answer (downstream abstention path).
    client = _FakeClient(_envelope([]))
    result = answer_question(
        conn, "q", [QaPassage("v1", "text")], provider=AnthropicProvider(client)
    )
    assert result.answer.claims == []


def test_body_offset_is_absent_from_the_provider_schema() -> None:
    # Support.body_offset is an app-side field (stamped after the faithfulness
    # gate, never supplied by the model), but Support also doubles as the
    # structured-output response shape. The request-side mirror
    # (_ClaimsEnvelope -> _RequestClaim -> _RequestSupport) must omit it
    # entirely as a *property* of the JSON schema handed to the provider
    # (lode-9nmk) -- not just describe it as "leave unset". (Doc prose
    # elsewhere in the schema may still mention the field name in passing, so
    # this checks property keys specifically rather than the raw dump.)
    schema = _ClaimsEnvelope.model_json_schema()
    all_defs = {"": schema, **schema.get("$defs", {})}
    for definition in all_defs.values():
        assert "body_offset" not in definition.get("properties", {})


def test_decoded_claims_are_converted_to_real_claim_and_support(conn) -> None:
    # The provider returns the request-side mirror shape (_RequestClaim /
    # _RequestSupport, no body_offset); answer_question converts it into real
    # Claim/Support, where body_offset defaults to None until the faithfulness
    # gate stamps it.
    mirror_claim = _RequestClaim(
        text="lode is event-sourced.",
        support=[_RequestSupport(version_id="v1", quoted_span="event-sourced")],
    )
    client = _FakeClient(_envelope([mirror_claim]))
    result = answer_question(
        conn,
        "How is lode stored?",
        [QaPassage("v1", "lode is event-sourced")],
        provider=AnthropicProvider(client),
    )
    (claim,) = result.answer.claims
    assert isinstance(claim, Claim)
    support = claim.support[0]
    assert isinstance(support, Support)
    assert support.body_offset is None
