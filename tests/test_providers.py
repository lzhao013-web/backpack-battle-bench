import io
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from backpack_bench.providers.anthropic import (
    PLACEMENT_ANSWER_SCHEMA,
    AnthropicMessagesAdapter,
)
from backpack_bench.providers.base import (
    MAX_PROMPT_IMAGE_PIXELS,
    PromptImage,
    profile_hash,
    prompt_images_with_overview,
    split_prompt_image,
)
from backpack_bench.providers.openai import OpenAIChatAdapter
from backpack_bench.providers.responses import OpenAIResponsesAdapter
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
    assert body["response_format"] == {"type": "json_object"}
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


def test_openai_responses_mapping_and_parsing() -> None:
    profile = ModelProfile.model_validate(
        {
            "id": "responses-test",
            "protocol": "openai_responses",
            "base_url": "https://example.test/v1",
            "model": "reasoner",
            "auth_mode": "none",
            "params": {
                "thinking_effort": "high",
                "max_tokens": 4096,
                "temperature": 0.2,
            },
        }
    )
    adapter = OpenAIResponsesAdapter()
    assert adapter.endpoint(profile) == "https://example.test/v1/responses"
    assert adapter.headers(profile, None)["Accept"] == "text/event-stream"
    body = adapter.body(profile, "prompt")
    assert body == {
        "model": "reasoner",
        "input": "prompt",
        "temperature": 0.2,
        "max_output_tokens": 4096,
        "reasoning": {"effort": "high"},
        "text": {"format": {"type": "json_object"}},
        "stream": True,
    }
    response = {
        "id": "resp_123",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "thinking"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "{}"}],
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 8},
    }
    parsed = adapter.parse(response)
    assert parsed.content == "{}"
    assert parsed.reasoning == "thinking"
    assert parsed.finish_reason == "stop"
    assert parsed.response_id == "resp_123"
    assert adapter.parse({"type": "response.completed", "response": response}) == parsed

    content_delta = adapter.parse_stream_event(
        {
            "type": "response.output_text.delta",
            "response_id": "resp_123",
            "delta": "{",
        }
    )
    reasoning_delta = adapter.parse_stream_event(
        {
            "type": "response.reasoning_summary_text.delta",
            "response_id": "resp_123",
            "delta": "think",
        }
    )
    incomplete = adapter.parse_stream_event(
        {
            "type": "response.incomplete",
            "response": {
                "id": "resp_123",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"output_tokens": 4096},
            },
        }
    )
    assert content_delta.content_delta == "{"
    assert reasoning_delta.reasoning_delta == "think"
    assert incomplete.finish_reason == "length"
    assert incomplete.usage == {"output_tokens": 4096}


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
    assert body["output_config"] == {
        "effort": "high",
        "format": {
            "type": "json_schema",
            "schema": PLACEMENT_ANSWER_SCHEMA,
        },
    }
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
    assert body["output_config"]["format"] == {
        "type": "json_schema",
        "schema": PLACEMENT_ANSWER_SCHEMA,
    }


@pytest.mark.parametrize(
    ("protocol", "adapter", "json_field"),
    [
        ("openai_chat", OpenAIChatAdapter(), "response_format"),
        ("openai_responses", OpenAIResponsesAdapter(), "text"),
        ("anthropic_messages", AnthropicMessagesAdapter(), "output_config"),
    ],
)
def test_json_mode_can_be_disabled(
    protocol: str,
    adapter: OpenAIChatAdapter | OpenAIResponsesAdapter | AnthropicMessagesAdapter,
    json_field: str,
) -> None:
    profile = ModelProfile.model_validate(
        {
            "id": f"{protocol}-no-json",
            "protocol": protocol,
            "base_url": "https://example.test/v1",
            "model": "model",
            "auth_mode": "none",
            "params": {"json_mode": False},
        }
    )
    assert json_field not in adapter.body(profile, "prompt")


def test_multimodal_image_mapping(tmp_path: Path) -> None:
    image_path = tmp_path / "sheet.png"
    image_path.write_bytes(b"fake-png")
    image = PromptImage(str(image_path))
    openai_profile = ModelProfile.model_validate(
        {
            "id": "openai-vision",
            "protocol": "openai_chat",
            "base_url": "https://example.test/v1",
            "model": "vision",
            "auth_mode": "none",
        }
    )
    responses_profile = openai_profile.model_copy(
        update={"id": "responses-vision", "protocol": "openai_responses"}
    )
    anthropic_profile = openai_profile.model_copy(
        update={"id": "anthropic-vision", "protocol": "anthropic_messages"}
    )
    openai_content = OpenAIChatAdapter().body(openai_profile, "prompt", image)["messages"][0][
        "content"
    ]
    assert openai_content[0] == {"type": "text", "text": "prompt"}
    assert openai_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    responses_content = OpenAIResponsesAdapter().body(responses_profile, "prompt", image)[
        "input"
    ][0]["content"]
    assert responses_content[0] == {"type": "input_text", "text": "prompt"}
    assert responses_content[1]["type"] == "input_image"
    assert responses_content[1]["image_url"].startswith("data:image/png;base64,")
    anthropic_content = AnthropicMessagesAdapter().body(anthropic_profile, "prompt", image)[
        "messages"
    ][0]["content"]
    assert anthropic_content[0] == {"type": "text", "text": "prompt"}
    assert anthropic_content[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "ZmFrZS1wbmc=",
    }


def test_large_image_is_evenly_split_and_all_parts_are_mapped(tmp_path: Path) -> None:
    image_path = tmp_path / "large.png"
    Image.new("RGB", (1601, 800), "white").save(image_path)
    parts = split_prompt_image(PromptImage(str(image_path)))

    assert len(parts) == 3
    sizes: list[tuple[int, int]] = []
    for part in parts:
        with Image.open(io.BytesIO(part.bytes_data())) as tile:
            sizes.append(tile.size)
            assert tile.width * tile.height <= MAX_PROMPT_IMAGE_PIXELS
    assert max(width for width, _ in sizes) - min(width for width, _ in sizes) <= 1
    assert {height for _, height in sizes} == {800}

    request_images = prompt_images_with_overview(PromptImage(str(image_path)))
    assert len(request_images) == 4
    assert request_images[0].is_overview
    assert all(not image.is_overview for image in request_images[1:])
    with Image.open(io.BytesIO(request_images[0].bytes_data())) as overview:
        assert overview.width * overview.height <= MAX_PROMPT_IMAGE_PIXELS
        assert overview.size[0] / overview.size[1] == pytest.approx(1601 / 800, rel=0.01)

    profile = ModelProfile.model_validate(
        {
            "id": "split-vision",
            "protocol": "openai_chat",
            "base_url": "https://example.test/v1",
            "model": "vision",
            "auth_mode": "none",
            "params": {"split_image": True},
        }
    )
    openai_content = OpenAIChatAdapter().body(profile, "prompt", request_images)["messages"][
        0
    ]["content"]
    assert "第 1 张图片是低分辨率的完整题面总览图" in openai_content[0]["text"]
    assert "随后 3 张图片是原始题面的高清分片" in openai_content[0]["text"]
    assert "1 行 × 3 列" in openai_content[0]["text"]
    assert "跨越分片边界" in openai_content[0]["text"]
    assert len([block for block in openai_content if block["type"] == "image_url"]) == 4

    responses_profile = profile.model_copy(update={"protocol": "openai_responses"})
    responses_content = OpenAIResponsesAdapter().body(
        responses_profile, "prompt", request_images
    )[
        "input"
    ][0]["content"]
    assert len([block for block in responses_content if block["type"] == "input_image"]) == 4

    anthropic_profile = profile.model_copy(update={"protocol": "anthropic_messages"})
    anthropic_content = AnthropicMessagesAdapter().body(
        anthropic_profile, "prompt", request_images
    )["messages"][0]["content"]
    assert len([block for block in anthropic_content if block["type"] == "image"]) == 4


def test_image_within_pixel_limit_is_not_reencoded(tmp_path: Path) -> None:
    image_path = tmp_path / "small.png"
    Image.new("RGB", (800, 800), "white").save(image_path)
    image = PromptImage(str(image_path))
    assert split_prompt_image(image) == (image,)


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
    split = first.model_copy(
        update={"params": first.params.model_copy(update={"split_image": True})}
    )
    assert profile_hash(first) != profile_hash(split)


def test_extra_body_cannot_replace_prompt_or_contain_credentials() -> None:
    invalid_bodies: list[dict[str, object]] = [
        {"messages": []},
        {"input": "replacement prompt"},
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


def test_proxy_url_validation_and_profile_identity() -> None:
    direct = ModelProfile.model_validate(
        {
            "id": "direct",
            "protocol": "openai_responses",
            "base_url": "https://example.test/v1",
            "model": "model",
            "auth_mode": "none",
        }
    )
    proxied = ModelProfile.model_validate(
        {
            **direct.model_dump(),
            "id": "proxied",
            "proxy_url": "socks5://127.0.0.1:1080",
        }
    )
    assert str(proxied.proxy_url) == "socks5://127.0.0.1:1080"
    assert profile_hash(direct) != profile_hash(proxied)

    with pytest.raises(ValidationError, match="credentials are forbidden in proxy_url"):
        ModelProfile.model_validate(
            {
                "id": "credentialed-proxy",
                "protocol": "openai_chat",
                "base_url": "https://example.test/v1",
                "model": "model",
                "auth_mode": "none",
                "proxy_url": "http://user:password@proxy.test:8080",
            }
        )
    with pytest.raises(ValidationError, match="proxy_url must use"):
        ModelProfile.model_validate(
            {
                "id": "invalid-proxy",
                "protocol": "openai_chat",
                "base_url": "https://example.test/v1",
                "model": "model",
                "auth_mode": "none",
                "proxy_url": "ftp://proxy.test",
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
