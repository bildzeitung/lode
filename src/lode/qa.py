"""The E6 Q&A structured-claims call: ask Claude for cited claims (lode-az0.2).

This is the **cloud synthesis** step of the Q&A pipeline (``docs/retrieval.md``,
"grounded context -> Q&A LLM; cite the precise passage/span"). It hands the
trust-ranked context to Claude and gets back **structured claims**, each pinned
to a verbatim span of a specific cited target (``docs/stack.md`` "Q&A LLM";
``docs/retrieval.md`` "Make the answer schema verifiable"). It owns only the
*call* -- the downstream faithfulness gate (``lode-az0.3`` / ``lode-1k3.2``)
verifies the evidence and abstains; citations are enforced by verification, not
by this response schema (``docs/retrieval.md``).

**Model tier (``docs/stack.md`` "Q&A LLM").** The model is read from
``settings.qa_llm`` (default Claude **Sonnet 4.6**, :data:`SONNET_MODEL`) or, when
``think_harder=True``, from ``settings.qa_think_harder_llm`` (default **Opus
4.8**, :data:`OPUS_MODEL`) -- both ``Kind.RUNTIME`` knobs in :mod:`lode.config`,
so a user override actually reaches the call. Credentials resolve via the SDK
(env var or ``ant auth login`` profile) -- :func:`lode.auth.build_client`, never
a hardcoded key.

**The cloud-egress preconditions are honored before the send, not reimplemented.**
:func:`answer_question` runs the landed Q&A egress gate
(:func:`lode.egress.gate_qa_egress`, the fk8.1/fk8.2 mechanisms) *first*, which in
one step:

- **excludes ``no_egress`` passages** from the cloud context (surfaced as "present,
  withheld from cloud synthesis" instead of dropped -- ``docs/externals.md``);
- **redacts secret spans before egress** (``docs/stack.md`` "the context sent is
  redacted-before-egress");
- **records the send in the ``egress_log``** (``docs/storage.md`` §8 -- one audit
  row per time content leaves the box).

Only the already-redacted, already-cleared payloads it returns are then sent to
Claude. Structured decoding uses **structured outputs + Pydantic** (the same
convention as the enrichment LLM, ``docs/stack.md``), reusing the landed
:class:`lode.answer.Claim` so the schema stays pinned to the verifiable
claims/support shape.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

import anthropic
from pydantic import BaseModel, ConfigDict, Field

from lode.answer import Answer, Claim
from lode.auth import build_client
from lode.config import Settings
from lode.egress import RedactedSend, WithheldCitation, gate_qa_egress

#: Default Q&A model -- Claude Sonnet 4.6 (``docs/stack.md`` "Q&A LLM"). Mirrors
#: :attr:`lode.config.Settings.qa_llm`'s default; the live value always comes
#: from settings, never this constant directly (lode-obms).
SONNET_MODEL = "claude-sonnet-4-6"
#: "Think harder" toggle -- Claude Opus 4.8 (``docs/stack.md`` "Q&A LLM").
#: Mirrors :attr:`lode.config.Settings.qa_think_harder_llm`'s default; the live
#: value always comes from settings, never this constant directly (lode-obms).
OPUS_MODEL = "claude-opus-4-8"

#: Output cap for the synthesis call. Comfortably under the SDK's non-streaming
#: timeout guard; claims are a compact, bounded structure, not long prose.
MAX_TOKENS = 4096

_SYSTEM_PROMPT = (
    "You answer questions strictly from the SOURCES provided, which are passages "
    "from the user's personal knowledge base. Return a list of factual claims. "
    "Every claim must carry the verbatim evidence it rests on, pinned to a "
    "specific source:\n"
    '- for a source with kind="note", cite its id in the support\'s version_id '
    "field;\n"
    '- for a source with kind="external", cite its id in the support\'s '
    "snapshot_id field;\n"
    "- set exactly one of version_id or snapshot_id per support, never both.\n"
    "Each quoted_span must be copied verbatim -- character for character -- from "
    "that source's text. Assert only what the sources support. If the sources do "
    "not answer the question, return no claims. Never use knowledge beyond the "
    "sources."
)


@dataclass(frozen=True)
class QaPassage:
    """One trust-ranked context passage offered to the Q&A send.

    Carries the citation ``target_id``, the passage ``text``, and the
    ``no_egress`` flag, so it satisfies :class:`lode.egress.EgressPassage` and
    feeds :func:`lode.egress.gate_qa_egress` directly (no reimplementation of the
    no_egress / redact / audit precondition). ``is_external`` distinguishes an
    external snapshot (cite via ``snapshot_id``) from an owned note version (cite
    via ``version_id``), mirroring :class:`lode.retrieval.TrustTier`; the caller
    derives it from the trust-ranked :class:`lode.retrieval.ContextItem` tier.
    """

    target_id: str
    text: str
    no_egress: bool = False
    is_external: bool = False


class _ClaimsEnvelope(BaseModel):
    """Object wrapper around the claims list, for structured-output decoding.

    Structured outputs constrain the response to an **object** schema, so the
    verifiable answer (:class:`lode.answer.Answer` is a list-rooted model) is
    decoded through this single-field envelope and unwrapped to ``Answer``.
    Reuses :class:`lode.answer.Claim` so the schema stays pinned to the landed
    claims/support shape -- this module owns the call, not the answer shape.
    """

    model_config = ConfigDict(extra="forbid")

    claims: list[Claim] = Field(
        default_factory=list,
        description="The factual claims answering the question; empty if the "
        "sources do not answer it.",
    )


@dataclass(frozen=True)
class QaResult:
    """Outcome of the Q&A structured-claims call.

    ``answer`` is the verifiable claims (each pinned to a verbatim span of a cited
    target) for the downstream faithfulness gate to verify before display.
    ``withheld_citations`` are the ``no_egress`` items kept off-cloud, surfaced as
    "present, withheld from cloud synthesis". ``model`` is the tier actually used
    (Sonnet or Opus); ``egress_log_id`` is the audit row the gate wrote for the
    send.
    """

    answer: Answer
    withheld_citations: tuple[WithheldCitation, ...]
    model: str
    egress_log_id: int


def answer_question(
    conn: sqlite3.Connection,
    question: str,
    passages: Iterable[QaPassage],
    *,
    think_harder: bool = False,
    client: anthropic.Anthropic | None = None,
    settings: Settings | None = None,
) -> QaResult:
    """Ask Claude for structured, cited claims answering ``question``.

    Selects the model tier from ``settings`` -- ``settings.qa_llm`` by default,
    ``settings.qa_think_harder_llm`` when ``think_harder`` (lode-obms: these
    ``Kind.RUNTIME`` knobs must actually be consulted, not shadowed by a
    hardcoded constant) -- then runs the cloud-egress gate
    (:func:`lode.egress.gate_qa_egress`) over ``passages`` -- excluding
    ``no_egress`` items, redacting secret spans, and writing the ``egress_log``
    audit row -- **before** any byte reaches Claude. Only the redacted, cleared
    payloads it returns are sent. The structured response is decoded with
    Pydantic into :class:`lode.answer.Answer` (claims each pinned to a verbatim
    span of a cited target).

    ``settings`` defaults to :class:`~lode.config.Settings`' own defaults when
    omitted (same pattern as :func:`lode.redact.redact_before_egress_counting`).
    ``client`` defaults to a credential-resolved SDK client
    (:func:`lode.auth.build_client`); tests pass a mock so the gates stay offline.
    """
    settings = settings or Settings()
    model = settings.qa_think_harder_llm if think_harder else settings.qa_llm
    passages = list(passages)
    # Keep note-vs-external per target so the prompt can tell Claude which support
    # field to cite (the gate's RedactedSend carries only target_id + text).
    is_external = {p.target_id: p.is_external for p in passages}

    egress = gate_qa_egress(conn, model, passages, settings)

    client = client or build_client()
    envelope = _request_claims(client, model, question, egress.sent, is_external)
    return QaResult(
        answer=Answer(envelope.claims),
        withheld_citations=egress.withheld_citations,
        model=model,
        egress_log_id=egress.egress_log_id,
    )


def _request_claims(
    client: anthropic.Anthropic,
    model: str,
    question: str,
    sent: tuple[RedactedSend, ...],
    is_external: dict[str, bool],
) -> _ClaimsEnvelope:
    """Make the structured-output call and return the decoded claims envelope.

    Uses ``messages.parse`` with an ``output_format`` Pydantic model so the SDK
    validates the response against the claims schema and returns a typed instance
    (``docs/stack.md`` "structured outputs + Pydantic").
    """
    sources = "\n\n".join(
        _render_source(send, is_external.get(send.target_id, False)) for send in sent
    )
    user_prompt = f"QUESTION:\n{question}\n\nSOURCES:\n{sources}"
    response = client.messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        output_format=_ClaimsEnvelope,
    )
    return response.parsed_output


def _render_source(send: RedactedSend, is_external: bool) -> str:
    """Render one cleared, redacted passage as a citable source block."""
    kind = "external" if is_external else "note"
    return f'<source id="{send.target_id}" kind="{kind}">\n{send.text}\n</source>'
