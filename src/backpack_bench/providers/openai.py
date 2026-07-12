"""OpenAI Chat Completions compatible wire adapter."""

from __future__ import annotations

from typing import Any, cast

from backpack_bench.providers.base import (
    ParsedCompletion,
    ParsedStreamEvent,
    effective_auth_mode,
    effective_endpoint,
    text_content,
)
from backpack_bench.schemas import ModelProfile


class OpenAIChatAdapter:
    def endpoint(self, profile: ModelProfile) -> str:
        return effective_endpoint(profile)

    def headers(self, profile: ModelProfile, api_key: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **profile.extra_headers,
        }
        auth_mode = effective_auth_mode(profile)
        if api_key and auth_mode in {"bearer", "both"}:
            headers["Authorization"] = f"Bearer {api_key}"
        if api_key and auth_mode in {"x-api-key", "both"}:
            headers["x-api-key"] = api_key
        return headers

    def body(self, profile: ModelProfile, prompt: str) -> dict[str, Any]:
        params = profile.params
        body: dict[str, Any] = {
            "model": profile.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if params.temperature is not None:
            body["temperature"] = params.temperature
        if params.max_tokens is not None:
            body["max_tokens"] = params.max_tokens
        if params.thinking_effort:
            body["reasoning_effort"] = params.thinking_effort
        if params.seed is not None:
            body["seed"] = params.seed
        if params.json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(params.extra_body)
        body["stream"] = True
        stream_options = body.get("stream_options")
        body["stream_options"] = {
            "include_usage": True,
            **(stream_options if isinstance(stream_options, dict) else {}),
        }
        return body

    def parse(self, value: Any) -> ParsedCompletion:
        if not isinstance(value, dict):
            raise ValueError("OpenAI response must be an object")
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("OpenAI response has no choices[0]")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("OpenAI response has no choices[0].message")
        reasoning_value = message.get("reasoning_content", message.get("reasoning"))
        reasoning = reasoning_value if isinstance(reasoning_value, str) else None
        usage_value = value.get("usage")
        usage = cast(dict[str, Any], usage_value) if isinstance(usage_value, dict) else {}
        finish_reason = (
            choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
        )
        content_value = message.get("content")
        content = (
            ""
            if content_value is None and finish_reason == "length"
            else text_content(content_value)
        )
        return ParsedCompletion(
            content=content,
            reasoning=reasoning,
            finish_reason=finish_reason,
            usage=usage,
            response_id=value.get("id") if isinstance(value.get("id"), str) else None,
        )

    def parse_stream_event(self, value: Any) -> ParsedStreamEvent:
        if not isinstance(value, dict):
            raise ValueError("OpenAI stream event must be an object")
        error = value.get("error")
        if error is not None:
            raise ValueError(f"OpenAI stream returned an error: {error}")
        usage_value = value.get("usage")
        usage = cast(dict[str, Any], usage_value) if isinstance(usage_value, dict) else {}
        choices = value.get("choices", [])
        if not isinstance(choices, list):
            raise ValueError("OpenAI stream event choices must be an array")
        if not choices:
            return ParsedStreamEvent(
                usage=usage,
                response_id=value.get("id") if isinstance(value.get("id"), str) else None,
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("OpenAI stream event choices[0] must be an object")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        content_value = delta.get("content")
        if isinstance(content_value, str):
            content = content_value
        elif isinstance(content_value, list):
            try:
                content = text_content(content_value)
            except ValueError:
                content = ""
        else:
            content = ""
        reasoning_value = delta.get("reasoning_content", delta.get("reasoning"))
        reasoning = reasoning_value if isinstance(reasoning_value, str) else ""
        finish_reason = (
            choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
        )
        return ParsedStreamEvent(
            content_delta=content,
            reasoning_delta=reasoning,
            finish_reason=finish_reason,
            usage=usage,
            response_id=value.get("id") if isinstance(value.get("id"), str) else None,
        )
