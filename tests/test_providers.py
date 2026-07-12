import pytest
from pydantic import ValidationError

from backpack_bench.providers.anthropic import AnthropicMessagesAdapter
from backpack_bench.providers.base import profile_hash
from backpack_bench.providers.openai import OpenAIChatAdapter
from backpack_bench.schemas import ModelProfile


def test_openai_mapping_and_default_token_limit() -> None:
    profile = ModelProfile.model_validate(
        {
            "id": "openai-test",
            "protocol": "openai_chat",
            "base_url": "https://example.test/v1",
            "model": "reasoner",
            "auth_mode": "none",
            "params": {"thinking_effort": "high"},
        }
    )
    adapter = OpenAIChatAdapter()
    assert adapter.headers(profile, None)["Accept"] == "text/event-stream"
    body = adapter.body(profile, "prompt")
    assert body["reasoning_effort"] == "high"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert "max_tokens" not in body
    parsed = adapter.parse(
        {
            "id": "x",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "{}", "reasoning_content": "thinking"},
                }
            ],
            "usage": {"completion_tokens": 10},
        }
    )
    assert parsed.content == "{}"
    assert parsed.reasoning == "thinking"
    content_event = adapter.parse_stream_event(
        {
            "id": "stream-x",
            "choices": [
                {
                    "delta": {"content": "{", "reasoning_content": "think"},
                    "finish_reason": None,
                }
            ],
        }
    )
    usage_event = adapter.parse_stream_event({"choices": [], "usage": {"completion_tokens": 7}})
    assert content_event.content_delta == "{"
    assert content_event.reasoning_delta == "think"
    assert usage_event.usage["completion_tokens"] == 7


def test_anthropic_adaptive_effort_and_truncation() -> None:
    profile = ModelProfile.model_validate(
        {
            "id": "anthropic-test",
            "protocol": "anthropic_messages",
            "base_url": "https://example.test/v1",
            "model": "reasoner",
            "auth_mode": "none",
            "params": {
                "max_tokens": 8192,
                "thinking_mode": "adaptive",
                "thinking_effort": "high",
            },
        }
    )
    adapter = AnthropicMessagesAdapter()
    assert adapter.headers(profile, None)["Accept"] == "text/event-stream"
    body = adapter.body(profile, "prompt")
    assert body["stream"] is True
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    parsed = adapter.parse(
        {
            "id": "x",
            "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "unfinished"}],
            "usage": {"output_tokens": 8192},
        }
    )
    assert parsed.content == ""
    assert parsed.finish_reason == "length"
    delta = adapter.parse_stream_event(
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "{"},
        }
    )
    final = adapter.parse_stream_event(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "max_tokens"},
            "usage": {"output_tokens": 12},
        }
    )
    assert delta.content_delta == "{"
    assert final.finish_reason == "length"
    assert final.usage["output_tokens"] == 12


def test_anthropic_default_omits_max_tokens() -> None:
    profile = ModelProfile.model_validate(
        {
            "id": "anthropic-unlimited",
            "protocol": "anthropic_messages",
            "base_url": "https://example.test/v1",
            "model": "model",
            "auth_mode": "none",
        }
    )
    body = AnthropicMessagesAdapter().body(profile, "prompt")
    assert body["stream"] is True
    assert "max_tokens" not in body


def test_manual_anthropic_thinking_requires_valid_output_budget() -> None:
    with pytest.raises(ValidationError, match="budget"):
        ModelProfile.model_validate(
            {
                "id": "bad-thinking",
                "protocol": "anthropic_messages",
                "base_url": "https://example.test/v1",
                "model": "model",
                "auth_mode": "none",
                "params": {
                    "max_tokens": 2048,
                    "thinking_mode": "enabled",
                    "thinking_budget": 2048,
                },
            }
        )


def test_profile_identity_excludes_key_name_and_normalizes_endpoint() -> None:
    first = ModelProfile.model_validate(
        {
            "id": "first",
            "protocol": "openai_chat",
            "base_url": "https://example.test/v1",
            "model": "model",
            "api_key_env": "FIRST_KEY",
        }
    )
    second = ModelProfile.model_validate(
        {
            "id": "second",
            "protocol": "openai_chat",
            "base_url": "https://example.test",
            "endpoint": "/v1/chat/completions",
            "model": "model",
            "api_key_env": "SECOND_KEY",
            "auth_mode": "bearer",
        }
    )
    assert profile_hash(first) == profile_hash(second)


def test_extra_body_cannot_replace_prompt_or_contain_credentials() -> None:
    invalid_bodies: list[dict[str, object]] = [
        {"messages": []},
        {"stream": False},
        {"api_key": "not-allowed"},
        {"provider_options": {"authorization": "not-allowed"}},
    ]
    for extra_body in invalid_bodies:
        with pytest.raises(ValidationError):
            ModelProfile.model_validate(
                {
                    "id": "invalid-extra",
                    "protocol": "openai_chat",
                    "base_url": "https://example.test/v1",
                    "model": "model",
                    "auth_mode": "none",
                    "params": {"extra_body": extra_body},
                }
            )


def test_credentials_are_forbidden_in_endpoint_url() -> None:
    with pytest.raises(ValidationError, match="URL query"):
        ModelProfile.model_validate(
            {
                "id": "invalid-url",
                "protocol": "openai_chat",
                "base_url": "https://example.test/v1?api_key=not-allowed",
                "model": "model",
                "api_key_env": "SAFE_ENV_NAME",
            }
        )
