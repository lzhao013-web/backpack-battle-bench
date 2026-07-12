"""Provider-neutral request/response contract."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from backpack_bench.canonical import content_hash
from backpack_bench.schemas import ModelProfile


@dataclass(frozen=True)
class ParsedCompletion:
    content: str
    reasoning: str | None
    finish_reason: str | None
    usage: dict[str, Any]
    response_id: str | None


@dataclass(frozen=True)
class ParsedStreamEvent:
    content_delta: str = ""
    reasoning_delta: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    response_id: str | None = None


class ProviderAdapter(Protocol):
    def endpoint(self, profile: ModelProfile) -> str: ...

    def headers(self, profile: ModelProfile, api_key: str | None) -> dict[str, str]: ...

    def body(self, profile: ModelProfile, prompt: str) -> dict[str, Any]: ...

    def parse(self, value: Any) -> ParsedCompletion: ...

    def parse_stream_event(self, value: Any) -> ParsedStreamEvent: ...


def effective_endpoint(profile: ModelProfile) -> str:
    if profile.endpoint:
        if profile.endpoint.startswith(("http://", "https://")):
            return profile.endpoint.rstrip("/")
        return f"{str(profile.base_url).rstrip('/')}/{profile.endpoint.lstrip('/')}"
    base = str(profile.base_url).rstrip("/")
    suffix = "/chat/completions" if profile.protocol == "openai_chat" else "/messages"
    return base if base.endswith(suffix) else f"{base}{suffix}"


def effective_auth_mode(profile: ModelProfile) -> str:
    return profile.auth_mode or ("bearer" if profile.protocol == "openai_chat" else "x-api-key")


def resolve_api_key(profile: ModelProfile) -> str | None:
    auth_mode = effective_auth_mode(profile)
    if auth_mode == "none":
        return None
    if profile.api_key_env is None:
        raise ValueError(f"profile {profile.id} does not define api_key_env")
    value = os.getenv(profile.api_key_env)
    if not value:
        message = (
            f"environment variable {profile.api_key_env} required by "
            f"profile {profile.id} is missing"
        )
        raise ValueError(message)
    return value


def profile_hash(profile: ModelProfile) -> str:
    return content_hash(
        {
            "protocol": profile.protocol,
            "endpoint": effective_endpoint(profile),
            "model": profile.model,
            "auth_mode": effective_auth_mode(profile),
            "params": profile.params,
            "verify_tls": profile.verify_tls,
            "extra_headers": profile.extra_headers,
        }
    )


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    fragments = ("authorization", "api-key", "apikey", "token", "secret")
    return {
        name: "***REDACTED***" if any(fragment in name.lower() for fragment in fragments) else value
        for name, value in headers.items()
    }


def redact_secret_values(value: Any, *secrets: str | None) -> Any:
    """Remove exact credential values if a gateway unexpectedly echoes them."""
    active = tuple(secret for secret in secrets if secret)
    if not active:
        return value
    if isinstance(value, str):
        for secret in active:
            value = value.replace(secret, "***REDACTED***")
        return value
    if isinstance(value, dict):
        return {
            redact_secret_values(key, *active)
            if isinstance(key, str)
            else key: redact_secret_values(item, *active)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secret_values(item, *active) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secret_values(item, *active) for item in value)
    return value


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "".join(parts)
    raise ValueError("response does not contain final text content")
