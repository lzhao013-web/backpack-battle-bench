"""OpenAI Responses API compatible wire adapter."""

from __future__ import annotations

from typing import Any, cast

from backpack_bench.providers.base import (
    ParsedCompletion,
    ParsedStreamEvent,
    PromptImage,
    effective_auth_mode,
    effective_endpoint,
)
from backpack_bench.schemas import ModelProfile


class OpenAIResponsesAdapter:
    def endpoint(self, profile: ModelProfile) -> str:
        return effective_endpoint(profile)

    def headers(self, profile: ModelProfile, api_key: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if profile.params.stream else "application/json",
            **profile.extra_headers,
        }
        auth_mode = effective_auth_mode(profile)
        if api_key and auth_mode in {"bearer", "both"}:
            headers["Authorization"] = f"Bearer {api_key}"
        if api_key and auth_mode in {"x-api-key", "both"}:
            headers["x-api-key"] = api_key
        return headers

    def body(
        self,
        profile: ModelProfile,
        prompt: str,
        image: PromptImage | None = None,
    ) -> dict[str, Any]:
        params = profile.params
        input_value: str | list[dict[str, Any]] = prompt
        if image is not None:
            input_value = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": image.data_url(),
                            "detail": "high",
                        },
                    ],
                }
            ]
        body: dict[str, Any] = {
            "model": profile.model,
            "input": input_value,
        }
        if params.temperature is not None:
            body["temperature"] = params.temperature
        if params.max_tokens is not None:
            body["max_output_tokens"] = params.max_tokens
        if params.thinking_effort:
            body["reasoning"] = {"effort": params.thinking_effort}
        if params.seed is not None:
            body["seed"] = params.seed
        if params.json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        for key, value in params.extra_body.items():
            if (
                key in {"reasoning", "text"}
                and isinstance(value, dict)
                and isinstance(body.get(key), dict)
            ):
                body[key] = {**body[key], **value}
            else:
                body[key] = value
        body["stream"] = params.stream
        return body

    def parse(self, value: Any) -> ParsedCompletion:
        if not isinstance(value, dict):
            raise ValueError("OpenAI Responses response must be an object")
        event_type = value.get("type")
        if event_type in {"response.completed", "response.incomplete"}:
            nested = value.get("response")
            if not isinstance(nested, dict):
                raise ValueError(f"OpenAI Responses {event_type} event has no response object")
            value = nested
        output = value.get("output")
        if not isinstance(output, list):
            raise ValueError("OpenAI Responses response output must be an array")

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "output_text"
                        and isinstance(block.get("text"), str)
                    ):
                        text_parts.append(block["text"])
            elif item_type == "reasoning":
                for field in ("summary", "content"):
                    blocks = item.get(field)
                    if not isinstance(blocks, list):
                        continue
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        text = block.get("text")
                        if isinstance(text, str):
                            reasoning_parts.append(text)

        status = value.get("status") if isinstance(value.get("status"), str) else None
        if status == "failed":
            raise ValueError(f"OpenAI Responses response failed: {value.get('error')}")
        incomplete_details = value.get("incomplete_details")
        incomplete_reason = (
            incomplete_details.get("reason")
            if isinstance(incomplete_details, dict)
            and isinstance(incomplete_details.get("reason"), str)
            else None
        )
        if status == "incomplete":
            finish_reason = (
                "length" if incomplete_reason == "max_output_tokens" else incomplete_reason
            )
            finish_reason = finish_reason or "incomplete"
        elif status == "completed":
            finish_reason = "stop"
        else:
            finish_reason = status
        if not text_parts and finish_reason != "length":
            raise ValueError("OpenAI Responses response has no output_text content block")
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
            raise ValueError("OpenAI Responses stream event must be an object")
        event_type = value.get("type")
        if event_type in {"error", "response.failed"}:
            error = value.get("error")
            if event_type == "response.failed" and error is None:
                response = value.get("response")
                error = response.get("error") if isinstance(response, dict) else None
            raise ValueError(f"OpenAI Responses stream returned an error: {error}")
        response_id = (
            value.get("response_id") if isinstance(value.get("response_id"), str) else None
        )
        if event_type == "response.output_text.delta":
            delta = value.get("delta")
            return ParsedStreamEvent(
                content_delta=delta if isinstance(delta, str) else "",
                response_id=response_id,
            )
        if event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            delta = value.get("delta")
            return ParsedStreamEvent(
                reasoning_delta=delta if isinstance(delta, str) else "",
                response_id=response_id,
            )
        if event_type in {
            "response.created",
            "response.in_progress",
            "response.completed",
            "response.incomplete",
        }:
            response = value.get("response")
            if not isinstance(response, dict):
                if event_type in {"response.completed", "response.incomplete"}:
                    raise ValueError(
                        f"OpenAI Responses {event_type} event has no response object"
                    )
                return ParsedStreamEvent(response_id=response_id)
            usage_value = response.get("usage")
            usage = cast(dict[str, Any], usage_value) if isinstance(usage_value, dict) else {}
            status = (
                response.get("status") if isinstance(response.get("status"), str) else None
            )
            incomplete_details = response.get("incomplete_details")
            incomplete_reason = (
                incomplete_details.get("reason")
                if isinstance(incomplete_details, dict)
                and isinstance(incomplete_details.get("reason"), str)
                else None
            )
            finish_reason: str | None = None
            if event_type == "response.completed" or status == "completed":
                finish_reason = "stop"
            elif event_type == "response.incomplete" or status == "incomplete":
                finish_reason = (
                    "length" if incomplete_reason == "max_output_tokens" else incomplete_reason
                )
                finish_reason = finish_reason or "incomplete"
            return ParsedStreamEvent(
                finish_reason=finish_reason,
                usage=usage,
                response_id=(
                    response.get("id") if isinstance(response.get("id"), str) else response_id
                ),
            )
        return ParsedStreamEvent(response_id=response_id)
