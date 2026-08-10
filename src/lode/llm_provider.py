"""The vendor-neutral LLM provider seam (lode-568v.2, design pinned lode-568v.1).

Introduces the single abstraction every cloud-LLM call site now goes through --
``docs/stack.md`` "LLM provider seam" is the pinned design this module builds
against exactly; read that first for the *why*. This module owns the *what*:

- :class:`ModelTier` -- a per-surface ``(model, reasoning_effort)`` pair,
  replacing a bare model-id string on ``Settings`` (``enrichment_llm`` /
  ``qa_llm`` / ``qa_think_harder_llm``).
- :class:`LLMProvider` -- a ``Protocol`` (structural typing, matching this
  repo's existing :class:`lode.embedding.Embedder` precedent) covering all
  three cloud-LLM call surfaces: one generic immediate structured-output
  method (:meth:`LLMProvider.structured_call`, serving both the enrichment
  forced-tool-use call and the Q&A ``messages.parse`` call) plus the two-phase
  batch contract (:meth:`LLMProvider.submit_batch` / :meth:`LLMProvider.collect_batch`).
- :class:`AnthropicProvider` -- the sole implementation for now (T2,
  lode-568v.2), mapping onto today's exact Anthropic calls with **zero
  behavior change** (byte-for-byte on the wire).
- :func:`build_provider` -- resolves ``settings.llm_provider`` to a concrete
  provider, replacing :func:`lode.auth.build_client` as the construction seam
  every call site now uses.

**Implementation details left open by the pinned design, resolved here (T2):**

- ``structured_call`` gains a ``tool_description`` keyword beyond the pinned
  signature -- required for :func:`AnthropicProvider.structured_call`'s forced
  tool-use branch to send the *exact* tool description text
  :func:`lode.enrich._call_haiku` sends today (byte-for-byte wire equivalence
  is this ticket's own acceptance bar; the pinned signature had no way to
  carry it). Same addition on :class:`BatchRequest` for the batch path.
- ``reasoning_effort`` is wired through by :class:`AnthropicProvider` to
  Anthropic's ``output_config.effort`` (GA since the 4.6 generation; ``xhigh``
  added on Opus 4.7) on both the ``messages.parse`` branch and the
  forced-tool-use branch of :meth:`structured_call`, and on the
  :meth:`submit_batch` path (``lode-wnz1``, closing the gap left open by
  ``lode-568v.2``). A value outside the legal set (``low``/``medium``/``high``/
  ``xhigh``/``max``) raises :class:`LLMProviderError` before any request is
  sent, rather than being silently dropped or surfacing as a raw
  ``anthropic.BadRequestError``. That check is on the *value* only, not on the
  value/model *pairing* -- see :class:`AnthropicProvider`'s own docstring for
  what that leaves reachable, and for the ``thinking``-interaction note.
  **lode-90o7** closes the reachable gap that left: the *pairing* is still not
  predicted, but the rejection it produces is now converted to
  :class:`LLMProviderError` on both providers -- see
  :class:`AnthropicProvider`'s docstring for the Anthropic half (and for what
  it deliberately does *not* cover) and :func:`_openai_effort_kwargs` for the
  OpenAI half. Both of those value checks fire at the first API call;
  :data:`EFFORT_LEVELS_BY_PROVIDER` re-exports the two legal sets so
  ``lode.config.Settings`` can reject a typo at config load as well
  (**lode-tvps** -- see that constant's own comment).
- **The batch handle stays the bare Anthropic ``batch.id`` string** (identical
  to ``submit_enrich_batch`` today) -- schema information never needs to
  survive to :meth:`collect_batch` because :class:`BatchResult.parsed` holds
  the **raw** decoded wire payload (via ``pydantic.RootModel[dict]``, which
  literally satisfies the pinned ``BaseModel | None`` type) rather than a
  domain-specific validated model. The caller (:mod:`lode.enrich`) does its
  own ``EnrichmentResult.model_validate(result.parsed.root)`` -- exactly what
  it does today, just fed from the provider's raw payload instead of
  ``tool_block.input`` directly. This is what keeps :class:`AnthropicProvider`
  generic (it never needs to know about :class:`lode.enrich.EnrichmentResult`)
  while preserving the resume-on-restart durability
  :func:`lode.enrich.collect_enrich_batch` depends on (``lode-i05.5`` -- a
  fresh process's freshly-built provider has no in-memory state, only the
  persisted ``batch_handle``).
- **``build_provider``'s Anthropic branch does not wrap a missing-credential
  failure into :class:`LLMAuthError`.** :func:`lode.auth.build_client`'s
  :class:`~lode.auth.AuthError` propagates unchanged -- preserving
  :mod:`lode.worker`'s extensively-tested ``lode-9yy`` permanent-failure
  handling (``except AuthError``) byte-for-byte, rather than requiring every
  one of its catch sites (and the dozens of tests pinning that behavior) to
  also catch :class:`LLMAuthError`. :class:`LLMAuthError` is still defined per
  the pinned contract, for a *future* non-Anthropic provider's own credential
  failures to raise -- see ``docs/decisions.md`` (lode-568v.2) for the full
  rationale and the tracked follow-up (widen the worker's exception handling
  once a second provider exists).

**lode-568v.3 (OpenAI-via-Azure, this ticket):**

- :class:`OpenAIProvider` -- the second implementation, added by this ticket.
  Uses **one** wire mechanism for structured output regardless of ``tool_name``
  -- the Responses API's ``text.format`` ``json_schema`` (``docs/stack.md``
  "2 & 3." -- ``tool_name``/``tool_description`` are Anthropic-mechanism-
  selecting, not a cross-provider requirement; ``OpenAIProvider`` uses
  ``tool_name`` only as the schema's ``name`` field when given). ``strict`` is
  deliberately **not** set true: Structured Outputs' strict mode requires
  every object in the schema to set ``additionalProperties: false`` and list
  every property as ``required`` (optional fields must be modeled as
  nullable) -- a transformation ``pydantic``'s own ``model_json_schema()``
  does not perform, so asserting strict-mode compliance without transforming
  the schema first would be exactly the kind of wire-shape assumption the
  epic's own challenge review flagged as this ticket's highest risk (see
  ``docs/decisions.md`` lode-568v.3). Non-strict json_schema mode is
  best-effort on the API's part; :meth:`OpenAIProvider.structured_call` then
  validates the returned JSON against ``output_schema`` itself via
  ``model_validate`` regardless, which is the actual conformance check either
  way once a domain-typed result is needed. **This provider's correctness
  rests entirely on mocked Responses-API response shapes (no live Azure
  endpoint was available to verify against) -- see the module's
  ``lode-568v.3`` decisions.md entry for the named risk and the
  diagnostic-logging compensating control.**
- ``build_provider``'s ``"openai"`` branch resolves ``OPENAI_API_KEY`` (direct
  OpenAI) or, when ``settings.azure_openai_endpoint`` is set,
  ``AZURE_OPENAI_API_KEY`` + the endpoint/api-version routing knobs, and DOES
  wrap a missing credential into :class:`LLMAuthError` -- unlike the Anthropic
  branch above, there is no existing exception type to preserve here
  (``lode-568v.2``'s tracked follow-up). :mod:`lode.worker`'s three
  ``except AuthError`` sites are widened to ``except (AuthError,
  LLMAuthError)`` by this ticket so a missing OpenAI/Azure credential gets the
  same permanent-failure (no retry, no dead-letter) treatment
  ``lode-9yy`` already gives a missing Anthropic credential.

**lode-35nu.11.6 (multi-turn tool-use):**

- :class:`ToolSpec` + :meth:`LLMProvider.run_tool_turns` -- the seam's second
  call surface, alongside :meth:`structured_call`: N free tool turns (the
  model may call any of ``tools``, resolved via a caller-supplied
  ``tool_result`` callback) followed by exactly one final FORCED-schema turn,
  since free tool choice and a forced ``output_schema`` are mutually
  exclusive within one call. This ticket adds the mechanism only -- it
  defines no tool schemas (lode-35nu.11.2's job) and :mod:`lode.qa` calls it
  with ``tools=()`` for now, so every provider's empty-``tools`` case MUST
  delegate straight to :meth:`structured_call` -- the acceptance bar is
  byte-for-byte unchanged behavior on every existing single-shot call site.
- **Provider parity, settled here: Anthropic-only.**
  :class:`AnthropicProvider.run_tool_turns` implements the real loop
  (``messages.create`` with ``tool_choice={"type": "auto"}``, tool results fed
  back as ``tool_result`` content blocks, then a forced final turn).
  :class:`OpenAIProvider.run_tool_turns` implements only the degenerate
  empty-``tools`` delegation; a non-empty ``tools`` raises
  :class:`LLMProviderError` rather than silently ignoring the tools and
  answering ungrounded. Rationale: the Responses API's function-calling shape
  differs enough from :class:`OpenAIProvider.structured_call`'s
  ``text.format`` json_schema mechanism (a genuine second implementation, not
  a mechanical port), and this module's own OpenAIProvider was already built
  and tested against mocked shapes with no live Azure endpoint available
  (module docstring, "lode-568v.3") -- adding an unverified multi-turn
  function-calling implementation on top compounds that risk for a path
  nothing calls yet. Revisit once lode-35nu.11.2 defines real tools and an
  OpenAI/Azure user actually needs this path; full rationale in
  ``docs/stack.md`` "LLM provider seam" / lode-35nu.11.6.
- ``timeout_s`` is a budget for the whole **run**, not one call each: the
  deadline is set once and each turn is sent only what remains, so a run
  cannot outlive it however many turns it takes. ``max_tokens`` is **not**
  decremented across turns -- it stays what it has always been, a per-response
  output cap, applied to each turn. That asymmetry is a DECIDED design choice
  (maintainer, lode-3dh1), not a gap left over from lode-35nu.11.6's
  acceptance wording: decrementing ``max_tokens`` against each turn's
  ``usage.output_tokens`` was considered and rejected, because it silently
  shrinks the budget left for the final forced-schema turn and can truncate
  the answer. Total output spend for a run is instead bounded by the turn
  count (``_DEFAULT_MAX_TOOL_TURNS``) -- see ``docs/stack.md`` "LLM provider
  seam" / lode-3dh1 for the full write-up, and ``docs/decisions.md``
  (lode-csl2) for the deferred, additive ``max_output_tokens_per_run`` knob.
  ``AnthropicProvider.run_tool_turns`` logs each completed run's total
  output-token spend next to that bound, so the GAP is readable without
  arithmetic -- the evidence lode-csl2's deferral trigger waits on
  (lode-9594; ``docs/decisions.md``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    model_validator,
)

from lode.auth import build_client

if TYPE_CHECKING:
    import anthropic
    import openai

    from lode.config import Settings

BaseModelT = TypeVar("BaseModelT", bound=BaseModel)

_log = logging.getLogger(__name__)


class ModelTier(BaseModel):
    """A per-surface model/effort/budget tier (``docs/stack.md`` "Config shape").

    ``model`` is an Anthropic model id, or an Azure/OpenAI deployment name
    once a second provider lands (``lode-568v.3``). ``reasoning_effort`` is
    meaningful only under a reasoning-capable deployment; :class:`AnthropicProvider`
    sends it as ``output_config.effort`` (``lode-wnz1``) and
    :class:`OpenAIProvider` sends it as ``reasoning.effort``.

    ``max_tokens`` is an optional per-tier override of the output-budget
    constant each call site otherwise falls back to
    (:data:`lode.qa.MAX_TOKENS`, :data:`lode.enrich.MAX_TOKENS`), resolved
    through :meth:`resolve_max_tokens`: ``None`` means "use the call site's
    own default", and a set value must be positive. ``docs/configuration.md``
    "Models" owns the rationale and the truncation-vs-cost tradeoff a lower
    override makes (``lode-d70n``).

    A bare TOML string (every existing ``config.toml`` today, e.g.
    ``enrichment_llm = "claude-haiku-4-5"``) coerces to
    ``ModelTier(model=<string>)`` -- back-compat, no migration required.
    """

    model_config = ConfigDict(frozen=True)

    model: str
    reasoning_effort: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _coerce_bare_string(cls, data: object) -> object:
        if isinstance(data, str):
            return {"model": data}
        return data

    def resolve_max_tokens(self, default: int) -> int:
        """This tier's :attr:`max_tokens` if set, else ``default`` (lode-d70n).

        The one home for the "unset means the call site's own constant" rule,
        on the type that owns the field -- so the Q&A call and both enrichment
        routes cannot drift apart on it (the byte-for-byte wire-equivalence
        bar :data:`lode.enrich.MAX_TOKENS` is itself pinned by, lode-568v.2).
        """
        return self.max_tokens if self.max_tokens is not None else default


# ---------------------------------------------------------------------------
# Error contract (docs/stack.md "Error contract -- diagnosability over genericness")
# ---------------------------------------------------------------------------


class LLMProviderError(RuntimeError):
    """A provider call failure. Carries enough to diagnose remotely.

    Chain the underlying SDK exception via ``raise ... from exc`` so
    ``__cause__`` still exposes it for debugging.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.request_id = request_id


class LLMAuthError(LLMProviderError):
    """No credentials resolved for the active provider -- raised by :func:`build_provider`.

    Not raised by :class:`AnthropicProvider`'s construction path in this
    ticket (T2) -- see the module docstring's "implementation details"
    section. Reserved for a future non-Anthropic provider's own credential
    failures.
    """


# ---------------------------------------------------------------------------
# The two-phase batch contract (docs/stack.md "4. The two-phase batch contract")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchRequest:
    """One request in a submitted batch -- mirrors :class:`lode.enrich.EnrichmentResult`'s
    per-version-id request shape, generalized across providers.

    ``tool_description`` is an addition beyond the pinned shape (see module
    docstring) -- carries the exact tool description text so
    :class:`AnthropicProvider` can force tool-use byte-for-byte identical to
    today's hand-built request dict.
    """

    custom_id: str
    model: str
    reasoning_effort: str | None
    system: str
    user_prompt: str
    output_schema: type[BaseModel]
    max_tokens: int
    tool_name: str | None = None
    tool_description: str | None = None


@dataclass(frozen=True)
class BatchResult:
    """One result from a collected batch.

    ``parsed`` holds the **raw** decoded wire payload (a ``pydantic.RootModel``
    wrapping a plain ``dict``) on success -- never a schema-validated domain
    object; see the module docstring for why. The caller validates it against
    whatever schema it submitted.
    """

    custom_id: str
    outcome: Literal["succeeded", "errored", "expired", "canceled"]
    parsed: BaseModel | None  # set iff outcome == "succeeded"; a RootModel[dict]
    error: LLMProviderError | None  # set iff outcome != "succeeded"


#: Default free-tool-turn cap for :meth:`LLMProvider.run_tool_turns`
#: (lode-35nu.11.6). One constant rather than the literal repeated across the
#: Protocol and both implementations, which could otherwise drift so that a
#: caller reading the Protocol signature is wrong about the actual budget.
_DEFAULT_MAX_TOOL_TURNS = 8


@dataclass(frozen=True)
class ToolSpec:
    """One tool the model may call during a :meth:`LLMProvider.run_tool_turns` run.

    Deliberately minimal -- just the wire shape a provider needs to offer the
    tool (``name``/``description``/JSON-Schema ``input_schema``). Domain tool
    definitions (JIRA search, web fetch, ...) are lode-35nu.11.2's job, not
    this ticket's; this module never constructs one itself.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


class LLMProvider(Protocol):
    """The vendor-neutral seam every cloud-LLM call site goes through.

    Protocol, not ABC (``docs/stack.md`` -- matches the :class:`lode.embedding.Embedder`
    precedent): structural typing, no shared base class required.
    """

    def structured_call(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        system: str,
        user_prompt: str,
        output_schema: type[BaseModelT],
        max_tokens: int,
        timeout_s: float,
        tool_name: str | None = None,
        tool_description: str | None = None,
    ) -> BaseModelT:
        """One immediate structured-output call; returns a validated ``output_schema``.

        Serves both the enrichment (forced tool-use, ``tool_name`` given) and
        Q&A (``messages.parse``, ``tool_name=None``) surfaces.
        """
        ...

    def run_tool_turns(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        system: str,
        user_prompt: str,
        tools: Sequence[ToolSpec],
        tool_result: Callable[[str, dict[str, Any]], str],
        output_schema: type[BaseModelT],
        max_tokens: int,
        timeout_s: float,
        tool_name: str | None = None,
        tool_description: str | None = None,
        max_tool_turns: int = _DEFAULT_MAX_TOOL_TURNS,
    ) -> BaseModelT:
        """N free tool turns, then one final FORCED-schema turn (lode-35nu.11.6).

        Free tool choice and a forced structured-output schema are mutually
        exclusive within a single call, so a run is: up to ``max_tool_turns``
        turns where the model may freely call a tool from ``tools`` (each
        ``tool_result(name, input)`` call resolves one, its return value fed
        back as the tool result, until the model stops calling tools or the
        turn budget is exhausted), followed by exactly one final call with
        tool choice forced to ``output_schema`` -- identical in spirit to
        :meth:`structured_call`'s forced-tool-use branch, but continuing the
        accumulated conversation instead of a single user turn.

        ``timeout_s`` is a budget for the **whole run**, not one call each
        (``docs/configuration.md``): the deadline is set once and each turn is
        sent only the remaining wall clock. ``max_tokens`` is **not** spread
        across turns -- it stays a per-response output cap, applied to each
        turn, so a run may emit up to ``(max_tool_turns + 1) x max_tokens``
        output tokens -- the ``+ 1`` being the final forced-schema turn, which
        is spent after the free-turn loop and is not covered by
        ``max_tool_turns``. That asymmetry is a DECIDED design choice
        (maintainer, lode-3dh1), not an open gap: total output spend for a run
        is bounded by the turn count rather than by decrementing
        ``max_tokens``. See the module docstring and ``docs/stack.md`` for the
        full rationale.

        **Degenerate case, byte-for-byte (acceptance bar):** when ``tools``
        is empty, a provider MUST delegate straight to :meth:`structured_call`
        with the same arguments -- no loop machinery engages, so every
        existing single-shot call site (:mod:`lode.enrich`, and
        :mod:`lode.qa` until lode-35nu.11.2 wires real tools in) is
        unaffected in behavior by this method's existence.
        """
        ...

    def submit_batch(
        self, requests: Sequence[BatchRequest], *, timeout_s: float
    ) -> str:
        """Submit; return an opaque, PERSISTABLE handle string (stored as ``batch_handle``)."""
        ...

    def collect_batch(
        self, handle: str, *, timeout_s: float
    ) -> tuple[Literal["pending"], None] | tuple[Literal["ended"], list[BatchResult]]:
        """Poll ``handle``; ``("pending", None)`` or ``("ended", <results>)``."""
        ...


# ---------------------------------------------------------------------------
# AnthropicProvider (docs/stack.md -- "AnthropicProvider maps onto today's
# exact calls with zero behavior change")
# ---------------------------------------------------------------------------

# The legal `output_config.effort` values (lode-wnz1), in intensity order --
# mirrors Anthropic's
# `OutputConfigParam.effort: Literal["low", "medium", "high", "xhigh", "max"]`
# exactly, including order. `xhigh` was added on Opus 4.7; all five are GA.
# `test_effort_levels_match_the_installed_sdk_literal` pins this tuple to the
# installed SDK's own `Literal` so the claim cannot silently go stale -- the
# ladder has already grown once, and a sixth level shipping upstream would
# otherwise make lode reject a legal value.
_ANTHROPIC_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _anthropic_effort_kwargs(
    reasoning_effort: str | None, *, model: str
) -> dict[str, dict[str, str]]:
    """Build the ``output_config`` **kwarg fragment** for ``reasoning_effort``.

    Returns ``{"output_config": {"effort": ...}}`` when set and an empty dict
    when unset, so every call site can splat it unconditionally. Returning the
    fragment rather than the value is what makes "omitted, never ``null``"
    structural: there is no code path that can put ``output_config=None`` on
    the wire, and ``None`` is what the SDK would serialize as an explicit
    ``null`` (its ``is_given`` treats ``None`` as given -- only an absent kwarg
    is dropped from the request body).

    Validates against :data:`_ANTHROPIC_EFFORT_LEVELS` so an invalid value
    fails clearly and immediately -- via :class:`LLMProviderError` -- rather
    than being silently dropped or surfacing as a raw
    ``anthropic.BadRequestError`` deep inside the SDK call (lode-wnz1
    acceptance criteria). This checks the *value*, not the value/``model``
    pairing; ``model`` is carried only to name the offending tier in the error.
    See :class:`AnthropicProvider`'s docstring for what the pairing gap leaves
    reachable.
    """
    if reasoning_effort is None:
        return {}
    if reasoning_effort not in _ANTHROPIC_EFFORT_LEVELS:
        raise LLMProviderError(
            f"invalid reasoning_effort {reasoning_effort!r} for model {model!r} "
            f"-- Anthropic's output_config.effort accepts one of "
            f"{list(_ANTHROPIC_EFFORT_LEVELS)}",
            provider="anthropic",
        )
    return {"output_config": {"effort": reasoning_effort}}


#: Shared tail for the three "the block I need isn't in ``content``" errors
#: (:meth:`AnthropicProvider.structured_call`'s two branches and
#: :meth:`AnthropicProvider.collect_batch`). All three have the same cause --
#: thinking shares ``max_tokens`` with the payload, see
#: :class:`AnthropicProvider` -- so they read identically in a log; single-
#: sourced here because they are far apart and a reword would otherwise drift.
_BUDGET_EXHAUSTED_HINT = "-- typically the whole output budget was consumed by thinking"

#: Same three call sites, but for ``stop_reason="model_context_window_exceeded"``
#: (anthropic SDK 0.119.0+, lode-cai6) -- a distinct cause from the output-budget
#: exhaustion above: the *input* (system + user_prompt + tool results so far)
#: no longer fits the model's context window, so growing ``max_tokens`` cannot
#: help. Names the actionable knob: shrink what's being sent, not the output
#: budget.
_CONTEXT_WINDOW_EXCEEDED_HINT = (
    "-- the input (prompt + retrieved context) exceeded the model's context "
    "window; shrink the retrieved context or user_prompt, not max_tokens"
)


def _budget_hint(stop_reason: str | None) -> str:
    """Pick the right "why is the block missing" hint for ``stop_reason``.

    ``model_context_window_exceeded`` gets its own diagnostic --
    :data:`_CONTEXT_WINDOW_EXCEEDED_HINT` -- because its remedy (shrink the
    input) is the opposite of the ``max_tokens`` remedy (this function's
    default, :data:`_BUDGET_EXHAUSTED_HINT`, which is about the *output*
    budget). Anything else falls back to the ``max_tokens`` hint, unchanged
    from before this branch existed.
    """
    if stop_reason == "model_context_window_exceeded":
        return _CONTEXT_WINDOW_EXCEEDED_HINT
    return _BUDGET_EXHAUSTED_HINT


def _output_tokens(response: Any) -> int:
    """``response.usage.output_tokens`` as an int, 0 when absent or not an int.

    Total by construction (lode-9594): measurement must never break a run,
    and an accessor that cannot raise is how that is guaranteed here -- so
    there is no ``except`` for an accounting bug to hide in.
    """
    value = getattr(getattr(response, "usage", None), "output_tokens", 0)
    return value if isinstance(value, int) else 0


def _anthropic_error_from_exception(
    exc: anthropic.APIStatusError, *, context: str
) -> LLMProviderError:
    """Wrap an Anthropic SDK 4xx/5xx into :class:`LLMProviderError` (lode-90o7, lode-i7yr).

    Used at all five ``AnthropicProvider`` SDK call sites: the three that
    *submit* a request -- ``messages.create``, ``messages.parse``,
    ``batches.create`` -- and the two that *poll* an existing batch in
    :meth:`AnthropicProvider.collect_batch` -- ``batches.retrieve``,
    ``batches.results``. Same "log + wrap, preserve status_code/request_id"
    shape :meth:`OpenAIProvider._error_from_exception` already uses for the
    equivalent ``openai.APIStatusError``. ``context`` names what was being
    attempted.

    Most reachably this converts the 400 that :func:`_anthropic_effort_kwargs`
    cannot predict -- ``reasoning_effort`` set to a legal *value* on a tier
    whose *model* does not support it (e.g. any effort on Haiku 4.5/Sonnet
    4.5, or ``xhigh``/``max`` below Opus 4.7) -- into a diagnosable seam-level
    error instead of a raw SDK exception escaping past code that only expects
    :class:`LLMProviderError`. That one is submit-only; see
    :meth:`AnthropicProvider.collect_batch` for why the polling pair is
    wrapped too.

    **Not** the non-status ``anthropic.APIError`` subclasses
    (``APITimeoutError``, ``APIConnectionError``): a timeout is not a
    rejected request, and :data:`lode.qa.MAX_TOKENS`'s note documents it
    surfacing raw today. Nor a failure raised while *streaming* a batch's JSONL
    results: that is never an ``anthropic.APIStatusError``, so it cannot go
    through this helper at all -- :meth:`AnthropicProvider.collect_batch` wraps
    it separately (lode-3gtu).
    """
    _log.error(
        "Anthropic call failed (%s, status_code=%s request_id=%s): %s",
        context,
        exc.status_code,
        exc.request_id,
        exc,
    )
    return LLMProviderError(
        f"Anthropic call failed ({context}): {exc}",
        provider="anthropic",
        status_code=exc.status_code,
        request_id=exc.request_id,
    )


#: Stands in for a batch-results line's ``custom_id`` when the line's own is
#: missing/``None``/not a non-empty ``str`` (lode-i821). Deliberately not a
#: plausible version id, so a ``job_map`` lookup misses rather than colliding.
_UNKNOWN_CUSTOM_ID = "<unknown>"


def _result_custom_id(result: Any) -> str:
    """Read one batch-results line's ``custom_id``, honoring the declared type.

    Sole owner of the invariant that **every** :class:`BatchResult` this
    module returns carries a non-empty ``str`` ``custom_id``, matching the
    declared ``custom_id: str`` -- consumers may rely on that without a
    ``None`` check. ``custom_id`` comes off the same
    ``construct_type_unchecked`` model as every other field, so the wire can
    make it absent, ``None``, or the wrong type; reading it never *raises* in
    the loop body, so it would otherwise break the contract silently, one
    seam downstream in the consumer.

    Every branch goes through here, not just the wrong-shape ones -- a line
    whose ``result`` block is well-formed and whose ``custom_id`` alone is
    missing takes the ordinary *succeeded* (or ``errored``/no-``tool_use``)
    branch and never touches :func:`_wrong_shape_result`. ``docs/stack.md``
    "Error contract" owns the reasoning and the routing consequence.
    """
    custom_id = getattr(result, "custom_id", None)
    return custom_id if isinstance(custom_id, str) and custom_id else _UNKNOWN_CUSTOM_ID


def _wrong_shape_result(result: Any, detail: str) -> BatchResult:
    """Degrade one well-formed-but-wrong-shape batch-results line (lode-i821).

    ``construct_type_unchecked`` (anthropic's ``jsonl.py``) deliberately does
    not validate a decoded line's shape, so a missing/malformed field on it
    surfaces as a raw ``AttributeError``/``TypeError``/``ValidationError``
    from :meth:`AnthropicProvider.collect_batch`'s loop BODY rather than a
    caught SDK exception -- ``docs/stack.md`` "Error contract" owns the
    inventory. Every call site here degrades the *one* item to an
    ``errored`` :class:`BatchResult` rather than failing the whole
    collection, matching the pre-existing "no tool_use block" treatment.

    ``result.custom_id`` may itself be the field that's missing/malformed --
    it comes from the same unvalidated line -- so it is read via
    :func:`_result_custom_id`, which owns the placeholder substitution that
    keeps :class:`BatchResult.custom_id`'s declared ``str`` honest.
    """
    return BatchResult(
        custom_id=_result_custom_id(result),
        outcome="errored",
        parsed=None,
        error=LLMProviderError(
            f"wrong-shape batch result line ({detail})", provider="anthropic"
        ),
    )


class AnthropicProvider:
    """Wraps today's ``anthropic.Anthropic`` client; the sole provider (T2).

    Every method reproduces today's exact wire mechanism -- forced tool-use
    for enrichment, ``messages.parse`` for Q&A, ``beta.messages.batches.*``
    for the batch path -- so routing through this seam is byte-for-byte
    equivalent to calling the SDK directly, as it was before this ticket.

    **Thinking is never explicitly disabled on the ``messages.parse`` branch
    (lode-3dlt, superseding lode-d1sr's ``thinking={"type": "disabled"}``
    pin).** This is the single source of truth for that decision; the call
    sites below and :data:`lode.qa.MAX_TOKENS` point here rather than restate
    it.

    lode-d1sr pinned ``thinking={"type": "disabled"}`` on this branch because
    Anthropic models from Opus 5 onward run adaptive thinking when ``thinking``
    is omitted, sharing ``max_tokens`` between thinking and response text --
    which could let a cap sized for claims alone (:data:`lode.qa.MAX_TOKENS`)
    truncate mid-answer. But an explicit ``disabled`` is **not** universally
    accepted: Fable-class models (``claude-fable-5``, ``claude-mythos-5``)
    reject it with a 400 at any effort level, and Opus 5 itself rejects the
    combination of ``disabled`` with effort ``xhigh``/``max``. Both
    ``qa_llm``/``qa_think_harder_llm`` are ``Kind.RUNTIME``, so a user override
    to either shape hit an unhandled ``anthropic.BadRequestError`` (lode-3dlt).

    The fix is to never send ``disabled`` at all -- omit ``thinking`` entirely,
    for every model, with no model-family branching. This works because
    disabling thinking is not just illegal on some tiers, it is the
    *disfavoured* setting even where it IS legal: Anthropic's current guidance
    for Opus 5 prefers thinking on at low/medium effort over disabled (two
    failure modes of disabled thinking -- tool calls emitted as plain text, and
    ``<thinking>`` tag leakage -- neither applies badly here: this branch sends
    no tools, and the schema-constrained ``output_format`` contains leakage).
    :data:`lode.qa.MAX_TOKENS` was raised accordingly to give the now-possible
    adaptive thinking headroom to share with the claims response. Net effect:
    Sonnet 4.6 (``qa_llm`` default) is unaffected -- it does not think when
    ``thinking`` is omitted, matching pre-lode-d1sr behavior. Opus 5
    (``qa_think_harder_llm`` default) now runs adaptive thinking instead of
    disabled, a deliberate change. Fable-class overrides now work.

    The raised cap is headroom, not a guarantee, so this branch now also
    *handles* running out of it. Thinking shares ``max_tokens`` with the answer
    text, and exhausting the budget shows up two ways: a response whose
    ``content`` holds only a thinking block (the SDK's ``parsed_output`` is then
    ``None``, which would otherwise escape under this method's ``-> BaseModelT``
    annotation and fail as an ``AttributeError`` in the caller), or a text block
    truncated mid-JSON (a raw ``pydantic.ValidationError`` from inside the SDK).
    Both are converted to :class:`LLMProviderError` -- the same contract
    :class:`OpenAIProvider` already honors for its equivalent shapes -- so the
    failure is diagnosable at the seam instead of surfacing far from its cause.

    *The forced tool-use branch never needed the Fable-class-400 fix* -- it
    has never sent ``thinking`` at all (lode-d1sr never touched it), so it
    already followed the "never explicitly disable" rule before this class
    existed; no Fable-class 400 is reachable there. That is a property of the
    enrichment tier's *default* (``enrichment_llm`` = Haiku 4.5 predates
    thinking-on-by-default), not of forced tool use itself -- on the
    first-party Claude API a forced ``tool_choice`` does not preclude thinking
    (only Amazon Bedrock requires an explicit ``disabled`` alongside it), so a
    ``Kind.RUNTIME`` override to a thinking-capable model runs adaptive
    thinking here too, sharing ``max_tokens`` with the tool-call JSON --
    lode-3dlt tracked this as a real but then-unreachable risk rather than
    fixing it. **lode-jgus closes it:** :data:`lode.enrich.MAX_TOKENS` was
    raised (1024 -> 2048) for the same headroom reason
    :data:`lode.qa.MAX_TOKENS` was, and the branch below now guards the
    symptom of running out of that budget -- a response whose whole
    ``max_tokens`` was spent on thinking carries no ``tool_use`` block at
    all, which used to escape as a raw ``StopIteration`` from an unguarded
    ``next(...)``. It is now converted to :class:`LLMProviderError`, the same
    treatment :meth:`structured_call`'s other branch gives its own
    "budget spent on thinking" symptom (no text block, below).
    :meth:`collect_batch` reaches the identical symptom by the identical route
    -- ``enrich`` sends the same raised cap through both -- and in fact reaches
    it *more* easily; see :data:`lode.enrich.MAX_TOKENS` for why the batch
    route is bounded differently. It already degraded the one item to an
    ``errored`` :class:`BatchResult` rather than failing the whole collection,
    so no raw ``StopIteration`` ever escaped there; lode-jgus only gave its
    message the same model/``stop_reason`` diagnosis this branch reports.

    **``reasoning_effort`` -> ``output_config.effort`` (lode-wnz1)** on every
    branch below; see :func:`_anthropic_effort_kwargs` for the wiring and
    ``docs/configuration.md`` for the decision. Two standing constraints live
    here because they bind the *call sites*:

    **1. Do not reintroduce an explicit ``thinking={"type": "disabled"}`` on
    either branch.** Opus 5 rejects ``disabled`` paired with effort
    ``xhigh``/``max`` outright (400) -- the same family of incompatibility the
    ``thinking``-omission rule above exists to dodge. Unreachable today only
    because neither branch sends ``thinking`` at all; sending ``effort``
    alongside an omitted ``thinking`` is fine on every tier.

    **2. Effort validation is value-only -- the value/model pairing is NOT
    checked, but an unsupported pairing now fails clean instead of raw
    (lode-90o7).** ``effort`` is not universally accepted: it errors outright
    on Haiku 4.5 and Sonnet 4.5, and ``xhigh``/``max`` do not exist on the 4.6
    generation (``xhigh`` arrived with Opus 4.7). All three tiers are
    ``Kind.RUNTIME`` and two default to affected models -- ``enrichment_llm``
    = Haiku 4.5, ``qa_llm`` = Sonnet 4.6 -- so e.g.
    ``enrichment_llm = {model = "claude-haiku-4-5", reasoning_effort = "low"}``
    reaches the API and gets rejected, where before this change that would
    have been a raw ``anthropic.BadRequestError`` escaping the seam. A
    model->capability predicate was deliberately rejected as a moving target
    (lode-3dlt option 1, reaffirmed by lode-90o7), so this is not predicted
    ahead of time; the rejection is instead caught at each of the three
    request-submitting call sites below and converted to
    :class:`LLMProviderError` via :func:`_anthropic_error_from_exception`,
    with ``status_code``/``request_id`` preserved for diagnosis. Setting
    ``reasoning_effort`` still requires pointing the tier at a model that
    supports the level you ask for.
    """

    def __init__(self, client: anthropic.Anthropic) -> None:
        self._client = client

    def _forced_schema_turn(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        output_schema: type[BaseModelT],
        tool_name: str,
        tool_description: str | None,
        max_tokens: int,
        timeout: float,
        effort_kwargs: Mapping[str, Any],
        where: str = "",
    ) -> tuple[BaseModelT, int]:
        """One forced-tool-use ``messages.create`` decoded into ``output_schema``.

        Returns the decoded model and this turn's ``usage.output_tokens``
        (lode-9594) -- :meth:`run_tool_turns` folds the second element into
        its per-run spend total; :meth:`structured_call` discards it.

        The single implementation of Anthropic's forced-schema wire shape,
        shared by :meth:`structured_call`'s ``tool_name is not None`` branch
        (one user turn) and :meth:`run_tool_turns`' final turn (the whole
        accumulated history) -- they differ only in ``messages``, ``timeout``
        and the ``where`` diagnostic suffix, so extracting this keeps
        ``lode-jgus``' missing-``tool_use`` guard, ``lode-90o7``'s
        ``APIStatusError`` wrap and ``lode-wnz1``'s ``effort_kwargs`` on one
        maintained path instead of two near-clones (lode-35nu.11.6 review).

        No ``thinking`` is ever sent here (lode-d1sr): the enrichment tier
        predates thinking-on-by-default. That is a model property, NOT a
        consequence of forced tool use -- see the class docstring.

        """
        import anthropic  # deferred -- lode-4q97; needed by the `except` below

        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=[
                    {
                        "name": tool_name,
                        "description": tool_description or "",
                        "input_schema": output_schema.model_json_schema(),
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
                messages=messages,
                timeout=timeout,
                **effort_kwargs,
            )
        except anthropic.APIStatusError as exc:
            # lode-90o7 -- see `_anthropic_error_from_exception` and the class
            # docstring.
            raise _anthropic_error_from_exception(
                exc, context=f"model={model}{where}"
            ) from exc
        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            # A response that spent its whole budget inside thinking carries no
            # tool_use block at all; unguarded, `next()` with no default raised
            # a raw StopIteration here instead of the LLMProviderError every
            # caller of this seam expects (lode-jgus). Why that is now
            # reachable: the class docstring.
            stop_reason = getattr(response, "stop_reason", None)
            raise LLMProviderError(
                f"Anthropic response contained no tool_use block to decode "
                f"into {output_schema.__name__}{where} (model={model}, "
                f"max_tokens={max_tokens}, "
                f"stop_reason={stop_reason!r}) "
                f"{_budget_hint(stop_reason)}",
                provider="anthropic",
            )
        return output_schema.model_validate(tool_block.input), _output_tokens(response)

    def structured_call(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        system: str,
        user_prompt: str,
        output_schema: type[BaseModelT],
        max_tokens: int,
        timeout_s: float,
        tool_name: str | None = None,
        tool_description: str | None = None,
    ) -> BaseModelT:
        import anthropic  # deferred -- lode-4q97; needed by the `except` below

        # reasoning_effort -> output_config.effort (lode-wnz1): validated up
        # front, then splatted -- empty when unset, so the kwarg is absent
        # rather than `None`. See `_anthropic_effort_kwargs`.
        effort_kwargs = _anthropic_effort_kwargs(reasoning_effort, model=model)
        if tool_name is not None:
            parsed, _output_tokens_spent = self._forced_schema_turn(
                model=model,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
                output_schema=output_schema,
                tool_name=tool_name,
                tool_description=tool_description,
                max_tokens=max_tokens,
                timeout=timeout_s,
                effort_kwargs=effort_kwargs,
            )
            # Single-call path: no multi-turn bound to compare a spend
            # total against, so the count is discarded here (lode-9594).
            return parsed

        # `thinking` is never sent here (lode-3dlt, superseding lode-d1sr's
        # unconditional `disabled` pin) -- an explicit `disabled` 400s on
        # Fable-class models at any effort and on Opus 5 at effort xhigh/max;
        # omitting it lets every model run its own default (no thinking on
        # Sonnet 4.6, adaptive thinking on Opus 5/Fable-class). See the class
        # docstring for the full rationale, and its note on not reintroducing
        # `disabled` alongside `output_config.effort`.
        try:
            # `.parse()` merges an `output_config` we pass with the
            # `output_format`-derived `{"format": ...}` it builds internally, so
            # sending `effort` here does not clobber the schema wiring (the test
            # suite asserts both kwargs reach the SDK).
            response = self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
                output_format=output_schema,
                timeout=timeout_s,
                **effort_kwargs,
            )
        except anthropic.APIStatusError as exc:
            # lode-90o7 -- see the forced-tool-use branch above.
            raise _anthropic_error_from_exception(
                exc, context=f"model={model}"
            ) from exc
        except ValidationError as exc:
            # ``messages.parse`` validates the response text against
            # ``output_schema`` inside the SDK, so a schema violation -- or a
            # text block truncated mid-JSON because thinking ate the budget --
            # surfaces as a raw pydantic error from the SDK's own
            # post-processing. Wrap it: callers of this seam see
            # LLMProviderError, exactly as OpenAIProvider already guarantees.
            raise LLMProviderError(
                f"Anthropic response did not match {output_schema.__name__} "
                f"(model={model}, max_tokens={max_tokens}) -- a response "
                f"truncated mid-JSON is indistinguishable from a genuine "
                f"schema violation here: {exc}",
                provider="anthropic",
            ) from exc

        parsed = response.parsed_output
        if parsed is None:
            # ``parsed_output`` scans ``content`` for a TEXT block and returns
            # None when there is none. A response that spent its whole budget
            # inside thinking carries only a thinking block and
            # ``stop_reason="max_tokens"`` -- reachable precisely because
            # lode-3dlt stopped pinning thinking off. Unguarded, that None
            # escapes under this method's ``-> BaseModelT`` annotation and
            # fails as an AttributeError inside :func:`lode.qa.answer_question`.
            stop_reason = getattr(response, "stop_reason", None)
            raise LLMProviderError(
                f"Anthropic response contained no text block to decode into "
                f"{output_schema.__name__} (model={model}, "
                f"max_tokens={max_tokens}, "
                f"stop_reason={stop_reason!r}) "
                f"{_budget_hint(stop_reason)}",
                provider="anthropic",
            )
        return parsed

    def run_tool_turns(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        system: str,
        user_prompt: str,
        tools: Sequence[ToolSpec],
        tool_result: Callable[[str, dict[str, Any]], str],
        output_schema: type[BaseModelT],
        max_tokens: int,
        timeout_s: float,
        tool_name: str | None = None,
        tool_description: str | None = None,
        max_tool_turns: int = _DEFAULT_MAX_TOOL_TURNS,
    ) -> BaseModelT:
        """lode-35nu.11.6 -- see :meth:`LLMProvider.run_tool_turns` for the shape.

        Empty ``tools`` delegates straight to :meth:`structured_call`
        (the degenerate, byte-for-byte case that acceptance requires). A
        non-empty ``tools`` runs up to ``max_tool_turns`` free
        (``tool_choice={"type": "auto"}``) ``messages.create`` turns --
        feeding each tool_use block through ``tool_result`` and appending the
        result as a ``tool_result`` content block -- then one final
        ``messages.create`` with ``tool_choice`` forced to
        ``tool_name or output_schema.__name__``, continuing the same message
        history. ``timeout_s`` bounds the whole run (``max_tokens`` does not --
        see the module docstring): each SDK
        call is sent the *remaining* wall-clock budget, and the run raises
        :class:`LLMProviderError` rather than starting a call once that
        budget is exhausted.
        """
        if not tools:
            return self.structured_call(
                model=model,
                reasoning_effort=reasoning_effort,
                system=system,
                user_prompt=user_prompt,
                output_schema=output_schema,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                tool_name=tool_name,
                tool_description=tool_description,
            )

        import anthropic  # deferred -- lode-4q97; needed by the `except` below

        effort_kwargs = _anthropic_effort_kwargs(reasoning_effort, model=model)
        deadline = time.monotonic() + timeout_s
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        anthropic_tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

        def _remaining_or_raise(context: str) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMProviderError(
                    f"run_tool_turns budget (timeout_s={timeout_s}) exhausted "
                    f"before {context} (model={model})",
                    provider="anthropic",
                )
            return remaining

        total_output_tokens = 0

        for _ in range(max_tool_turns):
            remaining = _remaining_or_raise("starting the next free tool turn")
            try:
                response = self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=anthropic_tools,
                    tool_choice={"type": "auto"},
                    messages=messages,
                    timeout=remaining,
                    **effort_kwargs,
                )
            except anthropic.APIStatusError as exc:
                raise _anthropic_error_from_exception(
                    exc, context=f"model={model} (free tool turn)"
                ) from exc
            # lode-9594 -- feeds the per-run total logged below, compared
            # against lode-csl2's (max_tool_turns + 1) x max_tokens bound.
            total_output_tokens += _output_tokens(response)
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                break
            messages.append({"role": "assistant", "content": response.content})
            result_blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": tool_result(block.name, block.input),
                }
                for block in tool_use_blocks
            ]
            messages.append({"role": "user", "content": result_blocks})

        remaining = _remaining_or_raise("starting the final forced-schema turn")

        result, final_turn_output_tokens = self._forced_schema_turn(
            model=model,
            system=system,
            messages=messages,
            output_schema=output_schema,
            tool_name=tool_name or output_schema.__name__,
            tool_description=tool_description,
            max_tokens=max_tokens,
            timeout=remaining,
            effort_kwargs=effort_kwargs,
            where=" on run_tool_turns' final forced-schema turn",
        )
        total_output_tokens += final_turn_output_tokens
        # lode-9594 -- the completed run's total spend (free turns + the
        # unconditional final forced-schema turn) logged next to the bound it
        # should be compared against, so the GAP against
        # (max_tool_turns + 1) x max_tokens is readable without arithmetic.
        # Nothing here can raise: every operand is an int via
        # `_output_tokens`, and logging swallows its own handler errors.
        _log.info(
            "run_tool_turns spent %d output tokens (worst-case bound "
            "%d = (max_tool_turns=%d + 1) x max_tokens=%d, model=%s)",
            total_output_tokens,
            (max_tool_turns + 1) * max_tokens,
            max_tool_turns,
            max_tokens,
            model,
        )
        return result

    def submit_batch(
        self, requests: Sequence[BatchRequest], *, timeout_s: float
    ) -> str:
        # lode-a31q: pydantic v2 doesn't memoize model_json_schema() -- it
        # rebuilds a fresh dict per call (~0.5ms measured) -- and every
        # enrichment item in a batch carries the identical EnrichmentResult
        # schema, so building it per item wastes tens of ms per submission at
        # the default enrichment_batch_flush_size of 50, scaling with that
        # knob. Cache per distinct schema, keyed on the class object; scoped
        # to this submission, never process-global. The cached dict is shared
        # by reference across the requests using it -- treat it as read-only.
        schema_cache: dict[type[BaseModel], dict[str, Any]] = {}
        api_requests: list[dict[str, Any]] = []
        for req in requests:
            params: dict[str, Any] = {
                "model": req.model,
                "max_tokens": req.max_tokens,
                "system": req.system,
                "messages": [{"role": "user", "content": req.user_prompt}],
            }
            if req.tool_name is not None:
                input_schema = schema_cache.get(req.output_schema)
                if input_schema is None:
                    input_schema = req.output_schema.model_json_schema()
                    schema_cache[req.output_schema] = input_schema
                params["tools"] = [
                    {
                        "name": req.tool_name,
                        "description": req.tool_description or "",
                        "input_schema": input_schema,
                    }
                ]
                params["tool_choice"] = {"type": "tool", "name": req.tool_name}
            # reasoning_effort -> output_config.effort (lode-wnz1), same
            # validation and shape as the immediate structured_call path above.
            # `output_config` belongs inside each request's `params` (the batch
            # envelope is `{custom_id, params: MessageCreateParamsNonStreaming}`).
            params.update(
                _anthropic_effort_kwargs(req.reasoning_effort, model=req.model)
            )
            api_requests.append({"custom_id": req.custom_id, "params": params})

        import anthropic  # deferred -- lode-4q97; needed by the `except` below

        try:
            batch = self._client.beta.messages.batches.create(
                requests=api_requests, timeout=timeout_s
            )
        except anthropic.APIStatusError as exc:
            # lode-90o7: one `batches.create` submits every request atomically,
            # so a rejected effort/model pairing on any one of them fails the
            # whole submission -- hence a batch-shaped `context`, not a model.
            models = sorted({req.model for req in requests})
            raise _anthropic_error_from_exception(
                exc, context=f"batch of {len(requests)} request(s), models={models}"
            ) from exc
        return batch.id

    def collect_batch(
        self, handle: str, *, timeout_s: float
    ) -> tuple[Literal["pending"], None] | tuple[Literal["ended"], list[BatchResult]]:
        # Both deferred -- lode-4q97; needed by the `except` clauses below.
        import anthropic
        import httpx

        # These two SDK calls carry no `reasoning_effort`, so no pairing 400
        # (lode-90o7) can arise here -- but a 429/5xx/404 while polling still
        # must not escape raw (lode-i7yr): `enrich.collect_enrich_batch` calls
        # this with no `try` of its own, so its caller only ever expects
        # LLMProviderError.
        try:
            batch = self._client.beta.messages.batches.retrieve(
                handle, timeout=timeout_s
            )
        except anthropic.APIStatusError as exc:
            raise _anthropic_error_from_exception(
                exc, context=f"batches.retrieve handle={handle}"
            ) from exc
        if batch.processing_status != "ended":
            return ("pending", None)

        try:
            batch_results = self._client.beta.messages.batches.results(
                handle, timeout=timeout_s
            )
        except anthropic.APIStatusError as exc:
            raise _anthropic_error_from_exception(
                exc, context=f"batches.results handle={handle}"
            ) from exc

        # `batches.results` resolves the HTTP status before it builds the decoder
        # it returns, so an `APIStatusError` can only ever come from the call
        # above, never from iterating -- widening that clause would catch nothing
        # more. What the decoder *does* defer is the body: it streams lazily
        # (`http_response.iter_bytes`) and decodes each line as it is pulled, so a
        # dead stream or an undecodable line surfaces from the ITERATION as a raw
        # non-`anthropic` exception. `_stream` converts those (lode-3gtu);
        # `docs/stack.md` "Error contract" owns the inventory of what still gets
        # past this seam raw, and why discarding a partial read is safe.
        results: list[BatchResult] = []

        def _stream() -> Iterator[Any]:
            """Iterate `batch_results`, converting a mid-stream failure.

            Brackets only the *iteration* -- never the loop body below -- so a
            genuine bug there can never be mistaken for a stream failure, and no
            `except Exception` is needed to say so. Closes over `results` so the
            message can report how much had been decoded when the stream died.
            """
            try:
                yield from batch_results
            except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                context = (
                    f"batches.results handle={handle} failed while streaming JSONL "
                    f"results ({len(results)} result(s) already decoded, now "
                    f"discarded)"
                )
                _log.error("Anthropic call failed (%s): %s", context, exc)
                raise LLMProviderError(
                    f"{context}: {exc}", provider="anthropic"
                ) from exc

        for result in _stream():
            # lode-i821 (rebuild of lode-t7en): `construct_type_unchecked`
            # (jsonl.py) does not validate -- a well-formed-but-wrong-shape
            # line (e.g. missing `result`) leaves the *attribute* absent
            # rather than raising a pydantic `ValidationError`, so
            # `result.result.type` itself can raise a raw `AttributeError`.
            # Same treatment as the `StopIteration`/no-tool_use arm below:
            # degrade the one item to an `errored` `BatchResult` rather than
            # failing the whole collection -- narrow to `AttributeError`
            # only, so a genuine bug elsewhere in the loop body still
            # surfaces raw. `custom_id` may itself be the missing/malformed
            # field, hence `_wrong_shape_result`'s placeholder (see its
            # docstring).
            try:
                result_type = result.result.type
            except AttributeError as exc:
                results.append(
                    _wrong_shape_result(result, f"missing 'result' field: {exc}")
                )
                continue
            # One read for every branch below; see `_result_custom_id`.
            custom_id = _result_custom_id(result)
            if result_type == "succeeded":
                try:
                    tool_block = next(
                        b for b in result.result.message.content if b.type == "tool_use"
                    )
                except StopIteration:
                    # Degrading the one item (rather than failing the whole
                    # collection) is deliberate -- see the module docstring.
                    # lode-jgus made this reachable, and reaches it more
                    # easily here than on the immediate branch (class
                    # docstring); name the same model/stop_reason that branch
                    # does, or the failure is undiagnosable.
                    message = result.result.message
                    batch_stop_reason = getattr(message, "stop_reason", None)
                    results.append(
                        BatchResult(
                            custom_id=custom_id,
                            outcome="errored",
                            parsed=None,
                            error=LLMProviderError(
                                f"no tool_use block in batch result "
                                f"(model={getattr(message, 'model', None)!r}, "
                                f"stop_reason={batch_stop_reason!r}) "
                                f"{_budget_hint(batch_stop_reason)}",
                                provider="anthropic",
                            ),
                        )
                    )
                    continue
                except (AttributeError, TypeError) as exc:
                    # Same mechanism, two more unvalidated chains read by
                    # this one expression: `result.result.message.content`
                    # missing/`None` raises `TypeError` on iteration. A
                    # content block that's merely missing its `type` *key*
                    # decodes fine (the union falls back to a member with
                    # `type=None`, so `b.type == "tool_use"` is just `False`
                    # -- already handled by the `StopIteration` arm above);
                    # `b.type` only raises `AttributeError` when a content
                    # *item* isn't object-shaped at all (e.g. `null`), so
                    # `construct_type_unchecked` can't build any union member
                    # for it -- confirmed against the real SDK. Both failures
                    # here are the identical "wrong shape, not a stream
                    # failure" class as the `result.result.type` guard above
                    # -- one combined except arm, since either means this
                    # result line cannot be trusted at all.
                    results.append(
                        _wrong_shape_result(
                            result,
                            "missing 'message.content' field or a content "
                            f"block missing 'type': {exc}",
                        )
                    )
                    continue
                try:
                    parsed = RootModel[dict[str, Any]](tool_block.input)
                except ValidationError as exc:
                    # `tool_block.input` absent leaves it `None` rather than
                    # raising `AttributeError` (the SDK's `input` field
                    # itself tolerates `None` under `construct_type_unchecked`),
                    # so this chain fails one step later than the other two,
                    # inside `RootModel`'s own validation -- still the same
                    # wrong-shape class.
                    results.append(
                        _wrong_shape_result(result, f"missing 'input' field: {exc}")
                    )
                    continue
                results.append(
                    BatchResult(
                        custom_id=custom_id,
                        outcome="succeeded",
                        parsed=parsed,
                        error=None,
                    )
                )
            else:
                error_type = result_type
                msg = (
                    f"batch result={error_type}"
                    if not hasattr(result.result, "error")
                    else f"batch error: {result.result.error}"
                )
                results.append(
                    BatchResult(
                        custom_id=custom_id,
                        outcome=error_type,
                        parsed=None,
                        error=LLMProviderError(msg, provider="anthropic"),
                    )
                )
        return ("ended", results)


# ---------------------------------------------------------------------------
# OpenAIProvider (lode-568v.3) -- Responses API, one wire mechanism for
# structured output regardless of `tool_name` (docs/stack.md "2 & 3.").
# Batch is satisfied degenerately (docs/stack.md "4."): submit_batch runs
# every request through the same call synchronously and self-encodes the
# already-computed results into the returned handle string; collect_batch
# just decodes it, always "ended".
# ---------------------------------------------------------------------------

_MISSING_OPENAI_CREDENTIALS_MESSAGE = (
    "No OpenAI credentials found. lode resolves them from the OPENAI_API_KEY "
    "environment variable (direct OpenAI), or -- when the azure_openai_endpoint "
    "config knob is set -- the AZURE_OPENAI_API_KEY environment variable "
    "(Azure). Set the appropriate one (see docs/stack.md). lode never embeds "
    "an API key."
)

_MISSING_AZURE_CREDENTIALS_MESSAGE = (
    "No Azure OpenAI credentials found. azure_openai_endpoint is configured "
    "({endpoint!r}) but the AZURE_OPENAI_API_KEY environment variable is not "
    "set. Set it (see docs/stack.md). lode never embeds an API key."
)


def _safe_repr(value: object, *, limit: int = 2000) -> str:
    """``repr(value)``, truncated -- for logging a raw SDK object/body defensively."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _extract_content_filter(body: object) -> object | None:
    """Best-effort extraction of an Azure content-filter result from an error body.

    Azure's content-filtering surfaces as an ``innererror.content_filter_result``
    object nested in the error response body (challenge addendum,
    ``docs/stack.md`` "Error contract"). The exact nesting is not verified
    against a live Azure error (see the module docstring's OpenAIProvider risk
    note) -- this is defensive best-effort, returning ``None`` rather than
    raising when the shape does not match.
    """
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    inner = error.get("innererror")
    if isinstance(inner, dict) and "content_filter_result" in inner:
        return inner["content_filter_result"]
    return error.get("content_filter_result")


# The legal `reasoning.effort` values (lode-90o7) -- mirrors the installed SDK's
# own `Reasoning.effort` Literal, including order, and is pinned to it by
# `test_openai_effort_levels_match_the_installed_sdk_literal`. Same "ladder can
# grow" rationale as `_ANTHROPIC_EFFORT_LEVELS` above.
_OPENAI_EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Public so `lode.config.Settings` can validate `reasoning_effort` against the
# legal set for the *configured* `llm_provider` at Settings-construction time
# (lode-tvps), not only at the first API call. Keyed by the same literal
# `Settings.llm_provider` uses. The two per-provider tuples above stay the
# source of truth (and stay pinned to their installed SDK's own `Literal` by
# the meta-tests) -- this mapping is a thin, load-order-safe re-export, not a
# second copy.
EFFORT_LEVELS_BY_PROVIDER: Mapping[Literal["anthropic", "openai"], tuple[str, ...]] = {
    "anthropic": _ANTHROPIC_EFFORT_LEVELS,
    "openai": _OPENAI_EFFORT_LEVELS,
}


def _openai_effort_kwargs(
    reasoning_effort: str | None, *, model: str, provider_id: str
) -> dict[str, dict[str, str]]:
    """Build the ``reasoning`` **kwarg fragment** for ``reasoning_effort`` (lode-90o7).

    Mirrors :func:`_anthropic_effort_kwargs`'s shape exactly: returns
    ``{"reasoning": {"effort": ...}}`` when set and an empty dict when unset,
    so :meth:`OpenAIProvider._call_responses_raw` can splat it unconditionally
    with no ``None`` ever reaching the wire.

    Validates against :data:`_OPENAI_EFFORT_LEVELS` before any request is sent,
    closing the pre-flight gap :class:`OpenAIProvider` had relative to
    :class:`AnthropicProvider`. Value-only, not the value/``model``
    *pairing* -- same limitation and rationale as
    :func:`_anthropic_effort_kwargs`; a rejected pairing still comes back as a
    clean :class:`LLMProviderError` via
    :meth:`OpenAIProvider._error_from_exception`.
    """
    if reasoning_effort is None:
        return {}
    if reasoning_effort not in _OPENAI_EFFORT_LEVELS:
        raise LLMProviderError(
            f"invalid reasoning_effort {reasoning_effort!r} for model {model!r} "
            f"-- OpenAI's reasoning.effort accepts one of "
            f"{list(_OPENAI_EFFORT_LEVELS)}",
            provider=provider_id,
        )
    return {"reasoning": {"effort": reasoning_effort}}


class OpenAIProvider:
    """OpenAI/Azure implementation of the seam (``lode-568v.3``).

    One wire mechanism for structured output regardless of ``tool_name`` --
    the Responses API's ``text.format`` ``json_schema`` -- and batch satisfied
    degenerately by serializing through that same mechanism (module docstring
    "lode-568v.3" section has the full rationale, including why ``strict``
    mode is deliberately not used).

    **Diagnosability (challenge addendum, ``docs/stack.md`` "Error contract"):**
    every failure path logs the api-version/endpoint/deployment in play plus
    the raw provider error payload (including an Azure content-filter category
    when present) before raising, so a failure in a real Azure environment --
    which this repo cannot reproduce -- is diagnosable from logs alone.
    """

    def __init__(
        self,
        client: openai.OpenAI | openai.AzureOpenAI,
        *,
        provider_id: str = "openai",
        endpoint: str = "",
        api_version: str = "",
    ) -> None:
        self._client = client
        self._provider_id = provider_id
        self._endpoint = endpoint
        self._api_version = api_version

    def structured_call(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        system: str,
        user_prompt: str,
        output_schema: type[BaseModelT],
        max_tokens: int,
        timeout_s: float,
        tool_name: str | None = None,
        tool_description: str | None = None,
    ) -> BaseModelT:
        payload = self._call_responses_raw(
            model=model,
            reasoning_effort=reasoning_effort,
            system=system,
            user_prompt=user_prompt,
            schema_name=tool_name or output_schema.__name__,
            schema_description=tool_description,
            json_schema=output_schema.model_json_schema(),
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
        try:
            return output_schema.model_validate(payload)
        except ValidationError as exc:
            _log.error(
                "OpenAI/Azure response did not match %s (model=%s endpoint=%s "
                "api_version=%s): %s -- raw payload: %s",
                output_schema.__name__,
                model,
                self._endpoint,
                self._api_version,
                exc,
                _safe_repr(payload),
            )
            raise LLMProviderError(
                f"OpenAI/Azure response did not match {output_schema.__name__} "
                f"schema: {exc}",
                provider=self._provider_id,
            ) from exc

    def run_tool_turns(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        system: str,
        user_prompt: str,
        tools: Sequence[ToolSpec],
        tool_result: Callable[[str, dict[str, Any]], str],
        output_schema: type[BaseModelT],
        max_tokens: int,
        timeout_s: float,
        tool_name: str | None = None,
        tool_description: str | None = None,
        max_tool_turns: int = _DEFAULT_MAX_TOOL_TURNS,
    ) -> BaseModelT:
        """OpenAI/Azure degradation (lode-35nu.11.6, ``docs/stack.md``): the
        empty-``tools`` case delegates to :meth:`structured_call`, matching
        :class:`AnthropicProvider`; a non-empty ``tools`` is not supported by
        this provider and raises :class:`LLMProviderError` rather than
        silently answering without calling the tool the caller asked for.
        See the module docstring's "lode-35nu.11.6" section for why.
        """
        del tool_result, max_tool_turns
        if tools:
            raise LLMProviderError(
                "OpenAI/Azure multi-turn tool use is not implemented by this "
                "seam (lode-35nu.11.6 -- Anthropic-only provider parity "
                "decision, see docs/stack.md); only the empty-tools "
                "degenerate case (equivalent to structured_call) is "
                "supported for this provider.",
                provider=self._provider_id,
            )
        return self.structured_call(
            model=model,
            reasoning_effort=reasoning_effort,
            system=system,
            user_prompt=user_prompt,
            output_schema=output_schema,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            tool_name=tool_name,
            tool_description=tool_description,
        )

    def submit_batch(
        self, requests: Sequence[BatchRequest], *, timeout_s: float
    ) -> str:
        # lode-a31q: deliberately NOT carrying AnthropicProvider.submit_batch's
        # per-submission schema cache. This loop is degenerate -- one live
        # Responses API round trip per request -- so a sub-millisecond schema
        # rebuild is noise against the per-item network cost, where the
        # Anthropic loop is pure CPU before a single batches.create.
        encoded: list[dict[str, Any]] = []
        for req in requests:
            try:
                payload = self._call_responses_raw(
                    model=req.model,
                    reasoning_effort=req.reasoning_effort,
                    system=req.system,
                    user_prompt=req.user_prompt,
                    schema_name=req.tool_name or req.output_schema.__name__,
                    schema_description=req.tool_description,
                    json_schema=req.output_schema.model_json_schema(),
                    max_tokens=req.max_tokens,
                    timeout_s=timeout_s,
                )
            except LLMProviderError as exc:
                encoded.append(
                    {
                        "custom_id": req.custom_id,
                        "outcome": "errored",
                        "error": {
                            "message": str(exc),
                            "provider": exc.provider,
                            "status_code": exc.status_code,
                            "request_id": exc.request_id,
                        },
                    }
                )
                continue
            encoded.append(
                {
                    "custom_id": req.custom_id,
                    "outcome": "succeeded",
                    "payload": payload,
                }
            )
        return json.dumps(encoded)

    def collect_batch(
        self, handle: str, *, timeout_s: float
    ) -> tuple[Literal["pending"], None] | tuple[Literal["ended"], list[BatchResult]]:
        # Serialize: submit_batch already ran every request synchronously and
        # self-encoded the results into `handle` -- no network call, no actual
        # polling; always "ended" (docs/stack.md "4. The two-phase batch
        # contract").
        del timeout_s
        encoded = json.loads(handle)
        results: list[BatchResult] = []
        for item in encoded:
            if item["outcome"] == "succeeded":
                results.append(
                    BatchResult(
                        custom_id=item["custom_id"],
                        outcome="succeeded",
                        parsed=RootModel[dict[str, Any]](item["payload"]),
                        error=None,
                    )
                )
            else:
                err = item["error"]
                results.append(
                    BatchResult(
                        custom_id=item["custom_id"],
                        outcome="errored",
                        parsed=None,
                        error=LLMProviderError(
                            err["message"],
                            provider=err["provider"],
                            status_code=err.get("status_code"),
                            request_id=err.get("request_id"),
                        ),
                    )
                )
        return ("ended", results)

    # -- internals ------------------------------------------------------------

    def _call_responses_raw(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        system: str,
        user_prompt: str,
        schema_name: str,
        schema_description: str | None,
        json_schema: dict[str, Any],
        max_tokens: int,
        timeout_s: float,
    ) -> dict[str, Any]:
        """Make one Responses API call; return the raw decoded JSON payload."""
        text_format: dict[str, Any] = {
            "type": "json_schema",
            "name": schema_name,
            "schema": json_schema,
            # Deliberately NOT strict=True -- see the module docstring's
            # "lode-568v.3" OpenAIProvider note for why.
            "strict": False,
        }
        if schema_description:
            text_format["description"] = schema_description

        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": user_prompt,
            "max_output_tokens": max_tokens,
            "text": {"format": text_format},
            "timeout": timeout_s,
        }
        # reasoning_effort -> reasoning.effort (lode-90o7): validated up
        # front, then splatted -- empty when unset, so the kwarg is absent
        # rather than `None`. Mirrors `_anthropic_effort_kwargs`; see
        # `_openai_effort_kwargs`.
        kwargs.update(
            _openai_effort_kwargs(
                reasoning_effort, model=model, provider_id=self._provider_id
            )
        )

        try:
            response = self._client.responses.create(**kwargs)
        except Exception as exc:
            raise self._error_from_exception(exc, model=model) from exc

        return self._extract_json_payload(response, model=model)

    def _extract_json_payload(self, response: Any, model: str) -> dict[str, Any]:
        status = getattr(response, "status", None)
        if status is not None and status != "completed":
            raise self._error_from_incomplete_response(response, model=model)

        refusal = self._find_refusal(response)
        if refusal is not None:
            _log.error(
                "OpenAI/Azure structured-output call refused (model=%s "
                "endpoint=%s api_version=%s): %s",
                model,
                self._endpoint,
                self._api_version,
                refusal,
            )
            raise LLMProviderError(
                f"OpenAI/Azure model refused the request: {refusal}",
                provider=self._provider_id,
            )

        text = getattr(response, "output_text", "") or ""
        if not text:
            _log.error(
                "OpenAI/Azure structured-output call returned no text output "
                "(model=%s endpoint=%s api_version=%s status=%s) -- raw "
                "response: %s",
                model,
                self._endpoint,
                self._api_version,
                status,
                _safe_repr(response),
            )
            raise LLMProviderError(
                "OpenAI/Azure response contained no text output to parse",
                provider=self._provider_id,
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            _log.error(
                "OpenAI/Azure structured-output call returned unparseable "
                "JSON (model=%s endpoint=%s api_version=%s): %s -- raw text: %s",
                model,
                self._endpoint,
                self._api_version,
                exc,
                _safe_repr(text),
            )
            raise LLMProviderError(
                f"OpenAI/Azure response was not valid JSON: {exc}",
                provider=self._provider_id,
            ) from exc
        # A structured-output schema is always an object, but non-strict mode is
        # best-effort: the model can still emit a top-level `null` or array. Guard
        # here -- the single choke point both structured_call and submit_batch flow
        # through -- so a non-object payload becomes a clean LLMProviderError rather
        # than a raw pydantic ValidationError raised later out of collect_batch's
        # `RootModel[dict](...)`. On the immediate path structured_call would wrap
        # that itself, but on the batch path submit_batch stores the raw payload and
        # collect_batch builds the RootModel with no guard -- an unguarded raise
        # there escapes worker.drain (which catches only Auth errors) and, because
        # the offending payload is persisted inline in the batch handle, re-crashes
        # every subsequent drain, permanently wedging ALL enrich-batch collection.
        # Failing here instead lets submit_batch record this one request as
        # `errored` (like any other call failure), which drains as a transient
        # per-job failure. (lode-568v.3 correctness review.)
        if not isinstance(payload, dict):
            kind = type(payload).__name__
            _log.error(
                "OpenAI/Azure structured-output call returned a non-object JSON "
                "value (model=%s endpoint=%s api_version=%s type=%s) -- raw text: %s",
                model,
                self._endpoint,
                self._api_version,
                kind,
                _safe_repr(text),
            )
            raise LLMProviderError(
                f"OpenAI/Azure response JSON was not an object (got {kind})",
                provider=self._provider_id,
            )
        return payload

    @staticmethod
    def _find_refusal(response: Any) -> str | None:
        """Walk ``response.output`` message items for a Structured Outputs refusal."""
        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    return getattr(part, "refusal", None) or "refused"
        return None

    def _error_from_incomplete_response(
        self, response: Any, *, model: str
    ) -> LLMProviderError:
        status = getattr(response, "status", None)
        incomplete = getattr(response, "incomplete_details", None)
        reason = getattr(incomplete, "reason", None) if incomplete is not None else None
        api_error = getattr(response, "error", None)
        _log.error(
            "OpenAI/Azure call did not complete (model=%s endpoint=%s "
            "api_version=%s status=%s reason=%s error=%s) -- raw response: %s",
            model,
            self._endpoint,
            self._api_version,
            status,
            reason,
            api_error,
            _safe_repr(response),
        )
        detail = f"status={status}"
        if reason:
            detail += f" reason={reason}"
        if api_error is not None:
            detail += f" error={api_error}"
        return LLMProviderError(
            f"OpenAI/Azure call did not complete ({detail})",
            provider=self._provider_id,
        )

    def _error_from_exception(self, exc: Exception, *, model: str) -> LLMProviderError:
        status_code = getattr(exc, "status_code", None)
        request_id = getattr(exc, "request_id", None)
        body = getattr(exc, "body", None)
        if request_id is None:
            http_response = getattr(exc, "response", None)
            headers = getattr(http_response, "headers", None)
            if headers is not None:
                request_id = headers.get("x-request-id") or headers.get(
                    "apim-request-id"
                )
        content_filter = _extract_content_filter(body)
        _log.error(
            "OpenAI/Azure call failed (model=%s endpoint=%s api_version=%s "
            "status_code=%s request_id=%s content_filter=%s): %s -- raw "
            "body: %s",
            model,
            self._endpoint,
            self._api_version,
            status_code,
            request_id,
            content_filter,
            exc,
            _safe_repr(body),
        )
        message = f"OpenAI/Azure call failed: {exc}"
        if content_filter is not None:
            message += f" (content_filter={content_filter})"
        return LLMProviderError(
            message,
            provider=self._provider_id,
            status_code=status_code,
            request_id=request_id,
        )


def _build_openai_client(settings: Settings) -> openai.OpenAI | openai.AzureOpenAI:
    """Resolve OpenAI/Azure credentials + routing; return the constructed SDK client.

    Unlike :func:`build_provider`'s Anthropic branch, this DOES raise
    :class:`LLMAuthError` on a missing credential -- there is no pre-existing
    exception type to preserve for a provider that didn't exist before this
    ticket (``lode-568v.2``'s tracked follow-up; see the module docstring's
    "lode-568v.3" section). Keys are resolved from the environment only, never
    from ``config.toml`` (``docs/stack.md`` "6. Config shape").
    """
    import openai  # deferred -- mirrors auth.py's import discipline (lode-4q97)

    if settings.azure_openai_endpoint:
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        if not api_key:
            raise LLMAuthError(
                _MISSING_AZURE_CREDENTIALS_MESSAGE.format(
                    endpoint=settings.azure_openai_endpoint
                ),
                provider="openai",
            )
        return openai.AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
            api_key=api_key,
        )

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMAuthError(_MISSING_OPENAI_CREDENTIALS_MESSAGE, provider="openai")
    return openai.OpenAI(api_key=api_key)


# ---------------------------------------------------------------------------
# build_provider (docs/stack.md "1. Client + credential/routing construction")
# ---------------------------------------------------------------------------


def build_provider(settings: Settings) -> LLMProvider:
    """Resolve credentials + routing for ``settings.llm_provider``; return its provider.

    ``settings.llm_provider == "anthropic"`` resolves via
    :func:`lode.auth.build_client` (the same SDK credential chain used today)
    and returns an :class:`AnthropicProvider`. A missing credential raises
    :class:`~lode.auth.AuthError` (unchanged -- see the module docstring's
    "implementation details" section for why this ticket does not wrap it in
    :class:`LLMAuthError`).

    ``settings.llm_provider == "openai"`` resolves via
    :func:`_build_openai_client` (``OPENAI_API_KEY``, or ``AZURE_OPENAI_API_KEY``
    + routing knobs when ``settings.azure_openai_endpoint`` is set -- Azure-vs-
    direct-OpenAI is a routing detail under this one provider value, never a
    second provider value) and returns an :class:`OpenAIProvider`. A missing
    credential raises :class:`LLMAuthError` (unlike the Anthropic branch --
    see ``lode-568v.3`` in the module docstring for why).

    ``settings.llm_provider`` is validated to be ``"anthropic"`` or ``"openai"``
    at :class:`Settings` construction, so the ``else`` branch below is
    unreachable in practice -- it exists only to satisfy the return type if
    that validation is ever loosened.
    """
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(build_client())
    if settings.llm_provider == "openai":
        client = _build_openai_client(settings)
        return OpenAIProvider(
            client,
            endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
        )
    raise LLMProviderError(
        f"unsupported llm_provider {settings.llm_provider!r}",
        provider=settings.llm_provider,
    )


# ---------------------------------------------------------------------------
# Provenance (lode-568v.4, design pinned lode-568v.1)
# ---------------------------------------------------------------------------


def provider_identity(settings: Settings) -> str | None:
    """The provider value :mod:`lode.enrich` persists for provenance.

    ``docs/decisions.md`` (lode-568v.1): ``NULL`` means "anthropic" by
    convention -- pre-seam rows have no ``provider`` column to backfill, and
    every row written today is Anthropic-produced regardless (``anthropic``
    is the only value ``settings.llm_provider`` accepts until ``lode-568v.3``
    lands a second provider). So this returns ``None`` while the active
    provider is Anthropic, and the literal provider string once a non-Anthropic
    provider exists -- keeping the column's meaning consistent for old and new
    rows alike, rather than writing the literal ``"anthropic"`` string once
    this ticket lands and leaving every prior row looking different for no
    semantic reason.
    """
    return None if settings.llm_provider == "anthropic" else settings.llm_provider
