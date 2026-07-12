"""Anthropic Messages compatible wire adapter."""

from __future__ import annotations

from typing import Any, cast

from backpack_bench.providers.base import (
    ParsedCompletion,
    ParsedStreamEvent,
    effective_auth_mode,
    effective_endpoint,
)
from backpack_bench.schemas import ModelProfile


class AnthropicMessagesAdapter:
    def endpoint(self, profile: ModelProfile) -> str:
        return effective_endpoint(profile)

    def headers(self, profile: ModelProfile, api_key: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": "2023-06-01",
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
            body["output_config"] = {"effort": params.thinking_effort}
        if params.thinking_mode:
            thinking: dict[str, Any] = {"type": params.thinking_mode}
            if params.thinking_budget is not None:
                thinking["budget_tokens"] = params.thinking_budget
            if params.thinking_display is not None:
                thinking["display"] = params.thinking_display
            body["thinking"] = thinking
        for key, value in params.extra_body.items():
            if (
                key in {"thinking", "output_config"}
                and isinstance(value, dict)
                and isinstance(body.get(key), dict)
            ):
                body[key] = {**body[key], **value}
            else:
                body[key] = value
        body["stream"] = True
        return body

    def parse(self, value: Any) -> ParsedCompletion:
        if not isinstance(value, dict):
            raise ValueError("Anthropic response must be an object")
        content = value.get("content")
        if not isinstance(content, list):
            raise ValueError("Anthropic response content must be an array")
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                reasoning_parts.append(block["thinking"])
        raw_stop_reason = (
            value.get("stop_reason") if isinstance(value.get("stop_reason"), str) else None
        )
        finish_reason = "length" if raw_stop_reason == "max_tokens" else raw_stop_reason
        if not text_parts and finish_reason != "length":
            raise ValueError("Anthropic response has no text content block")
        usage_value = value.get("usage")
        usage = cast(dict[str, Any], usage_value) if isinstance(usage_value, dict) else {}
        return ParsedCompletion(
            content="".join(text_parts),
            reasoning="".join(reasoning_parts) or None,
            finish_reason=finish_reason,
            usage=usage,
            response_id=value.get("id") if isinstance(value.get("id"), str) else None,
        )

    def parse_stream_event(self, value: Any) -> ParsedStreamEvent:
        if not isinstance(value, dict):
            raise ValueError("Anthropic stream event must be an object")
        event_type = value.get("type")
        if event_type == "error":
            raise ValueError(f"Anthropic stream returned an error: {value.get('error')}")
        if event_type == "message_start":
            message = value.get("message")
            if not isinstance(message, dict):
                raise ValueError("Anthropic message_start has no message object")
            usage_value = message.get("usage")
            usage = cast(dict[str, Any], usage_value) if isinstance(usage_value, dict) else {}
            return ParsedStreamEvent(
                usage=usage,
                response_id=message.get("id") if isinstance(message.get("id"), str) else None,
            )
        if event_type == "content_block_start":
            block = value.get("content_block")
            if not isinstance(block, dict):
                return ParsedStreamEvent()
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                return ParsedStreamEvent(content_delta=block["text"])
            if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                return ParsedStreamEvent(reasoning_delta=block["thinking"])
            return ParsedStreamEvent()
        if event_type == "content_block_delta":
            delta = value.get("delta")
            if not isinstance(delta, dict):
                return ParsedStreamEvent()
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return ParsedStreamEvent(content_delta=delta["text"])
            if delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
                return ParsedStreamEvent(reasoning_delta=delta["thinking"])
            return ParsedStreamEvent()
        if event_type == "message_delta":
            delta = value.get("delta")
            raw_stop_reason = (
                delta.get("stop_reason")
                if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str)
                else None
            )
            finish_reason = "length" if raw_stop_reason == "max_tokens" else raw_stop_reason
            usage_value = value.get("usage")
            usage = cast(dict[str, Any], usage_value) if isinstance(usage_value, dict) else {}
            return ParsedStreamEvent(finish_reason=finish_reason, usage=usage)
        return ParsedStreamEvent()
