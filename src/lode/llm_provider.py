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
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, RootModel, ValidationError, model_validator

from lode.auth import build_client

if TYPE_CHECKING:
    import anthropic
    import openai

    from lode.config import Settings

BaseModelT = TypeVar("BaseModelT", bound=BaseModel)

_log = logging.getLogger(__name__)


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

    def submit_batch(
        self, requests: Sequence[BatchRequest], *, timeout_s: float
    ) -> str:
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
        if reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": reasoning_effort}

        try:
            response = self._client.responses.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 -- normalize every SDK failure
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
