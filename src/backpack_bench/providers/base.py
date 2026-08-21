"""Provider-neutral request/response contract."""

from __future__ import annotations

import base64
import io
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from PIL import Image

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


MAX_PROMPT_IMAGE_PIXELS = 640_000


@dataclass(frozen=True)
class PromptImage:
    path: str
    media_type: str = "image/png"
    content: bytes | None = field(default=None, repr=False)
    split_row: int | None = None
    split_column: int | None = None
    split_rows: int | None = None
    split_columns: int | None = None
    is_overview: bool = False

    def bytes_data(self) -> bytes:
        if self.content is not None:
            return self.content
        with open(self.path, "rb") as file:
            return file.read()

    def base64_data(self) -> str:
        return base64.b64encode(self.bytes_data()).decode("ascii")

    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.base64_data()}"


PromptImageInput = PromptImage | Sequence[PromptImage] | None


def normalize_prompt_images(value: PromptImageInput) -> tuple[PromptImage, ...]:
    if value is None:
        return ()
    if isinstance(value, PromptImage):
        return (value,)
    images = tuple(value)
    if not all(isinstance(image, PromptImage) for image in images):
        raise TypeError("prompt images must be PromptImage instances")
    return images


def _balanced_split_grid(
    width: int,
    height: int,
    max_pixels: int,
) -> tuple[int, int]:
    """Find a small, balanced grid whose largest tile fits the pixel limit."""
    minimum_parts = math.ceil(width * height / max_pixels)
    best: tuple[int, float, int, int] | None = None
    row_limit = min(height, max(1, minimum_parts * 2))
    for rows in range(1, row_limit + 1):
        tile_height = math.ceil(height / rows)
        allowed_width = max_pixels // tile_height
        if allowed_width < 1:
            continue
        columns = math.ceil(width / allowed_width)
        if columns > width:
            continue
        tile_width = math.ceil(width / columns)
        if tile_width * tile_height > max_pixels:
            continue
        tile_count = rows * columns
        aspect_penalty = abs(math.log(tile_width / tile_height))
        candidate = (tile_count, aspect_penalty, rows, columns)
        if best is None or candidate < best:
            best = candidate
    if best is not None:
        return best[2], best[3]

    # This fallback also handles unusually narrow images with an enormous height.
    edge = max(1, math.isqrt(max_pixels))
    return math.ceil(height / edge), math.ceil(width / edge)


def split_prompt_image(
    image: PromptImage,
    max_pixels: int = MAX_PROMPT_IMAGE_PIXELS,
) -> tuple[PromptImage, ...]:
    """Split an image into equal grid tiles no larger than ``max_pixels`` pixels."""
    if max_pixels < 1:
        raise ValueError("max_pixels must be positive")
    with Image.open(io.BytesIO(image.bytes_data())) as source:
        source.load()
        width, height = source.size
        if width * height <= max_pixels:
            return (image,)
        rows, columns = _balanced_split_grid(width, height, max_pixels)
        parts: list[PromptImage] = []
        for row in range(rows):
            top = row * height // rows
            bottom = (row + 1) * height // rows
            for column in range(columns):
                left = column * width // columns
                right = (column + 1) * width // columns
                tile = source.crop((left, top, right, bottom))
                buffer = io.BytesIO()
                try:
                    tile.save(buffer, format="PNG", optimize=False, compress_level=9)
                except OSError:
                    tile.convert("RGBA").save(
                        buffer,
                        format="PNG",
                        optimize=False,
                        compress_level=9,
                    )
                parts.append(
                    PromptImage(
                        path=image.path,
                        media_type="image/png",
                        content=buffer.getvalue(),
                        split_row=row,
                        split_column=column,
                        split_rows=rows,
                        split_columns=columns,
                    )
                )
    return tuple(parts)


def prompt_images_with_overview(
    image: PromptImage,
    max_pixels: int = MAX_PROMPT_IMAGE_PIXELS,
) -> tuple[PromptImage, ...]:
    """Return a complete low-resolution overview followed by full-resolution tiles."""
    parts = split_prompt_image(image, max_pixels)
    if len(parts) == 1:
        return parts
    with Image.open(io.BytesIO(image.bytes_data())) as source:
        source.load()
        scale = math.sqrt(max_pixels / (source.width * source.height))
        overview_width = max(1, math.floor(source.width * scale))
        overview_height = max(1, math.floor(source.height * scale))
        if overview_width * overview_height > max_pixels:
            overview_height = max(1, max_pixels // overview_width)
        if overview_width * overview_height > max_pixels:
            overview_width = max(1, max_pixels // overview_height)
        overview_image = source.resize(
            (overview_width, overview_height),
            Image.Resampling.LANCZOS,
        )
        buffer = io.BytesIO()
        try:
            overview_image.save(buffer, format="PNG", optimize=False, compress_level=9)
        except OSError:
            overview_image.convert("RGBA").save(
                buffer,
                format="PNG",
                optimize=False,
                compress_level=9,
            )
    overview = PromptImage(
        path=image.path,
        media_type="image/png",
        content=buffer.getvalue(),
        split_rows=parts[0].split_rows,
        split_columns=parts[0].split_columns,
        is_overview=True,
    )
    return (overview, *parts)


def prompt_for_images(prompt: str, value: PromptImageInput) -> str:
    """Tell the model how to reconstruct an image that was sent as multiple tiles."""
    images = normalize_prompt_images(value)
    if len(images) <= 1:
        return prompt
    overview = next((image for image in images if image.is_overview), None)
    parts = tuple(image for image in images if not image.is_overview)
    first_part = parts[0] if parts else images[0]
    if first_part.split_rows is not None and first_part.split_columns is not None:
        layout = (
            f"这些高清分片组成 {first_part.split_rows} 行 × "
            f"{first_part.split_columns} 列的网格，"
        )
    else:
        layout = ""
    if overview is not None:
        instruction = (
            "【多图题面说明】第 1 张图片是低分辨率的完整题面总览图，仅用于确认整体布局和分片位置；"
            f"随后 {len(parts)} 张图片是原始题面的高清分片。{layout}"
            "分片按从左到右、从上到下的顺序依次提供。"
            "请先用总览图建立整体位置关系，再组合查看全部高清分片；"
            "文字、格子和物品形状等细节以高清分片为准。"
            "输出前必须核对所有分片，尤其不要漏掉跨越分片边界的物品占用格或灰色叉格。"
        )
    else:
        instruction = (
            f"【多图题面说明】原始题面已被均等切分为 {len(images)} 张图片。"
            f"{layout}图片按从左到右、从上到下的顺序依次提供。"
            "请将所有图片组合成一张完整题面后再查看和作答，不要遗漏任何分片。"
        )
    return f"{prompt}\n\n{instruction}"


class ProviderAdapter(Protocol):
    def endpoint(self, profile: ModelProfile) -> str: ...

    def headers(self, profile: ModelProfile, api_key: str | None) -> dict[str, str]: ...

    def body(
        self,
        profile: ModelProfile,
        prompt: str,
        image: PromptImageInput = None,
    ) -> dict[str, Any]: ...

    def parse(self, value: Any) -> ParsedCompletion: ...

    def parse_stream_event(self, value: Any) -> ParsedStreamEvent: ...


def effective_endpoint(profile: ModelProfile) -> str:
    if profile.endpoint:
        if profile.endpoint.startswith(("http://", "https://")):
            return profile.endpoint.rstrip("/")
        return f"{str(profile.base_url).rstrip('/')}/{profile.endpoint.lstrip('/')}"
    base = str(profile.base_url).rstrip("/")
    suffix = {
        "openai_chat": "/chat/completions",
        "openai_responses": "/responses",
        "anthropic_messages": "/messages",
    }[profile.protocol]
    return base if base.endswith(suffix) else f"{base}{suffix}"


def effective_auth_mode(profile: ModelProfile) -> str:
    return profile.auth_mode or (
        "x-api-key" if profile.protocol == "anthropic_messages" else "bearer"
    )


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
    params = profile.params.model_dump(mode="json", exclude_none=True)
    if not profile.params.split_image:
        # Preserve profile identities created before optional image splitting existed.
        params.pop("split_image", None)
    return content_hash(
        {
            "protocol": profile.protocol,
            "endpoint": effective_endpoint(profile),
            "model": profile.model,
            "auth_mode": effective_auth_mode(profile),
            "params": params,
            "verify_tls": profile.verify_tls,
            "proxy_url": str(profile.proxy_url) if profile.proxy_url is not None else None,
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
