"""Canonical serialization and content hashes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return canonical_data(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return {str(key): canonical_data(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical_data(item) for item in value]
    if isinstance(value, set | frozenset):
        return [canonical_data(item) for item in sorted(value)]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
