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
- ``reasoning_effort`` is accepted by :class:`AnthropicProvider` but ignored --
  Anthropic has no such axis (``docs/stack.md`` "the two immediate
  structured-output calls").
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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, RootModel, model_validator

from lode.auth import build_client

if TYPE_CHECKING:
    import anthropic

    from lode.config import Settings

BaseModelT = TypeVar("BaseModelT", bound=BaseModel)


class ModelTier(BaseModel):
    """A per-surface model/effort tier (``docs/stack.md`` "Config shape").

    ``model`` is an Anthropic model id, or an Azure/OpenAI deployment name
    once a second provider lands (``lode-568v.3``). ``reasoning_effort`` is
    meaningful only under a reasoning-capable deployment; :class:`AnthropicProvider`
    ignores it.

    A bare TOML string (every existing ``config.toml`` today, e.g.
    ``enrichment_llm = "claude-haiku-4-5"``) coerces to
    ``ModelTier(model=<string>)`` -- back-compat, no migration required.
    """

    model_config = ConfigDict(frozen=True)

    model: str
    reasoning_effort: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_bare_string(cls, data: object) -> object:
        if isinstance(data, str):
            return {"model": data}
        return data


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


class AnthropicProvider:
    """Wraps today's ``anthropic.Anthropic`` client; the sole provider (T2).

    Every method reproduces today's exact wire mechanism -- forced tool-use
    for enrichment, ``messages.parse`` for Q&A, ``beta.messages.batches.*``
    for the batch path -- so routing through this seam is byte-for-byte
    equivalent to calling the SDK directly, as it was before this ticket.
    """

    def __init__(self, client: anthropic.Anthropic) -> None:
        self._client = client

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
        # reasoning_effort is ignored -- Anthropic has no such axis.
        if tool_name is not None:
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
                messages=[{"role": "user", "content": user_prompt}],
                timeout=timeout_s,
            )
            tool_block = next(b for b in response.content if b.type == "tool_use")
            return output_schema.model_validate(tool_block.input)

        response = self._client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            output_format=output_schema,
            timeout=timeout_s,
        )
        return response.parsed_output

    def submit_batch(
        self, requests: Sequence[BatchRequest], *, timeout_s: float
    ) -> str:
        api_requests: list[dict[str, Any]] = []
        for req in requests:
            params: dict[str, Any] = {
                "model": req.model,
                "max_tokens": req.max_tokens,
                "system": req.system,
                "messages": [{"role": "user", "content": req.user_prompt}],
            }
            if req.tool_name is not None:
                params["tools"] = [
                    {
                        "name": req.tool_name,
                        "description": req.tool_description or "",
                        "input_schema": req.output_schema.model_json_schema(),
                    }
                ]
                params["tool_choice"] = {"type": "tool", "name": req.tool_name}
            api_requests.append({"custom_id": req.custom_id, "params": params})

        batch = self._client.beta.messages.batches.create(
            requests=api_requests, timeout=timeout_s
        )
        return batch.id

    def collect_batch(
        self, handle: str, *, timeout_s: float
    ) -> tuple[Literal["pending"], None] | tuple[Literal["ended"], list[BatchResult]]:
        batch = self._client.beta.messages.batches.retrieve(handle, timeout=timeout_s)
        if batch.processing_status != "ended":
            return ("pending", None)

        results: list[BatchResult] = []
        for result in self._client.beta.messages.batches.results(
            handle, timeout=timeout_s
        ):
            if result.result.type == "succeeded":
                try:
                    tool_block = next(
                        b for b in result.result.message.content if b.type == "tool_use"
                    )
                except StopIteration:
                    results.append(
                        BatchResult(
                            custom_id=result.custom_id,
                            outcome="errored",
                            parsed=None,
                            error=LLMProviderError(
                                "no tool_use block in batch result",
                                provider="anthropic",
                            ),
                        )
                    )
                    continue
                results.append(
                    BatchResult(
                        custom_id=result.custom_id,
                        outcome="succeeded",
                        parsed=RootModel[dict[str, Any]](tool_block.input),
                        error=None,
                    )
                )
            else:
                error_type = result.result.type
                msg = (
                    f"batch result={error_type}"
                    if not hasattr(result.result, "error")
                    else f"batch error: {result.result.error}"
                )
                results.append(
                    BatchResult(
                        custom_id=result.custom_id,
                        outcome=error_type,
                        parsed=None,
                        error=LLMProviderError(msg, provider="anthropic"),
                    )
                )
        return ("ended", results)


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

    ``settings.llm_provider`` is validated to be ``"anthropic"`` (the only
    value accepted until ``lode-568v.3`` lands OpenAI) at :class:`Settings`
    construction, so the ``else`` branch below is unreachable in practice --
    it exists only to satisfy the return type if that validation is ever
    loosened.
    """
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(build_client())
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
