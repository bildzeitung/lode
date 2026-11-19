"""Tests for lode.llm_provider -- the vendor-neutral LLMProvider seam (lode-568v.2/.3).

Covers what the enrich/qa/cited_answer/worker/cli test suites only exercise
indirectly (through AnthropicProvider-wrapped fakes): ModelTier coercion, the
AnthropicProvider wire mapping for structured_call/submit_batch/collect_batch,
the OpenAIProvider (lode-568v.3) Responses API mapping + serialize-batch, and
build_provider's provider resolution for both providers.
"""

import inspect
import json
import logging
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import anthropic
import httpx
import httpx2
import pytest
from _anthropic_rig import (
    _ended_batch_body,
    _jsonl,
    _payload_without,
    _real_anthropic_client,
    _results_handler,
    _succeeded_payload,
)
from conftest import (
    _text_block,
    _tool_use_block,
    fake_batch_client,
    structured_response,
)
from pydantic import BaseModel, ValidationError

from lode.config import Settings
from lode.llm_provider import (
    _ANTHROPIC_EFFORT_LEVELS,
    _BUDGET_EXHAUSTED_HINT,
    _CONTEXT_WINDOW_EXCEEDED_HINT,
    _OPENAI_EFFORT_LEVELS,
    AnthropicProvider,
    BatchRequest,
    BatchResult,
    LLMAuthError,
    LLMProvider,
    LLMProviderError,
    ModelTier,
    OpenAIProvider,
    ToolSpec,
    _stop_reason_hint,
    build_provider,
    provider_identity,
)


class _Widget(BaseModel):
    name: str
    count: int = 0


def _widget_schema() -> dict:
    """The ``output_config.format.schema`` the SDK derives for ``_Widget``."""
    return anthropic.transform_schema(_Widget)


# ---------------------------------------------------------------------------
# ModelTier
# ---------------------------------------------------------------------------


def test_model_tier_coerces_from_a_bare_string() -> None:
    tier = ModelTier.model_validate("claude-haiku-4-5")
    assert tier.model == "claude-haiku-4-5"
    assert tier.reasoning_effort is None
    assert tier.max_tokens is None


def test_model_tier_accepts_explicit_fields() -> None:
    tier = ModelTier(model="gpt-5.5", reasoning_effort="high")
    assert tier.model == "gpt-5.5"
    assert tier.reasoning_effort == "high"


def test_model_tier_accepts_an_explicit_max_tokens_override() -> None:
    # lode-d70n: the per-tier output-budget override.
    tier = ModelTier(model="claude-opus-5", max_tokens=4096)
    assert tier.max_tokens == 4096


def test_model_tier_max_tokens_defaults_to_none() -> None:
    # None means "use the call site's own source-constant default"
    # (qa.MAX_TOKENS / enrich.MAX_TOKENS) -- back-compat, no migration needed.
    tier = ModelTier(model="x")
    assert tier.max_tokens is None


def test_model_tier_rejects_a_non_positive_max_tokens() -> None:
    with pytest.raises(ValidationError):
        ModelTier(model="x", max_tokens=0)
    with pytest.raises(ValidationError):
        ModelTier(model="x", max_tokens=-1)


def test_resolve_max_tokens_falls_back_to_the_call_sites_default() -> None:
    # lode-d70n: the one home for "unset means the call site's own constant",
    # shared by qa.answer_question and both enrichment routes.
    assert ModelTier(model="x").resolve_max_tokens(2048) == 2048


def test_resolve_max_tokens_prefers_the_tier_override() -> None:
    assert ModelTier(model="x", max_tokens=777).resolve_max_tokens(2048) == 777


def test_model_tier_is_frozen() -> None:
    tier = ModelTier(model="x")
    with pytest.raises(ValidationError):
        tier.model = "y"


# ---------------------------------------------------------------------------
# AnthropicProvider.structured_call
# ---------------------------------------------------------------------------


def _anthropic_bad_request() -> object:
    """A real ``anthropic.BadRequestError`` (lode-90o7) -- the shape the SDK
    actually raises for a legal ``reasoning_effort`` *value* on a model that
    doesn't support it, the reachable gap this ticket closes. A real instance
    (not a duck-typed fake) proves the ``except anthropic.APIStatusError``
    clauses in ``AnthropicProvider`` actually match the SDK's own exception
    class, not just something that happens to have the right attributes.
    """
    import anthropic

    message = "effort not supported for this model"
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        400,
        request=request,
        headers={"request-id": "req-test-1"},
        json={"error": {"type": "invalid_request_error", "message": message}},
    )
    return anthropic.BadRequestError(message, response=response, body=response.json())


@pytest.mark.parametrize(
    "tool_kwargs",
    [{}, {"tool_name": "extract_widget", "tool_description": "Extract a widget."}],
    ids=["no-tool-name", "tool-name-ignored"],
)
def test_structured_call_sends_structured_output_and_never_forces_a_tool(
    tool_kwargs: dict,
) -> None:
    # lode-ni7p: enrichment passes tool_name/tool_description (OpenAI reads
    # them as the schema name); Anthropic sends the same structured-output
    # request either way, never a forced tool_choice, so a model that 400s on
    # one still works.
    client = mock.MagicMock()
    client.messages.create.return_value = _parsed_response(_Widget(name="w", count=1))
    provider = AnthropicProvider(client)

    result = provider.structured_call(
        model="claude-sonnet-5-5",
        reasoning_effort=None,
        system="sys",
        user_prompt="prompt",
        output_schema=_Widget,
        max_tokens=50,
        timeout_s=7.0,
        **tool_kwargs,
    )

    assert result == _Widget(name="w", count=1)
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["output_config"]["format"]["schema"] == _widget_schema()
    assert kwargs["messages"] == [{"role": "user", "content": "prompt"}]
    assert kwargs["timeout"] == 7.0
    # lode-3dlt: `thinking` is never sent -- an explicit `disabled`
    # (lode-d1sr) 400s on Fable-class models at any effort and on Opus 5 at
    # effort xhigh/max. See the AnthropicProvider docstring.
    assert "thinking" not in kwargs
    # Tool definitions belong only to run_tool_turns' final turn; the
    # single-shot wire shape carries none.
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs
    client.messages.create.assert_called_once()


def test_structured_call_omits_thinking_for_a_fable_class_model() -> None:
    # lode-3dlt: the regression this ticket exists to fix -- an explicit
    # thinking={"type": "disabled"} 400s on Fable-class models at ANY effort
    # level. No model-family branching in the fix, so the same assertion as
    # the default-tier test above holds here too -- proving there is no
    # per-model code path that could reintroduce the illegal value.
    client = mock.MagicMock()
    client.messages.create.return_value = _parsed_response(_Widget(name="w", count=1))
    provider = AnthropicProvider(client)

    result = provider.structured_call(
        model="claude-fable-5",
        reasoning_effort=None,
        system="sys",
        user_prompt="prompt",
        output_schema=_Widget,
        max_tokens=50,
        timeout_s=7.0,
    )

    assert result == _Widget(name="w", count=1)
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-fable-5"
    assert "thinking" not in kwargs


def test_structured_call_raises_when_the_response_has_no_text_block() -> None:
    # lode-3dlt: with `thinking` no longer pinned off, a response can spend its
    # whole max_tokens budget inside thinking and come back with no text block
    # at all. It must be an LLMProviderError naming the cause, not an
    # AttributeError inside qa.answer_question.
    client = mock.MagicMock()
    client.messages.create.return_value = _parsed_response(
        None, stop_reason="max_tokens"
    )
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.structured_call(
            model="claude-opus-5",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=8192,
            timeout_s=1.0,
        )

    message = str(excinfo.value)
    assert "no text block" in message
    # The diagnosis has to survive to the log, not just the exception type.
    assert "max_tokens" in message
    assert "claude-opus-5" in message
    assert excinfo.value.provider == "anthropic"


def _structured_call_with(response: SimpleNamespace) -> LLMProviderError:
    client = mock.MagicMock()
    client.messages.create.return_value = response
    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).structured_call(
            model="claude-opus-5",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=8192,
            timeout_s=1.0,
        )
    return excinfo.value


def test_structured_call_diagnoses_a_max_tokens_truncation() -> None:
    # lode-3dlt / lode-pdss: text cut off mid-JSON because the output budget
    # ran out names stop_reason and the budget remedy.
    error = _structured_call_with(
        structured_response(None, stop_reason="max_tokens", text=_TRUNCATED_JSON)
    )

    message = str(error)
    assert "_Widget" in message
    assert "stop_reason='max_tokens'" in message
    assert "cut off" in message
    assert _BUDGET_EXHAUSTED_HINT in message
    assert "policy refusal" not in message
    assert error.provider == "anthropic"
    assert isinstance(error.__cause__, ValidationError)


def test_structured_call_diagnoses_a_genuine_schema_violation() -> None:
    # Complete JSON of the wrong shape, finished normally: neither a refusal
    # nor a truncation, and it still names stop_reason.
    error = _structured_call_with(
        structured_response(None, text='{"name": 5, "count": "x"}')
    )

    message = str(error)
    assert "_Widget" in message
    assert "stop_reason='end_turn'" in message
    assert "violates the schema" in message
    assert "cut off" not in message
    assert "policy refusal" not in message
    assert isinstance(error.__cause__, ValidationError)


def test_structured_call_names_context_window_exceeded_when_no_text_block() -> None:
    # lode-cai6: stop_reason="model_context_window_exceeded" is a distinct
    # cause from "max_tokens" -- the *input* no longer fits, so shrinking
    # max_tokens can't help. The message must name the overflow and its remedy
    # instead of the generic thinking-budget hint.
    client = mock.MagicMock()
    client.messages.create.return_value = _parsed_response(
        None, stop_reason="model_context_window_exceeded"
    )
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.structured_call(
            model="claude-opus-5",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=2048,
            timeout_s=1.0,
        )

    message = str(excinfo.value)
    assert "no text block" in message
    assert "stop_reason='model_context_window_exceeded'" in message
    assert "context window" in message
    assert "shrink the retrieved context or user_prompt" in message
    assert "typically the whole output budget was consumed by thinking" not in message
    assert excinfo.value.provider == "anthropic"


def test_structured_call_wraps_a_bad_request_from_the_structured_output_branch() -> (
    None
):
    # lode-90o7: reasoning_effort set to a legal *value* on a tier whose
    # *model* doesn't support it reaches the API and comes back as
    # anthropic.BadRequestError. Must not escape raw -- callers of this seam
    # only expect LLMProviderError.
    client = mock.MagicMock()
    bad_request = _anthropic_bad_request()
    client.messages.create.side_effect = bad_request
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.structured_call(
            model="claude-sonnet-4-6",
            reasoning_effort="low",
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )

    err = excinfo.value
    assert err.provider == "anthropic"
    assert err.status_code == 400
    assert err.request_id == "req-test-1"
    assert err.__cause__ is bad_request


def test_structured_call_sends_effort_on_the_structured_output_branch() -> None:
    # lode-wnz1: reasoning_effort now reaches Anthropic as
    # output_config.effort on the structured-output branch. Must never surface
    # as a raw `reasoning_effort` kwarg -- that's not a real SDK parameter.
    client = mock.MagicMock()
    client.messages.create.return_value = _parsed_response(_Widget(name="w"))
    provider = AnthropicProvider(client)

    provider.structured_call(
        model="claude-opus-5",
        reasoning_effort="high",
        system="sys",
        user_prompt="p",
        output_schema=_Widget,
        max_tokens=10,
        timeout_s=1.0,
    )

    kwargs = client.messages.create.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    # `format` must survive alongside `effort` in the one output_config.
    assert kwargs["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": _widget_schema()},
    }


def test_structured_call_omits_effort_when_reasoning_effort_is_none() -> None:
    client = mock.MagicMock()
    client.messages.create.return_value = _parsed_response(_Widget(name="w"))
    provider = AnthropicProvider(client)

    provider.structured_call(
        model="claude-sonnet-4-6",
        reasoning_effort=None,
        system="sys",
        user_prompt="p",
        output_schema=_Widget,
        max_tokens=10,
        timeout_s=1.0,
    )

    assert "effort" not in client.messages.create.call_args.kwargs["output_config"]


@pytest.mark.parametrize("bad_effort", ["LOW", "extreme", "", "medium ", "effort"])
def test_structured_call_rejects_an_invalid_effort_value(bad_effort: str) -> None:
    # lode-wnz1 acceptance: an invalid/unsupported effort value fails clearly
    # (LLMProviderError, before any request is sent) rather than being
    # silently dropped or producing a raw anthropic.BadRequestError.
    client = mock.MagicMock()
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.structured_call(
            model="claude-opus-5",
            reasoning_effort=bad_effort,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )

    assert bad_effort in str(excinfo.value)
    assert excinfo.value.provider == "anthropic"
    client.messages.create.assert_not_called()


def test_effort_levels_match_the_installed_sdk_literal() -> None:
    # `_ANTHROPIC_EFFORT_LEVELS` claims to mirror the SDK's own effort Literal
    # exactly. Pin that claim mechanically: the ladder has grown once already
    # (xhigh arrived with Opus 4.7), and a sixth level shipping upstream would
    # otherwise make lode reject a legal value with a spurious
    # LLMProviderError, with nothing failing to say so. Order is asserted too,
    # since the constant is what renders the error message's intensity ladder.
    import typing

    from anthropic.types.output_config_param import OutputConfigParam

    effort_hint = typing.get_type_hints(OutputConfigParam)["effort"]
    # `effort: Optional[Literal[...]]` -- unwrap the Optional, then the Literal.
    literal, _none = typing.get_args(effort_hint)
    assert typing.get_args(literal) == _ANTHROPIC_EFFORT_LEVELS


# ---------------------------------------------------------------------------
# run_tool_turns (lode-35nu.11.6)
# ---------------------------------------------------------------------------


def _tool_use_response(
    name: str, tool_input: dict, block_id: str = "toolu_1", output_tokens: int = 10
) -> object:
    """Wrap ``conftest._tool_use_block`` in a free-tool-turn response
    (``.content``/``.stop_reason``). The response-wrapping form stays local to
    this module rather than moving into conftest.py, since no other test
    module needs it (lode-j5o1) -- see ``_tool_use_block``'s docstring.

    ``output_tokens`` (default 10, arbitrary but nonzero) backs
    ``response.usage.output_tokens`` -- lode-9594's per-run spend
    accumulation reads this real int rather than an auto-created MagicMock
    attribute.
    """
    block = _tool_use_block(name=name, tool_input=tool_input, block_id=block_id)
    response = mock.MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    response.usage = mock.MagicMock(output_tokens=output_tokens)
    return response


def _parsed_response(
    parsed: object, *, output_tokens: int = 0, stop_reason: str = "end_turn"
) -> SimpleNamespace:
    """A ``messages.create`` response: what a structured-output turn gets."""
    return structured_response(
        parsed, output_tokens=output_tokens, stop_reason=stop_reason
    )


def test_run_tool_turns_with_empty_tools_delegates_to_structured_call() -> None:
    # lode-35nu.11.6 acceptance bar: the degenerate case is byte-for-byte
    # identical to calling structured_call directly -- no messages.create loop
    # machinery engages at all.
    client = mock.MagicMock()
    client.messages.create.return_value = _parsed_response(_Widget(name="w", count=1))
    provider = AnthropicProvider(client)

    result = provider.run_tool_turns(
        model="claude-sonnet-4-6",
        reasoning_effort=None,
        system="sys",
        user_prompt="prompt",
        tools=(),
        tool_result=lambda name, args: pytest.fail("no tool should be called"),
        output_schema=_Widget,
        max_tokens=50,
        timeout_s=7.0,
    )

    assert result == _Widget(name="w", count=1)
    client.messages.create.assert_called_once()
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["output_config"]["format"]["schema"] == _widget_schema()
    assert kwargs["timeout"] == 7.0
    assert "tools" not in kwargs


@pytest.mark.parametrize("owner", [LLMProvider, AnthropicProvider, OpenAIProvider])
def test_run_tool_turns_takes_no_forced_tool_parameters(owner: type) -> None:
    # lode-66ve: the final turn is structured output, never a forced
    # `tool_choice` (which 400s on the Q&A default models, Sonnet 5.5 / Opus
    # 5.5), so there is no tool to name. Nothing type-checks the
    # implementations against the Protocol, hence all three.
    params = inspect.signature(owner.run_tool_turns).parameters
    assert "tool_name" not in params
    assert "tool_description" not in params


def test_run_tool_turns_runs_a_free_tool_turn_then_forces_the_final_schema() -> None:
    # The full run this ticket's acceptance criteria names: free tool turn ->
    # tool result returned -> final structured-output turn producing a validated
    # output_schema instance.
    tool = ToolSpec(
        name="lookup_widget", description="Look up a widget.", input_schema={}
    )
    free_turn = _tool_use_response("lookup_widget", {"id": "w-1"})
    final_turn = _parsed_response(_Widget(name="widget", count=3))
    client = mock.MagicMock()
    client.messages.create.side_effect = [free_turn, final_turn]
    provider = AnthropicProvider(client)

    calls: list[tuple[str, dict]] = []

    def _tool_result(name: str, args: dict) -> str:
        calls.append((name, args))
        return "widget w-1 has count 3"

    result = provider.run_tool_turns(
        model="claude-sonnet-4-6",
        reasoning_effort=None,
        system="sys",
        user_prompt="find widget w-1",
        tools=(tool,),
        tool_result=_tool_result,
        output_schema=_Widget,
        max_tokens=100,
        timeout_s=30.0,
        # One free turn only -- the loop otherwise asks again after a tool
        # result to see whether the model wants another tool, which needs a
        # third mock response; max_tool_turns=1 isolates exactly the shape
        # this test asserts (one tool call, then the final turn).
        max_tool_turns=1,
    )

    assert result == _Widget(name="widget", count=3)
    assert calls == [("lookup_widget", {"id": "w-1"})]
    assert client.messages.create.call_count == 2  # one free turn + the final turn

    free_kwargs = client.messages.create.call_args_list[0].kwargs
    assert free_kwargs["tool_choice"] == {"type": "auto"}
    assert free_kwargs["tools"] == [
        {
            "name": "lookup_widget",
            "description": "Look up a widget.",
            "input_schema": {},
        }
    ]
    # `messages` is the same list object mutated in place across turns (the
    # mock stores it by reference), so only its first element is a stable
    # snapshot of what the free turn actually saw -- its full final shape is
    # asserted via `final_kwargs["messages"]` below instead.
    assert free_kwargs["messages"][0] == {"role": "user", "content": "find widget w-1"}

    # lode-ugj5: the final turn is structured output (`output_config.format`),
    # never a forced tool_choice (400s on Sonnet 5.5 /
    # Opus 5.5). The tool definitions stay because the history carries
    # tool_use blocks; `none` makes a further call impossible.
    final_kwargs = client.messages.create.call_args.kwargs
    assert final_kwargs["output_config"]["format"]["schema"] == _widget_schema()
    assert final_kwargs["tool_choice"] == {"type": "none"}
    assert final_kwargs["tools"] == free_kwargs["tools"]
    final_messages = final_kwargs["messages"]
    assert final_messages[0] == {"role": "user", "content": "find widget w-1"}
    assert final_messages[1] == {"role": "assistant", "content": free_turn.content}
    assert final_messages[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "widget w-1 has count 3",
            }
        ],
    }


def test_run_tool_turns_logs_total_output_tokens_against_the_worst_case_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # lode-9594 -- the measurement lode-csl2's deferral trigger waits on: a
    # completed run logs its total output-token spend (free turn +
    # unconditional final structured-output turn) next to the
    # (max_tool_turns + 1) x max_tokens worst-case bound.
    tool = ToolSpec(name="lookup_widget", description="d", input_schema={})
    free_turn = _tool_use_response("lookup_widget", {"id": "w"}, output_tokens=40)
    final_turn = _parsed_response(_Widget(name="w", count=1), output_tokens=25)
    client = mock.MagicMock()
    client.messages.create.side_effect = [free_turn, final_turn]
    provider = AnthropicProvider(client)

    with caplog.at_level(logging.INFO, logger="lode.llm_provider"):
        result = provider.run_tool_turns(
            model="claude-sonnet-4-6",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: "ok",
            output_schema=_Widget,
            max_tokens=100,
            timeout_s=30.0,
            max_tool_turns=1,
        )

    assert result == _Widget(name="w", count=1)
    [record] = [r for r in caplog.records if "run_tool_turns spent" in r.message]
    assert record.message == (
        "run_tool_turns spent 65 output tokens (worst-case bound 200 = "
        "(max_tool_turns=1 + 1) x max_tokens=100, model=claude-sonnet-4-6)"
    )


def test_run_tool_turns_forces_the_final_turn_when_the_model_never_calls_a_tool() -> (
    None
):
    # The model may decline to call any tool on the first free turn -- the run
    # still proceeds straight to the final turn.
    tool = ToolSpec(
        name="lookup_widget", description="Look up a widget.", input_schema={}
    )
    text_only = mock.MagicMock()
    text_only.content = [_text_block()]
    final_turn = _parsed_response(_Widget(name="w", count=0))
    client = mock.MagicMock()
    client.messages.create.side_effect = [text_only, final_turn]
    provider = AnthropicProvider(client)

    result = provider.run_tool_turns(
        model="claude-sonnet-4-6",
        reasoning_effort=None,
        system="sys",
        user_prompt="p",
        tools=(tool,),
        tool_result=lambda name, args: pytest.fail("no tool should be called"),
        output_schema=_Widget,
        max_tokens=100,
        timeout_s=30.0,
    )

    assert result == _Widget(name="w", count=0)
    assert client.messages.create.call_count == 2  # one free turn + the final turn
    final_kwargs = client.messages.create.call_args.kwargs
    assert final_kwargs["tool_choice"] == {"type": "none"}


def test_run_tool_turns_stops_after_max_tool_turns_and_still_forces_the_final_turn() -> (
    None
):
    tool = ToolSpec(name="lookup_widget", description="d", input_schema={})
    always_calls_tool = _tool_use_response("lookup_widget", {"id": "w"})
    final_turn = _parsed_response(_Widget(name="w", count=9))
    client = mock.MagicMock()
    # 2 free turns (max_tool_turns=2) always calling the tool, then the
    # structured-output final turn.
    client.messages.create.side_effect = [
        always_calls_tool,
        always_calls_tool,
        final_turn,
    ]
    provider = AnthropicProvider(client)

    result = provider.run_tool_turns(
        model="claude-sonnet-4-6",
        reasoning_effort=None,
        system="sys",
        user_prompt="p",
        tools=(tool,),
        tool_result=lambda name, args: "ok",
        output_schema=_Widget,
        max_tokens=100,
        timeout_s=30.0,
        max_tool_turns=2,
    )

    assert result == _Widget(name="w", count=9)
    # Two free turns, then the final structured-output turn.
    assert client.messages.create.call_count == 3


def test_run_tool_turns_raises_when_the_run_timeout_is_exhausted() -> None:
    tool = ToolSpec(name="lookup_widget", description="d", input_schema={})
    client = mock.MagicMock()
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError, match="budget"):
        provider.run_tool_turns(
            model="claude-sonnet-4-6",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: "ok",
            output_schema=_Widget,
            max_tokens=100,
            timeout_s=-1.0,
        )
    client.messages.create.assert_not_called()


def test_run_tool_turns_wraps_a_bad_request_from_the_free_turn() -> None:
    tool = ToolSpec(name="lookup_widget", description="d", input_schema={})
    client = mock.MagicMock()
    bad_request = _anthropic_bad_request()
    client.messages.create.side_effect = bad_request
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.run_tool_turns(
            model="claude-haiku-4-5",
            reasoning_effort="low",
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: "ok",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=30.0,
        )
    assert excinfo.value.provider == "anthropic"
    assert excinfo.value.status_code == 400


def test_run_tool_turns_wraps_a_bad_request_from_the_final_structured_turn() -> None:
    tool = ToolSpec(name="lookup_widget", description="d", input_schema={})
    free_turn = _tool_use_response("lookup_widget", {"id": "w"})
    bad_request = _anthropic_bad_request()
    client = mock.MagicMock()
    client.messages.create.side_effect = [free_turn, bad_request]
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.run_tool_turns(
            model="claude-sonnet-5-5",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: "ok",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=30.0,
            max_tool_turns=1,
        )
    assert excinfo.value.provider == "anthropic"
    assert excinfo.value.status_code == 400
    # Distinguishes this from the free-turn wrap above, which reports the
    # same provider and status.
    assert "final structured-output turn" in str(excinfo.value)


def test_run_tool_turns_raises_when_the_final_turn_has_no_text_block() -> None:
    tool = ToolSpec(name="lookup_widget", description="d", input_schema={})
    free_turn = _tool_use_response("lookup_widget", {"id": "w"})
    # Budget spent on thinking: the response carries no text block.
    no_text = _parsed_response(None, stop_reason="max_tokens")
    client = mock.MagicMock()
    client.messages.create.side_effect = [free_turn, no_text]
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError, match="no text block"):
        provider.run_tool_turns(
            model="claude-opus-5-5",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: "ok",
            output_schema=_Widget,
            max_tokens=2048,
            timeout_s=30.0,
            max_tool_turns=1,  # isolate the one free turn + final turn
        )


def test_openai_run_tool_turns_with_empty_tools_delegates_to_structured_call() -> None:
    client = _fake_responses_client(output_text='{"name": "w", "count": 1}')
    provider = OpenAIProvider(client)

    result = provider.run_tool_turns(
        model="gpt-5.5",
        reasoning_effort=None,
        system="sys",
        user_prompt="prompt",
        tools=(),
        tool_result=lambda name, args: pytest.fail("no tool should be called"),
        output_schema=_Widget,
        max_tokens=50,
        timeout_s=7.0,
    )

    assert result == _Widget(name="w", count=1)


def test_openai_run_tool_turns_with_tools_raises_the_documented_degradation() -> None:
    # lode-35nu.11.6 provider-parity decision (docs/stack.md): Anthropic-only
    # multi-turn tool use -- OpenAI/Azure raises rather than silently ignoring
    # the tools it was asked to offer the model.
    tool = ToolSpec(name="lookup_widget", description="d", input_schema={})
    client = mock.MagicMock()
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError, match="not implemented"):
        provider.run_tool_turns(
            model="gpt-5.5",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: "ok",
            output_schema=_Widget,
            max_tokens=50,
            timeout_s=7.0,
        )
    client.responses.create.assert_not_called()


# ---------------------------------------------------------------------------
# AnthropicProvider.submit_batch / collect_batch
# ---------------------------------------------------------------------------


def _batch_request(**overrides: object) -> BatchRequest:
    defaults: dict = {
        "custom_id": "ver-1",
        "model": "claude-haiku-4-5",
        "reasoning_effort": None,
        "system": "sys",
        "user_prompt": "prompt",
        "output_schema": _Widget,
        "max_tokens": 100,
        "tool_name": "extract_widget",
        "tool_description": "Extract a widget.",
    }
    defaults.update(overrides)
    return BatchRequest(**defaults)


def test_submit_batch_builds_structured_output_requests() -> None:
    # lode-ni7p: the batch route sends `output_config.format`, never a forced
    # tool_choice (which 400s on Sonnet 5.5 / Opus 5.5 -- and only at collect
    # time, after the handle is persisted).
    client = mock.MagicMock()
    client.beta.messages.batches.create.return_value = SimpleNamespace(id="batch-1")
    provider = AnthropicProvider(client)

    handle = provider.submit_batch([_batch_request()], timeout_s=30.0)

    assert handle == "batch-1"
    kwargs = client.beta.messages.batches.create.call_args.kwargs
    assert kwargs["timeout"] == 30.0
    (req,) = kwargs["requests"]
    assert req["custom_id"] == "ver-1"
    assert req["params"]["model"] == "claude-haiku-4-5"
    assert req["params"]["output_config"] == {
        "format": {"type": "json_schema", "schema": anthropic.transform_schema(_Widget)}
    }
    assert "tools" not in req["params"]
    assert "tool_choice" not in req["params"]


@pytest.mark.network
def test_batch_and_immediate_routes_put_the_same_request_on_the_wire() -> None:
    """Pins the wire equivalence `submit_batch`'s comment claims (lode-568v.2).

    Both routes build `output_config.format` by hand (the immediate route in
    `_parsed_turn`, since lode-pdss). Driven through the real SDK so the two
    cannot silently diverge.
    """
    bodies: dict[str, dict] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/batches"):
            bodies["batch"] = body
            return httpx2.Response(200, json=_ended_batch_body("batch-1"))
        bodies["immediate"] = body
        return httpx2.Response(200, json=_succeeded_payload()["result"]["message"])

    provider = AnthropicProvider(_real_anthropic_client(handler))
    request = _batch_request(model="claude-opus-5", reasoning_effort="low")

    provider.structured_call(
        model=request.model,
        reasoning_effort=request.reasoning_effort,
        system=request.system,
        user_prompt=request.user_prompt,
        output_schema=request.output_schema,
        max_tokens=request.max_tokens,
        timeout_s=5.0,
        tool_name=request.tool_name,
        tool_description=request.tool_description,
    )
    provider.submit_batch([request], timeout_s=5.0)

    (batch_item,) = bodies["batch"]["requests"]
    assert batch_item["params"] == bodies["immediate"]
    assert set(batch_item["params"]["output_config"]) == {"effort", "format"}


def test_submit_batch_builds_schema_once_per_distinct_output_schema() -> None:
    # lode-a31q: every batch item sharing the same output_schema must reuse
    # one transform_schema() build, not rebuild it per item -- and the cache
    # must be scoped to ONE submission. A process-global cache keyed on the
    # class object would satisfy the first assertion while pinning every
    # schema class for the life of the process, so the second submission below
    # pins the lifetime too: it must pay its own build, not inherit the first.
    client = mock.MagicMock()
    client.beta.messages.batches.create.return_value = SimpleNamespace(id="batch-5")
    provider = AnthropicProvider(client)
    batch = [
        _batch_request(custom_id="ver-1"),
        _batch_request(custom_id="ver-2"),
        _batch_request(custom_id="ver-3"),
    ]

    with mock.patch.object(
        anthropic, "transform_schema", wraps=anthropic.transform_schema
    ) as spy:
        provider.submit_batch(batch, timeout_s=30.0)
        assert spy.call_count == 1
        provider.submit_batch(batch, timeout_s=30.0)
        assert spy.call_count == 2

    expected = anthropic.transform_schema(_Widget)
    reqs = client.beta.messages.batches.create.call_args.kwargs["requests"]
    assert len(reqs) == 3
    for req in reqs:
        assert req["params"]["output_config"]["format"]["schema"] == expected


def test_submit_batch_builds_correct_schema_per_output_schema_in_a_heterogeneous_batch() -> (
    None
):
    # lode-a31q: the per-submission schema cache is keyed on output_schema,
    # so a batch mixing schemas still gets the right schema on each request.
    class _Gadget(BaseModel):
        weight: float = 0.0

    client = mock.MagicMock()
    client.beta.messages.batches.create.return_value = SimpleNamespace(id="batch-6")
    provider = AnthropicProvider(client)

    provider.submit_batch(
        [
            _batch_request(custom_id="ver-1", output_schema=_Widget),
            _batch_request(custom_id="ver-2", output_schema=_Gadget),
            _batch_request(custom_id="ver-3", output_schema=_Widget),
        ],
        timeout_s=30.0,
    )

    widget_schema = anthropic.transform_schema(_Widget)
    gadget_schema = anthropic.transform_schema(_Gadget)
    reqs = client.beta.messages.batches.create.call_args.kwargs["requests"]
    by_id = {req["custom_id"]: req for req in reqs}
    assert (
        by_id["ver-1"]["params"]["output_config"]["format"]["schema"] == widget_schema
    )
    assert (
        by_id["ver-2"]["params"]["output_config"]["format"]["schema"] == gadget_schema
    )
    assert (
        by_id["ver-3"]["params"]["output_config"]["format"]["schema"] == widget_schema
    )


def test_submit_batch_sends_effort_when_reasoning_effort_is_set() -> None:
    # lode-wnz1: the batch path gets the same output_config.effort wiring as
    # the immediate structured_call path.
    client = mock.MagicMock()
    client.beta.messages.batches.create.return_value = SimpleNamespace(id="batch-3")
    provider = AnthropicProvider(client)

    # Override `_batch_request`'s Haiku 4.5 model too -- effort errors on
    # Haiku 4.5; see lode-90o7.
    provider.submit_batch(
        [_batch_request(model="claude-opus-5", reasoning_effort="medium")],
        timeout_s=30.0,
    )

    (req,) = client.beta.messages.batches.create.call_args.kwargs["requests"]
    assert req["params"]["output_config"]["effort"] == "medium"
    assert req["params"]["output_config"]["format"]["type"] == "json_schema"


def test_submit_batch_omits_effort_when_reasoning_effort_is_none() -> None:
    client = mock.MagicMock()
    client.beta.messages.batches.create.return_value = SimpleNamespace(id="batch-4")
    provider = AnthropicProvider(client)

    provider.submit_batch([_batch_request(reasoning_effort=None)], timeout_s=30.0)

    (req,) = client.beta.messages.batches.create.call_args.kwargs["requests"]
    assert set(req["params"]["output_config"]) == {"format"}


def test_submit_batch_rejects_an_invalid_effort_value() -> None:
    client = mock.MagicMock()
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.submit_batch(
            [_batch_request(reasoning_effort="not-a-real-level")], timeout_s=30.0
        )

    assert "not-a-real-level" in str(excinfo.value)
    assert excinfo.value.provider == "anthropic"
    client.beta.messages.batches.create.assert_not_called()


def test_submit_batch_wraps_a_bad_request_from_batches_create() -> None:
    # lode-90o7: a single `batches.create` call submits the whole batch
    # atomically, so a rejected effort/model pairing on any one request fails
    # the whole submission -- must surface as LLMProviderError, not raw.
    client = mock.MagicMock()
    bad_request = _anthropic_bad_request()
    client.beta.messages.batches.create.side_effect = bad_request
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.submit_batch(
            [_batch_request(model="claude-haiku-4-5", reasoning_effort="low")],
            timeout_s=30.0,
        )

    err = excinfo.value
    assert err.provider == "anthropic"
    assert err.status_code == 400
    assert "claude-haiku-4-5" in str(err)
    assert err.__cause__ is bad_request


def test_collect_batch_returns_pending_when_not_ended() -> None:
    client = mock.MagicMock()
    client.beta.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="in_progress"
    )
    provider = AnthropicProvider(client)

    status, results = provider.collect_batch("batch-1", timeout_s=10.0)

    assert status == "pending"
    assert results is None
    client.beta.messages.batches.results.assert_not_called()


def test_collect_batch_wraps_a_bad_request_from_batches_retrieve() -> None:
    # lode-i7yr: a failure while polling must surface as LLMProviderError.
    client = mock.MagicMock()
    bad_request = _anthropic_bad_request()
    client.beta.messages.batches.retrieve.side_effect = bad_request
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.collect_batch("batch-1", timeout_s=10.0)

    err = excinfo.value
    assert err.provider == "anthropic"
    assert err.status_code == 400
    assert "batch-1" in str(err)
    assert err.__cause__ is bad_request
    client.beta.messages.batches.results.assert_not_called()


def test_collect_batch_wraps_a_bad_request_from_batches_results() -> None:
    # lode-i7yr: same as above, for the second (results) polling call.
    client = mock.MagicMock()
    client.beta.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended"
    )
    bad_request = _anthropic_bad_request()
    client.beta.messages.batches.results.side_effect = bad_request
    provider = AnthropicProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.collect_batch("batch-1", timeout_s=10.0)

    err = excinfo.value
    assert err.provider == "anthropic"
    assert err.status_code == 400
    assert "batch-1" in str(err)
    assert err.__cause__ is bad_request


# The shared real-SDK Anthropic batch-results rig lives in _anthropic_rig.py
# (lode-a9x3, lode-qnfp); the imports at the top of this file are its roster.


def _succeeded_jsonl_line(custom_id: str) -> bytes:
    """A real, fully-decodable JSONL line for ``batches.results``."""
    return _jsonl(_succeeded_payload(custom_id))


@pytest.mark.network
def test_collect_batch_wraps_a_real_sdk_status_error_from_the_results_url() -> None:
    """Pins the SDK-internals property `collect_batch`'s comment rests on.

    Fails if a future SDK ever defers the status check into iteration, silently
    reopening the gap lode-i7yr closed.
    """
    import anthropic

    client = _real_anthropic_client(
        _results_handler(
            "batch-1",
            lambda: httpx2.Response(
                429,
                headers={"request-id": "req-test-2"},
                json={"error": {"type": "rate_limit_error", "message": "slow down"}},
            ),
        )
    )

    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    err = excinfo.value
    assert err.provider == "anthropic"
    assert err.status_code == 429
    assert err.request_id == "req-test-2"
    assert isinstance(err.__cause__, anthropic.APIStatusError)


@pytest.mark.network
def test_collect_batch_wraps_a_malformed_jsonl_line_from_the_results_stream() -> None:
    """lode-3gtu: a malformed line surfaces as a raw `json.JSONDecodeError`.

    It comes from inside the SDK's lazily-streamed decoder, with the status
    already resolved cleanly at 200, so no `except anthropic.*` clause reaches
    it. Non-vacuous: against the pre-fix code this raised the `JSONDecodeError`
    raw, failing `pytest.raises(LLMProviderError)`.
    """
    client = _real_anthropic_client(
        _results_handler(
            "batch-1", lambda: httpx2.Response(200, content=b"not valid json\n")
        )
    )

    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    err = excinfo.value
    assert err.provider == "anthropic"
    assert "batch-1" in str(err)
    assert isinstance(err.__cause__, json.JSONDecodeError)


@pytest.mark.network
def test_collect_batch_wraps_an_undecodable_utf8_byte_in_the_results_stream() -> None:
    """lode-3gtu: invalid UTF-8 in a line is a `UnicodeDecodeError`, NOT a
    `json.JSONDecodeError` -- `json.loads(bytes)` sniffs an encoding and decodes
    before it ever parses, so an invalid byte fails one step earlier and lands
    on a different `ValueError` subclass. Catching only `JSONDecodeError` misses
    it entirely; measured raw-escaping against the real SDK before this test.

    The first four bytes are ASCII so `json.detect_encoding` picks utf-8 -- i.e.
    this is the ordinary "server emitted a bad byte mid-line" case, not a
    contrived BOM.
    """
    client = _real_anthropic_client(
        _results_handler(
            "batch-1",
            lambda: httpx2.Response(200, content=b'{"custom_id": "\xff\xfe\xfd"}\n'),
        )
    )

    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    err = excinfo.value
    assert err.provider == "anthropic"
    assert "batch-1" in str(err)
    assert isinstance(err.__cause__, UnicodeDecodeError)
    assert not isinstance(err.__cause__, json.JSONDecodeError)


@pytest.mark.network
def test_collect_batch_discards_partial_results_on_a_mid_stream_transport_failure() -> (
    None
):
    """lode-3gtu: a stream that dies AFTER a line already decoded discards it.

    The deliberate choice: raise rather than return the partial result list --
    `batches.results` re-fetches the identical JSONL from the start on every
    call (there is no resume-after-N-lines cursor), so nothing already-good is
    permanently lost. Pins that `collect_batch` never returns a tuple here.

    Two lines, not one, is load-bearing: httpx2's 64-byte chunker holds the tail
    of the last raw chunk back, so a single line's trailing newline never
    reaches the JSONL decoder and NOTHING would be decoded before the error --
    the "partial" in this test's name would be a fiction. The second line
    flushes the first. Asserting the decoded COUNT is what keeps that honest.
    """

    class _FlakyStream(httpx2.SyncByteStream):
        """Yields two good JSONL lines, then dies as if the connection reset."""

        def __iter__(self) -> Iterator[bytes]:
            yield _succeeded_jsonl_line("ver-one")
            yield _succeeded_jsonl_line("ver-two")
            raise httpx2.ReadError("connection reset mid-stream")

        def close(self) -> None:
            pass

    client = _real_anthropic_client(
        _results_handler("batch-1", lambda: httpx2.Response(200, stream=_FlakyStream()))
    )

    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    err = excinfo.value
    assert err.provider == "anthropic"
    assert "batch-1" in str(err)
    assert "1 result(s) already decoded" in str(err)
    assert isinstance(err.__cause__, httpx2.HTTPError)


@pytest.mark.network
@pytest.mark.parametrize(
    "path,expected_detail_substring",
    [
        (("result",), "missing 'result' field"),
        (("result", "message", "content"), "message.content"),
        (("result", "message", "content", 0, "text"), "not a JSON object"),
    ],
    ids=[
        "missing-result-field",
        "missing-message-content-field",
        "content-block-missing-text-field",
    ],
)
def test_collect_batch_degrades_a_wrong_shape_line_missing_a_field(
    path: tuple[str | int, ...], expected_detail_substring: str
) -> None:
    """lode-i821: each case is the success payload MINUS exactly one field.

    `construct_type_unchecked` (jsonl.py) does not validate, so each of these
    missing fields leaves the corresponding attribute absent/`None` rather
    than raising a pydantic `ValidationError` at decode time -- the resulting
    `AttributeError`/`TypeError`/(pydantic) `ValidationError` surfaces from
    `collect_batch`'s loop BODY. A `MagicMock` can't reproduce this leniency,
    so this drives the real SDK. Non-vacuous against the pre-fix code for
    each case: 'missing-result' escaped as a raw `AttributeError`,
    'missing-message-content' as a raw `TypeError`, and
    'content-block-missing-input' as a raw pydantic `ValidationError`.
    """
    payload = _payload_without(_succeeded_payload("ver-shape"), *path)
    client = _real_anthropic_client(
        _results_handler(
            "batch-1", lambda: httpx2.Response(200, content=_jsonl(payload))
        )
    )

    status, results = AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    assert status == "ended"
    (result,) = results
    assert result.custom_id == "ver-shape"
    assert result.outcome == "errored"
    assert result.parsed is None
    assert isinstance(result.error, LLMProviderError)
    assert "wrong-shape" in str(result.error)
    assert expected_detail_substring in str(result.error)


@pytest.mark.network
def test_collect_batch_degrades_a_wrong_shape_line_with_a_non_object_content_block() -> (
    None
):
    """lode-i821: `b.type` inside `next(b for b in ... if b.type == "text")`.

    A content block missing just its `type` *key* still decodes to a
    `BetaTextBlock` with `type=None` -- `b.type == "text"` is simply
    `False`, no exception, already handled by the "no text block"
    `StopIteration` arm. The chain actually breaks when a content
    *item* is not object-shaped at all (e.g. `null`): construct_type_unchecked
    then can't build any union member, so `b.type` raises a raw
    `AttributeError` -- confirmed against the real SDK before writing this
    test. Non-vacuous against the pre-fix code (this `AttributeError`
    escaped raw).
    """
    payload = _succeeded_payload("ver-shape")
    payload["result"]["message"]["content"] = [None]
    client = _real_anthropic_client(
        _results_handler(
            "batch-1", lambda: httpx2.Response(200, content=_jsonl(payload))
        )
    )

    status, results = AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    assert status == "ended"
    (result,) = results
    assert result.custom_id == "ver-shape"
    assert result.outcome == "errored"
    assert result.parsed is None
    assert isinstance(result.error, LLMProviderError)
    assert "wrong-shape" in str(result.error)
    assert "message.content" in str(result.error)


@pytest.mark.network
def test_collect_batch_substitutes_a_placeholder_for_a_line_that_is_not_an_object() -> (
    None
):
    """lode-i821 finding 2: `BatchResult.custom_id` is declared `str`, not
    `str | None`. A JSONL line that decodes to something other than an
    object (e.g. `null`) has no `custom_id` attribute at all --
    `construct_type_unchecked` returns the decoded value verbatim, not even
    wrapped in a model -- confirmed against the real SDK before writing this
    test. `_wrong_shape_result` must substitute a non-`None` placeholder
    rather than propagate `None` (which broke
    `enrich.collect_enrich_batch`'s `short_version_id(version_id)` call
    downstream with a raw `TypeError` -- see
    `test_collect_enrich_batch_survives_a_non_object_results_line` in
    `test_enrich.py`, which drives this same line through that seam).
    """
    line = (json.dumps(None) + "\n").encode()
    client = _real_anthropic_client(
        _results_handler("batch-1", lambda: httpx2.Response(200, content=line))
    )

    status, results = AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    assert status == "ended"
    (result,) = results
    assert result.custom_id is not None
    assert isinstance(result.custom_id, str)
    assert result.outcome == "errored"
    assert result.parsed is None
    assert isinstance(result.error, LLMProviderError)
    assert "wrong-shape" in str(result.error)


def _drop_custom_id(payload: dict) -> dict:
    return _payload_without(payload, "custom_id")


def _null_custom_id(payload: dict) -> dict:
    return {**payload, "custom_id": None}


def _expired_without_custom_id(payload: dict) -> dict:
    mutated = _drop_custom_id(payload)
    mutated["result"] = {"type": "expired"}
    return mutated


def _no_text_without_custom_id(payload: dict) -> dict:
    mutated = _drop_custom_id(payload)
    mutated["result"]["message"]["content"] = [{"type": "thinking", "thinking": "hi"}]
    return mutated


@pytest.mark.network
@pytest.mark.parametrize(
    "mutate,expected_outcome,expected_error_fragment",
    [
        (_drop_custom_id, "succeeded", None),
        (_null_custom_id, "succeeded", None),
        (_expired_without_custom_id, "expired", None),
        (_no_text_without_custom_id, "errored", "no text block"),
    ],
    ids=[
        "custom-id-missing",
        "custom-id-null",
        "errored-branch",
        "no-tool-use-branch",
    ],
)
def test_collect_batch_substitutes_a_placeholder_when_only_custom_id_is_unusable(
    mutate: Callable[[dict], dict],
    expected_outcome: str,
    expected_error_fragment: str | None,
) -> None:
    """lode-i821, found in review: the placeholder is NOT a wrong-shape-only concern.

    A line whose ``result`` block decodes fine and whose ``custom_id`` alone
    is missing/``None`` passes every wrong-shape guard and takes an ORDINARY
    branch -- it never reaches ``_wrong_shape_result``. The rebuild
    normalized the placeholder only inside that helper, so such a line still
    emitted ``BatchResult(custom_id=None)``, violating the declared
    ``custom_id: str`` and re-arming exactly the downstream
    ``short_version_id(None)`` ``TypeError`` that finding 2 exists to close.
    ``_result_custom_id`` now owns the substitution for every branch.

    One case per ``BatchResult`` construction site that reads ``custom_id``,
    since each builds its result from a different code path: the ``succeeded``
    branch (twice, for the two ways ``custom_id`` can be unusable), the
    ``else``/``error_type`` branch, and the pre-existing no-``tool_use``
    ``StopIteration`` arm -- which predates this ticket but leaked
    identically. Non-vacuous for all four: each asserts ``custom_id is None``
    pre-fix.

    Note ``outcome`` is unchanged by the substitution -- an unusable
    ``custom_id`` does not make the result wrong-shaped, only unroutable,
    which ``collect_enrich_batch`` already handles as a ``job_map`` miss.
    """
    payload = mutate(_succeeded_payload("ver-shape"))
    client = _real_anthropic_client(
        _results_handler(
            "batch-1", lambda: httpx2.Response(200, content=_jsonl(payload))
        )
    )

    status, results = AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    assert status == "ended"
    (result,) = results
    assert result.custom_id == "<unknown>"
    assert result.outcome == expected_outcome
    if expected_error_fragment is not None:
        assert expected_error_fragment in str(result.error)


def _succeeded_result(custom_id: str, payload: dict) -> mock.MagicMock:
    text_block = _text_block(json.dumps(payload))
    result_obj = mock.MagicMock()
    result_obj.custom_id = custom_id
    result_obj.result.type = "succeeded"
    result_obj.result.message.content = [text_block]
    return result_obj


def test_collect_batch_decodes_succeeded_results_as_raw_payload() -> None:
    client = mock.MagicMock()
    client.beta.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended"
    )
    client.beta.messages.batches.results.return_value = iter(
        [_succeeded_result("ver-1", {"name": "w", "count": 2})]
    )
    provider = AnthropicProvider(client)

    status, results = provider.collect_batch("batch-1", timeout_s=10.0)

    assert status == "ended"
    (result,) = results
    assert result.custom_id == "ver-1"
    assert result.outcome == "succeeded"
    assert result.error is None
    # parsed is the RAW payload (a RootModel[dict]), never a validated domain
    # object -- the caller re-validates against its own schema (module docstring).
    assert result.parsed.root == {"name": "w", "count": 2}
    assert _Widget.model_validate(result.parsed.root) == _Widget(name="w", count=2)


def test_collect_batch_maps_errored_result_to_llm_provider_error() -> None:
    client = mock.MagicMock()
    client.beta.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended"
    )
    errored = mock.MagicMock()
    errored.custom_id = "ver-2"
    errored.result.type = "errored"
    errored.result.error = "rate_limited"
    client.beta.messages.batches.results.return_value = iter([errored])
    provider = AnthropicProvider(client)

    status, results = provider.collect_batch("batch-1", timeout_s=10.0)

    assert status == "ended"
    (result,) = results
    assert result.outcome == "errored"
    assert result.parsed is None
    assert isinstance(result.error, LLMProviderError)
    assert result.error.provider == "anthropic"
    assert "rate_limited" in str(result.error)


def _collect_one(content: list, stop_reason: str) -> BatchResult:
    """Collect one message built from ``content``/``stop_reason`` and return its result.

    A good result follows it in the same batch: whatever happens to the first
    item must never take the second down with it.
    """
    first = mock.MagicMock()
    first.custom_id = "ver-4"
    first.result.type = "succeeded"
    first.result.message.content = content
    first.result.message.model = "claude-haiku-5-5"
    first.result.message.stop_reason = stop_reason
    first.result.message.stop_details = None
    good = _succeeded_result("ver-5", {"name": "w", "count": 2})
    client = fake_batch_client("batch-1", results=[first, good])

    status, results = AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    assert status == "ended"
    first_result, good_result = results
    assert first_result.custom_id == "ver-4"
    assert good_result.outcome == "succeeded"
    return first_result


def test_collect_batch_handles_a_succeeded_result_missing_a_text_block() -> None:
    # No text content block at all -- the provider must not blow up the
    # whole collect_batch call over one malformed result (see module docstring).
    result = _collect_one([], "max_tokens")

    assert result.outcome == "errored"
    assert result.parsed is None
    assert isinstance(result.error, LLMProviderError)
    # lode-jgus: this is the route where a thinking-capable enrichment_llm
    # override is MOST likely to exhaust `max_tokens` (nothing bounds a batch
    # item's generation but its own cap -- no per-item timeout), so the
    # diagnosis has to survive to the log, exactly as on the immediate branch.
    # Assert `stop_reason=` and not a bare "max_tokens": this message carries
    # no max_tokens FIELD, so a bare substring would pass on the stop_reason
    # VALUE alone and keep passing if the diagnosis were dropped.
    message = str(result.error)
    assert "no text block" in message
    assert "claude-haiku-5-5" in message
    assert "stop_reason='max_tokens'" in message


@pytest.mark.parametrize(
    "text",
    ['{"name": "w", "cou', "not json at all", "[1, 2]", ""],
    ids=["truncated-mid-json", "not-json", "json-array", "empty-text"],
)
def test_collect_batch_degrades_a_text_block_that_is_not_a_json_object(
    text: str,
) -> None:
    # lode-ni7p: structured output decodes from JSON text. A message that
    # ended normally with undecodable text is a wrong-shape line, failed per
    # item, not per collection.
    bad_result = _collect_one([_text_block(text)], "end_turn")

    assert bad_result.outcome == "errored"
    assert bad_result.parsed is None
    assert isinstance(bad_result.error, LLMProviderError)
    assert "wrong-shape batch result line" in str(bad_result.error)
    assert "not a JSON object" in str(bad_result.error)
    assert "stop_reason='end_turn'" in str(bad_result.error)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
@pytest.mark.parametrize(
    "text",
    ['{"name": "w", "cou', '{"name": "w", "count": 1}'],
    ids=["partial-json", "complete-json"],
)
def test_collect_batch_never_decodes_an_incomplete_message(
    text: str, stop_reason: str
) -> None:
    # lode-ni7p: thinking shares max_tokens with the JSON, so a cut-off message
    # is the realistic batch failure, and a declined one is partial by
    # contract. Neither is read -- even text that happens to parse must not
    # come back as a success -- and each carries its own diagnosis rather than
    # reading as a malformed line.
    result = _collect_one([_text_block(text)], stop_reason)

    assert result.outcome == "errored"
    assert result.parsed is None
    message = str(result.error)
    assert "incomplete text block discarded" in message
    assert "wrong-shape" not in message
    assert "claude-haiku-5-5" in message
    if stop_reason == "refusal":
        _assert_refusal_hint(message, category=None)
    else:
        assert "stop_reason='max_tokens'" in message
        assert _BUDGET_EXHAUSTED_HINT in message


def test_collect_batch_reads_the_text_block_after_a_thinking_block() -> None:
    # The default enrichment tier thinks, so the JSON text block is normally
    # preceded by a thinking block -- read by type, never by position.
    thinking_block = mock.MagicMock()
    thinking_block.type = "thinking"
    result = _collect_one(
        [thinking_block, _text_block('{"name": "w", "count": 1}')], "end_turn"
    )

    assert result.outcome == "succeeded"
    assert result.parsed is not None
    assert result.parsed.root == {"name": "w", "count": 1}


def _assert_refusal_hint(message: str, *, category: str | None = "cyber") -> None:
    assert "stop_reason='refusal'" in message
    assert "policy refusal" in message
    assert "raising max_tokens will not help" in message
    assert _BUDGET_EXHAUSTED_HINT not in message
    if category is None:
        assert "category" not in message
    else:
        assert f"category={category!r}" in message


_TRUNCATED_JSON = '{"name": "w", "cou'


def _refused_response(text: str | None = _TRUNCATED_JSON) -> SimpleNamespace:
    """A mid-generation refusal: HTTP 200, partial (default: truncated) text."""
    response = structured_response(None, stop_reason="refusal", text=text)
    response.stop_details = SimpleNamespace(category="cyber")
    return response


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(),
        SimpleNamespace(stop_details=None),
        SimpleNamespace(stop_details=SimpleNamespace(category=None)),
    ],
    ids=["stop-details-absent", "stop-details-null", "category-null"],
)
def test_refusal_hint_omits_category_when_absent(response: SimpleNamespace) -> None:
    # The API documents both `stop_details` and its `category` as nullable
    # even on a refusal.
    hint = _stop_reason_hint("refusal", response)
    assert "policy refusal" in hint
    assert "category" not in hint


def test_structured_call_names_a_refusal_whose_text_is_truncated() -> None:
    # lode-pdss: the text block is cut off mid-JSON, so it cannot validate;
    # stop_reason is read first, so this is a policy refusal, not a schema
    # mismatch.
    message = str(_structured_call_with(_refused_response()))

    assert "did not match" not in message
    assert "not visible from here" not in message
    _assert_refusal_hint(message)


def test_structured_call_names_a_refusal_with_no_text_block() -> None:
    error = _structured_call_with(_refused_response(text=None))

    _assert_refusal_hint(str(error))
    # lode-g9q3: the structured signal callers branch on, never the message.
    assert error.refusal is True
    assert error.refusal_category == "cyber"


def test_run_tool_turns_final_turn_names_a_refusal() -> None:
    tool = ToolSpec(
        name="lookup_widget", description="Look up a widget.", input_schema={}
    )
    client = mock.MagicMock()
    client.messages.create.side_effect = [
        _tool_use_response("lookup_widget", {}),
        _refused_response(),
    ]

    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).run_tool_turns(
            model="claude-sonnet-4-6",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: "r",
            output_schema=_Widget,
            max_tokens=100,
            timeout_s=30.0,
            max_tool_turns=1,
        )

    assert "did not match" not in str(excinfo.value)
    _assert_refusal_hint(str(excinfo.value))


@pytest.mark.network
@pytest.mark.parametrize(
    ("stop_details", "category"),
    [
        ({"type": "refusal", "category": "cyber", "explanation": None}, "cyber"),
        (None, None),
    ],
    ids=["with-category", "stop-details-null"],
)
def test_collect_batch_names_a_refusal_on_a_succeeded_result(
    stop_details: dict | None, category: str | None
) -> None:
    # Driven through the real SDK decoder rather than a MagicMock: a batch
    # line is built unvalidated (see the wrong-shape tests above), so only the
    # real decoder shows what `message.stop_details` actually is on this route.
    payload = _succeeded_payload("ver-3")
    payload["result"]["message"].update(
        content=[], stop_reason="refusal", stop_details=stop_details
    )
    client = _real_anthropic_client(
        _results_handler(
            "batch-1", lambda: httpx2.Response(200, content=_jsonl(payload))
        )
    )

    _, results = AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    (result,) = results
    assert result.outcome == "errored"
    assert "no text block" in str(result.error)
    _assert_refusal_hint(str(result.error), category=category)
    assert result.error.refusal is True
    assert result.error.refusal_category == category


def test_structured_refusal_whose_text_validates_is_not_returned() -> None:
    # lode-mzuk: a refused message whose text happens to validate is still
    # discarded.
    response = _refused_response(text=_Widget(name="w").model_dump_json())

    error = _structured_call_with(response)
    message = str(error)

    assert "partial content discarded" in message
    _assert_refusal_hint(message)
    assert error.refusal is True
    assert error.refusal_category == "cyber"


@pytest.mark.parametrize("partial_tool_use", [True, False], ids=["tool-use", "empty"])
def test_run_tool_turns_refused_free_turn_raises_without_running_tools(
    partial_tool_use: bool,
) -> None:
    tool = ToolSpec(
        name="lookup_widget", description="Look up a widget.", input_schema={}
    )
    refused = _tool_use_response("lookup_widget", {})
    refused.stop_reason = "refusal"
    refused.stop_details = SimpleNamespace(category="cyber")
    if not partial_tool_use:
        refused.content = []
    client = mock.MagicMock()
    client.messages.create.side_effect = [refused]
    ran: list[str] = []

    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).run_tool_turns(
            model="claude-sonnet-4-6",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: ran.append(name) or "r",
            output_schema=_Widget,
            max_tokens=100,
            timeout_s=30.0,
            max_tool_turns=2,
        )

    assert ran == []
    # The one free turn only: no final structured-output turn was attempted.
    assert client.messages.create.call_count == 1
    _assert_refusal_hint(str(excinfo.value))


def test_run_tool_turns_max_tokens_free_turn_raises_without_running_tools() -> None:
    tool = ToolSpec(
        name="lookup_widget", description="Look up a widget.", input_schema={}
    )
    truncated = _tool_use_response("lookup_widget", {})
    truncated.stop_reason = "max_tokens"
    client = mock.MagicMock()
    client.messages.create.side_effect = [truncated]
    ran: list[str] = []

    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).run_tool_turns(
            model="claude-sonnet-4-6",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: ran.append(name) or "r",
            output_schema=_Widget,
            max_tokens=100,
            timeout_s=30.0,
            max_tool_turns=2,
        )

    assert ran == []
    # The one free turn only: no final structured-output turn was attempted.
    assert client.messages.create.call_count == 1
    client.messages.parse.assert_not_called()
    assert "stop_reason='max_tokens'" in str(excinfo.value)
    assert _BUDGET_EXHAUSTED_HINT in str(excinfo.value)


def test_run_tool_turns_context_window_free_turn_raises_without_running_tools() -> None:
    tool = ToolSpec(
        name="lookup_widget", description="Look up a widget.", input_schema={}
    )
    truncated = _tool_use_response("lookup_widget", {})
    truncated.stop_reason = "model_context_window_exceeded"
    client = mock.MagicMock()
    client.messages.create.side_effect = [truncated]
    ran: list[str] = []

    with pytest.raises(LLMProviderError) as excinfo:
        AnthropicProvider(client).run_tool_turns(
            model="claude-sonnet-4-6",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            tools=(tool,),
            tool_result=lambda name, args: ran.append(name) or "r",
            output_schema=_Widget,
            max_tokens=100,
            timeout_s=30.0,
            max_tool_turns=2,
        )

    assert ran == []
    assert client.messages.create.call_count == 1
    client.messages.parse.assert_not_called()
    assert "stop_reason='model_context_window_exceeded'" in str(excinfo.value)
    assert _CONTEXT_WINDOW_EXCEEDED_HINT in str(excinfo.value)


@pytest.mark.network
def test_collect_batch_refusal_with_valid_text_is_errored_real_sdk() -> None:
    payload = _succeeded_payload("ver-4")
    payload["result"]["message"].update(stop_reason="refusal", stop_details=None)
    client = _real_anthropic_client(
        _results_handler(
            "batch-1", lambda: httpx2.Response(200, content=_jsonl(payload))
        )
    )

    _, results = AnthropicProvider(client).collect_batch("batch-1", timeout_s=10.0)

    (result,) = results
    assert result.outcome == "errored"
    assert result.parsed is None
    assert "incomplete text block discarded" in str(result.error)
    _assert_refusal_hint(str(result.error), category=None)


# ---------------------------------------------------------------------------
# OpenAIProvider.structured_call (lode-568v.3)
# ---------------------------------------------------------------------------


def _fake_responses_client(
    *, output_text: str = "", status: str = "completed", output: list | None = None
) -> mock.MagicMock:
    response = SimpleNamespace(
        status=status,
        output=output if output is not None else [],
        output_text=output_text,
        incomplete_details=None,
        error=None,
    )
    client = mock.MagicMock()
    client.responses.create.return_value = response
    return client


def _openai_bad_request() -> object:
    """A real ``openai.BadRequestError`` -- the OpenAI sibling of
    :func:`_anthropic_bad_request`; see that helper for why a real instance
    rather than a duck-typed fake.
    """
    import openai

    message = "reasoning.effort is not supported for this model"
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(
        400,
        request=request,
        headers={"x-request-id": "req-test-2"},
        json={"error": {"message": message}},
    )
    return openai.BadRequestError(message, response=response, body=response.json())


def test_openai_effort_levels_match_the_installed_sdk_literal() -> None:
    # The ticket this level set came from claimed a stale 4-value set
    # (minimal/low/medium/high); deriving from the SDK's own Literal instead of
    # hand-typing that claim is exactly what this test exists to enforce.
    import typing

    from openai.types.shared_params.reasoning import Reasoning

    effort_hint = typing.get_type_hints(Reasoning)["effort"]
    # `effort: Optional[ReasoningEffort]` -- unwrap the Optional, then the Literal.
    literal, _none = typing.get_args(effort_hint)
    assert typing.get_args(literal) == _OPENAI_EFFORT_LEVELS


@pytest.mark.parametrize("bad_effort", ["LOW", "extreme", "", "medium ", "effort"])
def test_openai_structured_call_rejects_an_invalid_effort_value(
    bad_effort: str,
) -> None:
    # lode-90o7: mirrors the pre-existing Anthropic pre-flight value check --
    # an invalid effort value fails clearly (LLMProviderError, before any
    # request is sent), closing OpenAIProvider's half of the asymmetry.
    client = mock.MagicMock()
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.structured_call(
            model="gpt-5.5",
            reasoning_effort=bad_effort,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )

    assert bad_effort in str(excinfo.value)
    assert excinfo.value.provider == "openai"
    client.responses.create.assert_not_called()


def test_openai_structured_call_wraps_a_bad_request_from_unsupported_pairing() -> None:
    # lode-90o7 AC: "tests cover the unsupported-pairing path for BOTH
    # providers". OpenAIProvider._error_from_exception already wrapped every
    # SDK exception from responses.create (predates this ticket) -- this pins
    # that guarantee for the reasoning_effort pairing specifically, so the
    # OpenAI half of the ticket's claim can't silently regress.
    # test_openai_structured_call_maps_sdk_exception_diagnostics below covers
    # the same wrap generically.
    bad_request = _openai_bad_request()
    client = mock.MagicMock()
    client.responses.create.side_effect = bad_request
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.structured_call(
            model="gpt-5.5",
            reasoning_effort="high",
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )

    err = excinfo.value
    assert err.provider == "openai"
    assert err.status_code == 400
    assert err.request_id == "req-test-2"
    assert err.__cause__ is bad_request


def test_openai_structured_call_builds_json_schema_format() -> None:
    client = _fake_responses_client(output_text='{"name": "widget", "count": 3}')
    provider = OpenAIProvider(
        client,
        endpoint="https://foo.openai.azure.com",
        api_version="2025-04-01-preview",
    )

    result = provider.structured_call(
        model="gpt-5.5",
        reasoning_effort="high",
        system="sys",
        user_prompt="prompt",
        output_schema=_Widget,
        max_tokens=100,
        timeout_s=42.0,
        tool_name="extract_widget",
        tool_description="Extract a widget.",
    )

    assert result == _Widget(name="widget", count=3)
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["model"] == "gpt-5.5"
    assert kwargs["instructions"] == "sys"
    assert kwargs["input"] == "prompt"
    assert kwargs["max_output_tokens"] == 100
    assert kwargs["timeout"] == 42.0
    assert kwargs["reasoning"] == {"effort": "high"}
    fmt = kwargs["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["name"] == "extract_widget"
    assert fmt["strict"] is False  # deliberate -- see module docstring
    assert fmt["description"] == "Extract a widget."
    assert fmt["schema"] == _Widget.model_json_schema()


def test_openai_structured_call_uses_schema_name_when_no_tool_name() -> None:
    client = _fake_responses_client(output_text='{"name": "w"}')
    provider = OpenAIProvider(client)

    provider.structured_call(
        model="gpt-5.5",
        reasoning_effort=None,
        system="sys",
        user_prompt="p",
        output_schema=_Widget,
        max_tokens=10,
        timeout_s=1.0,
    )

    fmt = client.responses.create.call_args.kwargs["text"]["format"]
    assert fmt["name"] == "_Widget"
    assert "reasoning" not in client.responses.create.call_args.kwargs
    assert "description" not in fmt


def test_openai_structured_call_raises_on_schema_mismatch() -> None:
    client = _fake_responses_client(output_text='{"count": "not-a-widget-name"}')
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError, match="did not match"):
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )


def test_openai_structured_call_raises_on_unparseable_json() -> None:
    client = _fake_responses_client(output_text="not json at all")
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError, match="not valid JSON"):
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )


def test_openai_structured_call_raises_on_non_object_json() -> None:
    # Non-strict json_schema mode is best-effort: the model can emit a top-level
    # `null` (or array). json.loads succeeds but the value is not an object --
    # must surface as a clean LLMProviderError, not a raw pydantic error.
    client = _fake_responses_client(output_text="null")
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError, match="not an object"):
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )


def test_openai_refusal_item_is_not_a_json_object_after_walk() -> None:
    # A top-level JSON array is likewise a non-object payload -- same guard.
    client = _fake_responses_client(output_text="[1, 2, 3]")
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError, match="not an object"):
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )


def test_openai_structured_call_raises_on_refusal() -> None:
    refusal_item = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="refusal", refusal="cannot help with that")],
    )
    client = _fake_responses_client(output_text="", output=[refusal_item])
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError, match="refused") as excinfo:
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )
    # lode-g9q3: the provider-neutral structured signal, category absent.
    assert excinfo.value.refusal is True
    assert excinfo.value.refusal_category is None


def test_openai_batch_refusal_survives_the_serialize_roundtrip() -> None:
    refusal_item = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="refusal", refusal="cannot help with that")],
    )
    provider = OpenAIProvider(
        _fake_responses_client(output_text="", output=[refusal_item])
    )

    handle = provider.submit_batch([_batch_request()], timeout_s=1.0)
    _, results = provider.collect_batch(handle, timeout_s=1.0)

    (result,) = results
    assert result.outcome == "errored"
    assert result.error.refusal is True


def test_openai_non_refusal_failure_is_not_marked_refusal() -> None:
    provider = OpenAIProvider(_fake_responses_client(output_text=""))

    handle = provider.submit_batch([_batch_request()], timeout_s=1.0)
    _, results = provider.collect_batch(handle, timeout_s=1.0)

    assert results[0].error.refusal is False


def test_openai_structured_call_raises_on_no_text_output() -> None:
    client = _fake_responses_client(output_text="")
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError, match="no text output"):
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )


def test_openai_structured_call_raises_on_incomplete_status() -> None:
    incomplete = SimpleNamespace(reason="content_filter")
    response = SimpleNamespace(
        status="incomplete",
        output=[],
        output_text="",
        incomplete_details=incomplete,
        error=None,
    )
    client = mock.MagicMock()
    client.responses.create.return_value = response
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError, match="content_filter"):
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )


def test_openai_structured_call_maps_sdk_exception_diagnostics() -> None:
    class _FakeAPIError(Exception):
        status_code = 400
        request_id = "req-1"
        body: ClassVar = {
            "error": {
                "message": "content filtered",
                "innererror": {"content_filter_result": {"hate": {"filtered": True}}},
            }
        }

    client = mock.MagicMock()
    client.responses.create.side_effect = _FakeAPIError("bad request")
    provider = OpenAIProvider(client)

    with pytest.raises(LLMProviderError) as excinfo:
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )

    err = excinfo.value
    assert err.provider == "openai"
    assert err.status_code == 400
    assert err.request_id == "req-1"
    assert "content_filter" in str(err)


# ---------------------------------------------------------------------------
# OpenAIProvider.submit_batch / collect_batch (serialize, lode-568v.3)
# ---------------------------------------------------------------------------


def test_openai_submit_batch_serializes_requests_and_collect_returns_ended() -> None:
    client = _fake_responses_client(output_text='{"name": "w", "count": 2}')
    provider = OpenAIProvider(client)

    handle = provider.submit_batch(
        [_batch_request(custom_id="ver-1", tool_name="extract_widget")],
        timeout_s=30.0,
    )

    # No network call in collect -- decodes the handle immediately (docs/stack.md).
    client.responses.create.reset_mock()
    status, results = provider.collect_batch(handle, timeout_s=30.0)

    client.responses.create.assert_not_called()
    assert status == "ended"
    (result,) = results
    assert result.custom_id == "ver-1"
    assert result.outcome == "succeeded"
    assert result.error is None
    assert result.parsed.root == {"name": "w", "count": 2}
    assert _Widget.model_validate(result.parsed.root) == _Widget(name="w", count=2)


def test_openai_submit_batch_captures_a_per_request_failure_as_errored() -> None:
    client = mock.MagicMock()
    client.responses.create.side_effect = RuntimeError("boom")
    provider = OpenAIProvider(client)

    handle = provider.submit_batch(
        [_batch_request(custom_id="ver-2", tool_name="extract_widget")],
        timeout_s=30.0,
    )
    status, results = provider.collect_batch(handle, timeout_s=30.0)

    assert status == "ended"
    (result,) = results
    assert result.custom_id == "ver-2"
    assert result.outcome == "errored"
    assert result.parsed is None
    assert isinstance(result.error, LLMProviderError)
    assert result.error.provider == "openai"


def test_openai_submit_batch_captures_an_invalid_effort_value_as_errored() -> None:
    # lode-90o7: the pre-flight value check inside _call_responses_raw raises
    # LLMProviderError before any request is sent; submit_batch's existing
    # per-request `except LLMProviderError` turns that into an `errored`
    # result the same way it already does for any other call failure.
    client = mock.MagicMock()
    provider = OpenAIProvider(client)

    handle = provider.submit_batch(
        [
            _batch_request(
                custom_id="ver-5",
                tool_name="extract_widget",
                reasoning_effort="not-a-real-level",
            )
        ],
        timeout_s=30.0,
    )
    status, results = provider.collect_batch(handle, timeout_s=30.0)

    assert status == "ended"
    (result,) = results
    assert result.custom_id == "ver-5"
    assert result.outcome == "errored"
    assert result.parsed is None
    assert "not-a-real-level" in str(result.error)
    client.responses.create.assert_not_called()


def test_openai_submit_batch_non_object_payload_does_not_poison_collect() -> None:
    # Regression (lode-568v.3 correctness review): a model emitting top-level
    # `null` under non-strict mode must NOT make collect_batch raise a raw
    # pydantic error while building RootModel[dict](...) -- an unguarded raise
    # there escapes worker.drain and, since the payload is persisted inline in
    # the handle, re-crashes every subsequent drain, permanently wedging ALL
    # enrich-batch collection. It must instead land as a per-request `errored`
    # result that drains as an ordinary transient failure.
    client = _fake_responses_client(output_text="null")
    provider = OpenAIProvider(client)

    handle = provider.submit_batch(
        [_batch_request(custom_id="ver-3", tool_name="extract_widget")],
        timeout_s=30.0,
    )
    status, results = provider.collect_batch(handle, timeout_s=30.0)

    assert status == "ended"
    (result,) = results
    assert result.custom_id == "ver-3"
    assert result.outcome == "errored"
    assert result.parsed is None
    assert isinstance(result.error, LLMProviderError)
    assert "not an object" in str(result.error)


# ---------------------------------------------------------------------------
# build_provider
# ---------------------------------------------------------------------------


def test_build_provider_anthropic_wraps_build_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_client = object()
    monkeypatch.setattr("lode.llm_provider.build_client", lambda: sentinel_client)

    provider = build_provider(Settings())

    assert isinstance(provider, AnthropicProvider)
    assert provider._client is sentinel_client


def test_build_provider_propagates_auth_error_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # lode-568v.2 implementation note (docs/decisions.md): AuthError propagates
    # unchanged rather than being wrapped into LLMAuthError, preserving
    # worker.py's lode-9yy exception handling byte-for-byte.
    from lode.auth import AuthError

    def _no_credentials() -> object:
        raise AuthError("no credentials (test)")

    monkeypatch.setattr("lode.llm_provider.build_client", _no_credentials)

    with pytest.raises(AuthError):
        build_provider(Settings())


def test_build_provider_openai_direct_resolves_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    sentinel_client = object()
    make_client = mock.MagicMock(return_value=sentinel_client)
    # Patch the real SDK constructor (conftest's guard 1 would otherwise fail
    # this test outright if it reached a real openai.OpenAI() construction --
    # mirrors test_build_provider_anthropic_wraps_build_client's approach).
    monkeypatch.setattr(openai, "OpenAI", make_client)

    provider = build_provider(Settings(llm_provider="openai"))

    assert isinstance(provider, OpenAIProvider)
    assert provider._provider_id == "openai"
    assert provider._client is sentinel_client
    make_client.assert_called_once_with(api_key="sk-test")


def test_build_provider_openai_missing_key_raises_llm_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)

    with pytest.raises(LLMAuthError, match="OPENAI_API_KEY"):
        build_provider(Settings(llm_provider="openai"))


def test_build_provider_openai_azure_resolves_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sentinel_client = object()
    make_client = mock.MagicMock(return_value=sentinel_client)
    monkeypatch.setattr(openai, "AzureOpenAI", make_client)

    settings = Settings(
        llm_provider="openai",
        azure_openai_endpoint="https://foo.openai.azure.com",
        azure_openai_api_version="2025-04-01-preview",
    )
    provider = build_provider(settings)

    assert isinstance(provider, OpenAIProvider)
    assert provider._client is sentinel_client
    assert provider._endpoint == "https://foo.openai.azure.com"
    assert provider._api_version == "2025-04-01-preview"
    make_client.assert_called_once_with(
        azure_endpoint="https://foo.openai.azure.com",
        api_version="2025-04-01-preview",
        api_key="azure-key",
    )


def test_build_provider_openai_azure_missing_key_raises_llm_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)

    settings = Settings(
        llm_provider="openai",
        azure_openai_endpoint="https://foo.openai.azure.com",
        azure_openai_api_version="2025-04-01-preview",
    )
    with pytest.raises(LLMAuthError, match="AZURE_OPENAI_API_KEY"):
        build_provider(settings)


# ---------------------------------------------------------------------------
# Error classes
# ---------------------------------------------------------------------------


def test_llm_provider_error_carries_diagnostic_fields() -> None:
    err = LLMProviderError(
        "boom", provider="anthropic", status_code=429, request_id="req-1"
    )
    assert str(err) == "boom"
    assert err.provider == "anthropic"
    assert err.status_code == 429
    assert err.request_id == "req-1"


def test_llm_auth_error_is_a_llm_provider_error() -> None:
    err = LLMAuthError("no creds", provider="openai")
    assert isinstance(err, LLMProviderError)


# ---------------------------------------------------------------------------
# provider_identity (lode-568v.4 -- provenance)
# ---------------------------------------------------------------------------


def test_provider_identity_is_none_for_anthropic() -> None:
    # NULL means "anthropic" by convention (docs/decisions.md lode-568v.1) --
    # settings.llm_provider is Literal["anthropic"] today, the only value real
    # Settings construction accepts.
    assert provider_identity(Settings()) is None


def test_provider_identity_returns_the_literal_string_for_non_anthropic() -> None:
    # A duck-typed stand-in (real Settings can't hold a non-anthropic value
    # yet, per its Literal["anthropic"] annotation) -- provider_identity only
    # reads .llm_provider, so this exercises the future-provider branch ahead
    # of lode-568v.3 landing a second one.
    fake_settings = SimpleNamespace(llm_provider="openai")
    assert provider_identity(fake_settings) == "openai"
