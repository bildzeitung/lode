"""Tests for lode.llm_provider -- the vendor-neutral LLMProvider seam (lode-568v.2).

Covers what the enrich/qa/cited_answer/worker/cli test suites only exercise
indirectly (through AnthropicProvider-wrapped fakes): ModelTier coercion, the
AnthropicProvider wire mapping for structured_call/submit_batch/collect_batch,
and build_provider's provider resolution.
"""

import unittest.mock as mock
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from lode.config import Settings
from lode.llm_provider import (
    AnthropicProvider,
    BatchRequest,
    LLMAuthError,
    LLMProviderError,
    ModelTier,
    build_provider,
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
    with pytest.raises(Exception):  # noqa: B017 -- pydantic ValidationError on frozen assign
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
    client.messages.create.assert_not_called()


def test_structured_call_ignores_reasoning_effort() -> None:
    # Anthropic has no reasoning_effort axis -- must not surface in the call.
    client = mock.MagicMock()
    client.messages.parse.return_value = SimpleNamespace(
        parsed_output=_Widget(name="w")
    )
    provider = AnthropicProvider(client)

    provider.structured_call(
        model="m",
        reasoning_effort="high",
        system="sys",
        user_prompt="p",
        output_schema=_Widget,
        max_tokens=10,
        timeout_s=1.0,
    )

    assert "reasoning_effort" not in client.messages.parse.call_args.kwargs


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
