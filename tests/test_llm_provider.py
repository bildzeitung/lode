"""Tests for lode.llm_provider -- the vendor-neutral LLMProvider seam (lode-568v.2/.3).

Covers what the enrich/qa/cited_answer/worker/cli test suites only exercise
indirectly (through AnthropicProvider-wrapped fakes): ModelTier coercion, the
AnthropicProvider wire mapping for structured_call/submit_batch/collect_batch,
the OpenAIProvider (lode-568v.3) Responses API mapping + serialize-batch, and
build_provider's provider resolution for both providers.
"""

from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import pytest
from pydantic import BaseModel, ValidationError

from lode.config import Settings
from lode.llm_provider import (
    AnthropicProvider,
    BatchRequest,
    LLMAuthError,
    LLMProviderError,
    ModelTier,
    OpenAIProvider,
    build_provider,
    provider_identity,
)


class _Widget(BaseModel):
    name: str
    count: int = 0


# ---------------------------------------------------------------------------
# ModelTier
# ---------------------------------------------------------------------------


def test_model_tier_coerces_from_a_bare_string() -> None:
    tier = ModelTier.model_validate("claude-haiku-4-5")
    assert tier.model == "claude-haiku-4-5"
    assert tier.reasoning_effort is None


def test_model_tier_accepts_explicit_fields() -> None:
    tier = ModelTier(model="gpt-5.5", reasoning_effort="high")
    assert tier.model == "gpt-5.5"
    assert tier.reasoning_effort == "high"


def test_model_tier_is_frozen() -> None:
    tier = ModelTier(model="x")
    with pytest.raises(Exception):
        tier.model = "y"


# ---------------------------------------------------------------------------
# AnthropicProvider.structured_call
# ---------------------------------------------------------------------------


def _fake_tool_use_client(payload: dict) -> mock.MagicMock:
    tool_block = mock.MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = payload
    response = mock.MagicMock()
    response.content = [tool_block]
    client = mock.MagicMock()
    client.messages.create.return_value = response
    return client


def test_structured_call_forces_tool_use_when_tool_name_given() -> None:
    client = _fake_tool_use_client({"name": "widget", "count": 3})
    provider = AnthropicProvider(client)

    result = provider.structured_call(
        model="claude-haiku-4-5",
        reasoning_effort=None,
        system="sys",
        user_prompt="prompt",
        output_schema=_Widget,
        max_tokens=100,
        timeout_s=42.0,
        tool_name="extract_widget",
        tool_description="Extract a widget.",
    )

    assert isinstance(result, _Widget)
    assert result == _Widget(name="widget", count=3)

    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == 100
    assert kwargs["system"] == "sys"
    assert kwargs["timeout"] == 42.0
    assert kwargs["tool_choice"] == {"type": "tool", "name": "extract_widget"}
    assert kwargs["tools"] == [
        {
            "name": "extract_widget",
            "description": "Extract a widget.",
            "input_schema": _Widget.model_json_schema(),
        }
    ]
    assert kwargs["messages"] == [{"role": "user", "content": "prompt"}]
    # lode-d1sr: no thinking default to disable on this branch -- see the
    # AnthropicProvider docstring.
    assert "thinking" not in kwargs


def test_structured_call_uses_messages_parse_when_no_tool_name() -> None:
    client = mock.MagicMock()
    client.messages.parse.return_value = SimpleNamespace(
        parsed_output=_Widget(name="w", count=1)
    )
    provider = AnthropicProvider(client)

    result = provider.structured_call(
        model="claude-sonnet-4-6",
        reasoning_effort=None,
        system="sys",
        user_prompt="prompt",
        output_schema=_Widget,
        max_tokens=50,
        timeout_s=7.0,
    )

    assert result == _Widget(name="w", count=1)
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["output_format"] is _Widget
    assert kwargs["timeout"] == 7.0
    # lode-3dlt: `thinking` is never sent on this branch -- an explicit
    # `disabled` (lode-d1sr) 400s on Fable-class models at any effort and on
    # Opus 5 at effort xhigh/max. See the AnthropicProvider docstring.
    assert "thinking" not in kwargs
    client.messages.create.assert_not_called()


def test_structured_call_omits_thinking_for_a_fable_class_model() -> None:
    # lode-3dlt: the regression this ticket exists to fix -- an explicit
    # thinking={"type": "disabled"} 400s on Fable-class models at ANY effort
    # level. No model-family branching in the fix, so the same assertion as
    # the default-tier test above holds here too -- proving there is no
    # per-model code path that could reintroduce the illegal value.
    client = mock.MagicMock()
    client.messages.parse.return_value = SimpleNamespace(
        parsed_output=_Widget(name="w", count=1)
    )
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
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-fable-5"
    assert "thinking" not in kwargs


def test_structured_call_raises_when_the_response_has_no_text_block() -> None:
    # lode-3dlt: with `thinking` no longer pinned off, a response can spend its
    # whole max_tokens budget inside thinking and come back with no text block
    # at all -- the SDK's parsed_output is then None. Unguarded that None
    # escapes under the `-> BaseModelT` annotation and fails as an
    # AttributeError inside qa.answer_question; it must be an LLMProviderError
    # naming the cause instead.
    client = mock.MagicMock()
    client.messages.parse.return_value = SimpleNamespace(
        parsed_output=None, stop_reason="max_tokens"
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


def test_structured_call_wraps_a_schema_validation_failure() -> None:
    # lode-3dlt: messages.parse validates the response text against
    # output_schema inside the SDK, so a text block truncated mid-JSON (newly
    # more reachable now that thinking shares the budget) raises a raw pydantic
    # ValidationError out of the SDK's own post-processing. The seam must
    # convert it, exactly as OpenAIProvider already does.
    client = mock.MagicMock()
    with pytest.raises(ValidationError) as raised:
        _Widget.model_validate_json('{"name": "w", "cou')
    client.messages.parse.side_effect = raised.value
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

    assert "_Widget" in str(excinfo.value)
    assert excinfo.value.provider == "anthropic"
    assert isinstance(excinfo.value.__cause__, ValidationError)


def test_structured_call_sends_effort_on_the_messages_parse_branch() -> None:
    # lode-wnz1: reasoning_effort now reaches Anthropic as
    # output_config.effort on the messages.parse branch. Must never surface
    # as a raw `reasoning_effort` kwarg -- that's not a real SDK parameter.
    client = mock.MagicMock()
    client.messages.parse.return_value = SimpleNamespace(
        parsed_output=_Widget(name="w")
    )
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

    kwargs = client.messages.parse.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    assert kwargs["output_config"] == {"effort": "high"}
    # output_format must survive alongside output_config -- the SDK's own
    # .parse() merges the two into {"format": ..., "effort": ...} internally;
    # this asserts the wiring didn't drop or replace it.
    assert kwargs["output_format"] is _Widget


def test_structured_call_omits_output_config_when_reasoning_effort_is_none() -> None:
    client = mock.MagicMock()
    client.messages.parse.return_value = SimpleNamespace(
        parsed_output=_Widget(name="w")
    )
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

    assert "output_config" not in client.messages.parse.call_args.kwargs


def test_structured_call_sends_effort_on_the_forced_tool_use_branch() -> None:
    client = _fake_tool_use_client({"name": "widget", "count": 3})
    provider = AnthropicProvider(client)

    provider.structured_call(
        model="claude-haiku-4-5",
        reasoning_effort="xhigh",
        system="sys",
        user_prompt="prompt",
        output_schema=_Widget,
        max_tokens=100,
        timeout_s=42.0,
        tool_name="extract_widget",
        tool_description="Extract a widget.",
    )

    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["output_config"] == {"effort": "xhigh"}


def test_structured_call_omits_output_config_on_forced_tool_use_when_unset() -> None:
    client = _fake_tool_use_client({"name": "widget", "count": 3})
    provider = AnthropicProvider(client)

    provider.structured_call(
        model="claude-haiku-4-5",
        reasoning_effort=None,
        system="sys",
        user_prompt="prompt",
        output_schema=_Widget,
        max_tokens=100,
        timeout_s=42.0,
        tool_name="extract_widget",
        tool_description="Extract a widget.",
    )

    assert "output_config" not in client.messages.create.call_args.kwargs


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
    client.messages.parse.assert_not_called()
    client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# AnthropicProvider.submit_batch / collect_batch
# ---------------------------------------------------------------------------


def _batch_request(**overrides: object) -> BatchRequest:
    defaults: dict = dict(
        custom_id="ver-1",
        model="claude-haiku-4-5",
        reasoning_effort=None,
        system="sys",
        user_prompt="prompt",
        output_schema=_Widget,
        max_tokens=100,
        tool_name="extract_widget",
        tool_description="Extract a widget.",
    )
    defaults.update(overrides)
    return BatchRequest(**defaults)


def test_submit_batch_builds_forced_tool_use_requests() -> None:
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
    assert req["params"]["tool_choice"] == {
        "type": "tool",
        "name": "extract_widget",
    }
    assert req["params"]["tools"][0]["description"] == "Extract a widget."


def test_submit_batch_omits_tools_when_no_tool_name() -> None:
    client = mock.MagicMock()
    client.beta.messages.batches.create.return_value = SimpleNamespace(id="batch-2")
    provider = AnthropicProvider(client)

    provider.submit_batch(
        [_batch_request(tool_name=None, tool_description=None)], timeout_s=30.0
    )

    (req,) = client.beta.messages.batches.create.call_args.kwargs["requests"]
    assert "tools" not in req["params"]
    assert "tool_choice" not in req["params"]


def test_submit_batch_sends_effort_when_reasoning_effort_is_set() -> None:
    # lode-wnz1: the batch path gets the same output_config.effort wiring as
    # the immediate structured_call path.
    client = mock.MagicMock()
    client.beta.messages.batches.create.return_value = SimpleNamespace(id="batch-3")
    provider = AnthropicProvider(client)

    provider.submit_batch([_batch_request(reasoning_effort="medium")], timeout_s=30.0)

    (req,) = client.beta.messages.batches.create.call_args.kwargs["requests"]
    assert req["params"]["output_config"] == {"effort": "medium"}


def test_submit_batch_omits_output_config_when_reasoning_effort_is_none() -> None:
    client = mock.MagicMock()
    client.beta.messages.batches.create.return_value = SimpleNamespace(id="batch-4")
    provider = AnthropicProvider(client)

    provider.submit_batch([_batch_request(reasoning_effort=None)], timeout_s=30.0)

    (req,) = client.beta.messages.batches.create.call_args.kwargs["requests"]
    assert "output_config" not in req["params"]


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


def _succeeded_result(custom_id: str, payload: dict) -> mock.MagicMock:
    tool_block = mock.MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = payload
    result_obj = mock.MagicMock()
    result_obj.custom_id = custom_id
    result_obj.result.type = "succeeded"
    result_obj.result.message.content = [tool_block]
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


def test_collect_batch_handles_a_succeeded_result_missing_a_tool_use_block() -> None:
    # No tool_use content block at all -- the provider must not blow up the
    # whole collect_batch call over one malformed result (see module docstring).
    client = mock.MagicMock()
    client.beta.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended"
    )
    bad = mock.MagicMock()
    bad.custom_id = "ver-3"
    bad.result.type = "succeeded"
    bad.result.message.content = []
    client.beta.messages.batches.results.return_value = iter([bad])
    provider = AnthropicProvider(client)

    status, results = provider.collect_batch("batch-1", timeout_s=10.0)

    assert status == "ended"
    (result,) = results
    assert result.outcome == "errored"
    assert result.parsed is None
    assert isinstance(result.error, LLMProviderError)


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

    with pytest.raises(LLMProviderError, match="refused"):
        provider.structured_call(
            model="m",
            reasoning_effort=None,
            system="sys",
            user_prompt="p",
            output_schema=_Widget,
            max_tokens=10,
            timeout_s=1.0,
        )


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
