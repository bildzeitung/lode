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
5**, :data:`OPUS_MODEL`) -- both ``Kind.RUNTIME`` knobs in :mod:`lode.config`,
each a :class:`~lode.llm_provider.ModelTier` (model + reasoning_effort +
max_tokens; lode-568v.2, lode-d70n), so a user override actually reaches the
call. The call itself is
routed through the vendor-neutral :class:`~lode.llm_provider.LLMProvider` seam
(:func:`~lode.llm_provider.build_provider`) rather than a hardcoded Anthropic
client -- credentials still resolve via the SDK's own chain underneath, never a
hardcoded key.

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

**lode-35nu.11.6:** the call is routed through
:meth:`~lode.llm_provider.LLMProvider.run_tool_turns` with an empty ``tools``
list -- byte-for-byte identical to the direct :meth:`structured_call` this
replaced (every provider's empty-``tools`` case is required to delegate
straight to it). That ticket wired no tools in on its own.

**lode-8hsk:** ``tools_enabled=True`` now wires real tools in --
:func:`lode.tool_dispatch.build_ask_tools`' read-only search/fetch set --
through the same seam, with no reshape. The system prompt's tool-awareness is
derived from whether the resulting ``tools`` tuple is non-empty, never from a
second flag: :data:`_SYSTEM_PROMPT` (notes-only) is sent whenever ``tools``
ends up empty -- either ``tools_enabled=False`` or
``settings.ask_tools_enabled=False`` collapses to that same empty tuple --
and stays byte-for-byte what it was before this ticket, so that path is
unchanged. :data:`_SYSTEM_PROMPT_WITH_TOOLS` is sent only when ``tools`` is
non-empty.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from lode.answer import Answer, Claim
from lode.config import Settings
from lode.egress import RedactedSend, WithheldCitation, gate_qa_egress
from lode.llm_provider import LLMProvider, ToolSpec, build_provider
from lode.tool_dispatch import ToolBudget, build_ask_tools, make_tool_result
from lode.webfetch import Fetcher

#: Default Q&A model -- Claude Sonnet 4.6 (``docs/stack.md`` "Q&A LLM"). Mirrors
#: :attr:`lode.config.Settings.qa_llm`'s default; the live value always comes
#: from settings, never this constant directly (lode-obms).
SONNET_MODEL = "claude-sonnet-4-6"
#: "Think harder" toggle -- Claude Opus 5 (``docs/stack.md`` "Q&A LLM").
#: Mirrors :attr:`lode.config.Settings.qa_think_harder_llm`'s default; the live
#: value always comes from settings, never this constant directly (lode-obms).
OPUS_MODEL = "claude-opus-5"

#: Output cap for the synthesis call; claims are a compact, bounded structure,
#: not long prose. Raised 4096 -> 8192 (lode-3dlt) to give headroom for adaptive
#: thinking to share this budget with the claims response: an explicit
#: ``thinking={"type": "disabled"}`` used to guarantee zero thinking tokens
#: here, but that value 400s on Fable-class models at any effort and on Opus 5
#: at effort xhigh/max (lode-3dlt), so
#: :class:`~lode.llm_provider.AnthropicProvider` no longer sends it at all --
#: Sonnet 4.6 (the ``qa_llm`` default) is unaffected (no thinking when the
#: param is omitted), while Opus 5 (``qa_think_harder_llm`` default) and any
#: Fable-class override now run adaptive thinking.
#:
#: What bounds this call in practice is
#: :attr:`~lode.config.Settings.qa_call_timeout_s` (300s), NOT the Anthropic
#: SDK's non-streaming guard: that guard is skipped outright whenever an
#: explicit ``timeout`` is passed, and the provider seam always passes one. (Its
#: threshold is also ~21K output tokens for the models lode uses, not the ~16K
#: once claimed here -- see ``_calculate_nonstreaming_timeout``.) So this value
#: is headroom, not a hard truncation guarantee; exhausting **this budget**
#: raises :class:`~lode.llm_provider.LLMProviderError` from the provider rather
#: than yielding a malformed answer. See that class's docstring for the full
#: rationale.
#:
#: Raising this cap while also allowing adaptive thinking pushes wall-clock up
#: on the think-harder path twice over. ``qa_call_timeout_s`` (lode-wfyx) is
#: the call's own timeout knob, split off the shared
#: :attr:`~lode.config.Settings.enrich_call_timeout_s` (which still bounds
#: only ``enrich.py``'s calls) specifically because 120s was no longer enough
#: headroom once thinking could share this budget. Its default is **derived,
#: not a measured p95** -- a live-API p95 benchmark was deliberately declined
#: on cost/value grounds, not skipped for lack of capability. The derivation,
#: why SDK retry-on-timeout (``max_retries=2`` default) was left uncapped, and
#: the fact that a ``max_tokens`` override below invalidates the whole
#: derivation all live in ONE place, deliberately not restated here:
#: ``docs/configuration.md`` "Q&A call timeout split from llm_call_timeout_s"
#: (lode-wfyx).
#:
#: Exhausting ``qa_call_timeout_s`` raises a **raw**
#: ``anthropic.APITimeoutError``, not an
#: :class:`~lode.llm_provider.LLMProviderError`: the seam wraps
#: ``APIStatusError`` and pydantic ``ValidationError``, but deliberately not
#: the non-status errors.
#:
#: **This is the fallback, not the last word (lode-d70n):** the active tier's
#: :attr:`~lode.llm_provider.ModelTier.max_tokens` overrides this constant
#: when set, resolved in :func:`answer_question` through
#: :meth:`~lode.llm_provider.ModelTier.resolve_max_tokens`. See
#: ``docs/configuration.md`` "Models" for the decision.
MAX_TOKENS = 8192

#: Notes-only prompt (no tools offered). Byte-for-byte unchanged from before
#: lode-8hsk, asserted by test_qa.py -- the tools_enabled=False /
#: ask_tools_enabled=False path must reproduce today's behaviour exactly.
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

#: Tool-aware prompt, sent only when a non-empty ``tools`` tuple is offered
#: (lode-8hsk). Keeps the verbatim-span rule and the never-from-model-
#: knowledge rule intact -- the faithfulness gate downstream is unmodified
#: and must still pass -- while permitting the one path _SYSTEM_PROMPT
#: forbade: calling a tool, and citing what it returns.
_SYSTEM_PROMPT_WITH_TOOLS = (
    "You answer questions from the SOURCES provided, which are passages from "
    "the user's personal knowledge base, and from the read-only search/fetch "
    "tools available to you. Return a list of factual claims. Every claim "
    "must carry the verbatim evidence it rests on, pinned to a specific "
    "source:\n"
    '- for a source with kind="note", cite its id in the support\'s version_id '
    "field;\n"
    '- for a source with kind="external", cite its id in the support\'s '
    "snapshot_id field;\n"
    "- a snapshot_id returned by a fetch tool call is also a legitimate "
    "citation target for the support's snapshot_id field -- it is verified "
    "against the fetched content the same way any other external is;\n"
    "- set exactly one of version_id or snapshot_id per support, never both.\n"
    "Each quoted_span must be copied verbatim -- character for character -- "
    "from that source's text, or from a tool result you fetched. Assert only "
    "what the sources, or a tool result you fetched, support. If the SOURCES "
    "provided do not answer the question, you may call a search tool to find "
    "a relevant identifier and then fetch it before answering. If, after "
    "that, neither the sources nor anything you fetched answers the "
    "question, return no claims. Never use knowledge beyond what the sources "
    "or your tool results actually show."
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
    ``Support.body_offset`` is app-side only and is dropped from the generated
    schema by ``SkipJsonSchema`` on the field itself (lode-9nmk), so nothing
    here has to mirror or strip it.
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
    tools_enabled: bool = False,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
    jira_fetcher: Fetcher | None = None,
    confluence_fetcher: Fetcher | None = None,
    web_fetcher: Fetcher | None = None,
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

    ``tools_enabled`` (lode-8hsk) offers :func:`lode.tool_dispatch.build_ask_tools`'
    read-only search/fetch tool set through
    :meth:`~lode.llm_provider.LLMProvider.run_tool_turns`' free-tool-turn loop --
    ``build_ask_tools`` itself still returns ``()`` when
    ``settings.ask_tools_enabled`` is ``False``, so a caller passing
    ``tools_enabled=True`` against a config with the feature flag off gets the
    unchanged notes-only path regardless (module docstring). The per-ask
    tool-call budget (``settings.ask_tool_budget``) is created fresh per call.
    ``jira_fetcher``/``confluence_fetcher``/``web_fetcher`` are test/offline
    seams passed straight through to :func:`~lode.tool_dispatch.make_tool_result`.

    ``settings`` defaults to :class:`~lode.config.Settings`' own defaults when
    omitted (same pattern as :func:`lode.redact.redact_before_egress_counting`).
    ``provider`` defaults to a credential-resolved
    :class:`~lode.llm_provider.LLMProvider`
    (:func:`~lode.llm_provider.build_provider`); tests pass a mock so the gates
    stay offline.
    """
    settings = settings or Settings()
    tier = settings.qa_think_harder_llm if think_harder else settings.qa_llm
    model = tier.model
    passages = list(passages)
    # Keep note-vs-external per target so the prompt can tell Claude which support
    # field to cite (the gate's RedactedSend carries only target_id + text).
    is_external = {p.target_id: p.is_external for p in passages}

    egress = gate_qa_egress(conn, model, passages, settings)

    tools = build_ask_tools(settings) if tools_enabled else ()

    provider = provider or build_provider(settings)
    envelope = _request_claims(
        provider,
        model,
        tier.reasoning_effort,
        tier.resolve_max_tokens(MAX_TOKENS),
        question,
        egress.sent,
        is_external,
        settings.qa_call_timeout_s,
        tools,
        conn=conn,
        settings=settings,
        jira_fetcher=jira_fetcher,
        confluence_fetcher=confluence_fetcher,
        web_fetcher=web_fetcher,
    )
    return QaResult(
        answer=Answer(envelope.claims),
        withheld_citations=egress.withheld_citations,
        model=model,
        egress_log_id=egress.egress_log_id,
    )


def _no_tools_configured(name: str, tool_input: dict) -> str:  # pragma: no cover
    """``tool_result`` callback used only when ``tools`` is empty.

    Never actually invoked: :meth:`~lode.llm_provider.LLMProvider.run_tool_turns`
    is required to delegate straight to ``structured_call`` when ``tools`` is
    empty, engaging no loop machinery at all (lode-35nu.11.6). This stub exists
    only so a future regression that starts calling it fails loudly rather than
    silently swallowing a tool call.
    """
    raise AssertionError(
        f"unexpected tool call {name!r}({tool_input!r}) -- tools is empty for "
        "this run_tool_turns call, so run_tool_turns should never have reached "
        "a tool_result callback at all (lode-35nu.11.6's empty-tools contract)"
    )


def _request_claims(
    provider: LLMProvider,
    model: str,
    reasoning_effort: str | None,
    max_tokens: int,
    question: str,
    sent: tuple[RedactedSend, ...],
    is_external: dict[str, bool],
    timeout_s: float,
    tools: tuple[ToolSpec, ...],
    *,
    conn: sqlite3.Connection,
    settings: Settings,
    jira_fetcher: Fetcher | None,
    confluence_fetcher: Fetcher | None,
    web_fetcher: Fetcher | None,
) -> _ClaimsEnvelope:
    """Make the structured-output call and return the decoded claims envelope.

    Routed through the :class:`~lode.llm_provider.LLMProvider` seam
    (lode-568v.2) via :meth:`~lode.llm_provider.LLMProvider.run_tool_turns`
    (lode-35nu.11.6). An empty ``tools`` -- every provider's empty-``tools``
    case is required to delegate straight to ``structured_call``
    (``messages.parse`` with an ``output_format`` Pydantic model for
    :class:`~lode.llm_provider.AnthropicProvider`) -- is byte-for-byte
    identical to calling ``structured_call`` directly, as it did before
    lode-35nu.11.6. A non-empty ``tools`` (lode-8hsk) runs the free-tool-turn
    loop instead, dispatching each call through
    :func:`~lode.tool_dispatch.make_tool_result` with a fresh
    :class:`~lode.tool_dispatch.ToolBudget` (``settings.ask_tool_budget``, one
    counter shared by search and fetch). The system prompt is chosen from
    whether ``tools`` is non-empty, never from a separate flag (module
    docstring).
    """
    sources = "\n\n".join(
        _render_source(send, is_external.get(send.target_id, False)) for send in sent
    )
    user_prompt = f"QUESTION:\n{question}\n\nSOURCES:\n{sources}"
    if tools:
        budget = ToolBudget(max_calls=settings.ask_tool_budget)
        tool_result = make_tool_result(
            conn,
            budget,
            settings,
            jira_fetcher=jira_fetcher,
            confluence_fetcher=confluence_fetcher,
            web_fetcher=web_fetcher,
        )
        system = _SYSTEM_PROMPT_WITH_TOOLS
    else:
        tool_result = _no_tools_configured
        system = _SYSTEM_PROMPT
    return provider.run_tool_turns(
        model=model,
        reasoning_effort=reasoning_effort,
        system=system,
        user_prompt=user_prompt,
        tools=tools,
        tool_result=tool_result,
        output_schema=_ClaimsEnvelope,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )


def _render_source(send: RedactedSend, is_external: bool) -> str:
    """Render one cleared, redacted passage as a citable source block."""
    kind = "external" if is_external else "note"
    return f'<source id="{send.target_id}" kind="{kind}">\n{send.text}\n</source>'
