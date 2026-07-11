import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".bbbench",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "runs",
}
EXCLUDED_FILES = {"third_party_api_inventory.md", ".env"}
TEXT_SUFFIXES = {
    ".css",
    ".example",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SECRET_PATTERNS = (
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~-]{20,}", re.IGNORECASE),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
)
FORBIDDEN_CONFIG_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "x-api-key",
    "access_token",
}


def _public_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.name not in EXCLUDED_FILES
        and not (set(path.relative_to(ROOT).parts) & EXCLUDED_PARTS)
        and path.suffix.lower() in TEXT_SUFFIXES
    ]


def _forbidden_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            current = f"{path}.{name}" if path else name
            if name.lower() in FORBIDDEN_CONFIG_KEYS:
                found.append(current)
            found.extend(_forbidden_keys(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_forbidden_keys(item, f"{path}[{index}]"))
    return found


def test_public_files_have_no_high_confidence_secrets() -> None:
    findings: list[str] = []
    for path in _public_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: {pattern.pattern}")
    assert not findings, "possible committed secrets:\n" + "\n".join(findings)


def test_public_yaml_configs_do_not_contain_plaintext_auth_fields() -> None:
    findings: list[str] = []
    for path in (ROOT / "configs").glob("*.yaml"):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        findings.extend(f"{path.name}: {key}" for key in _forbidden_keys(value))
    assert not findings, "plaintext auth fields are forbidden:\n" + "\n".join(findings)
