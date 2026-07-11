"""YAML/JSON loading plus atomic artifact writes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def load_yaml(path: Path, model_type: type[ModelT]) -> ModelT:
    with path.open("r", encoding="utf-8") as file:
        value = yaml.safe_load(file)
    return model_type.model_validate(value)


def load_json(path: Path, model_type: type[ModelT]) -> ModelT:
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    return model_type.model_validate(value)


def dump_yaml_text(value: BaseModel | dict[str, Any]) -> str:
    data = (
        value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    )
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text)


def atomic_write_yaml(path: Path, value: BaseModel | dict[str, Any]) -> None:
    atomic_write_text(path, dump_yaml_text(value))
