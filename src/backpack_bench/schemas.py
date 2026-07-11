"""Versioned public configuration schemas."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PositiveFloat,
    PositiveInt,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from backpack_bench.geometry import Cell

Rotation = Literal[0, 90, 180, 270]
Difficulty = Literal["easy", "medium", "hard"]
ProviderProtocol = Literal["openai_chat", "anthropic_messages"]
CREDENTIAL_FIELD_NAMES = {
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "x-api-key",
    "access_token",
}


def default_rotations() -> list[Rotation]:
    return [0, 90, 180, 270]


def credential_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            current = f"{prefix}.{name}" if prefix else name
            if name.lower() in CREDENTIAL_FIELD_NAMES:
                paths.append(current)
            paths.extend(credential_paths(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(credential_paths(item, f"{prefix}[{index}]"))
    return paths


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoardSpec(StrictModel):
    width: PositiveInt
    height: PositiveInt
    cells: list[Cell] | None = None

    @field_validator("cells")
    @classmethod
    def validate_cells(cls, value: list[Cell] | None, info: Any) -> list[Cell] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("explicit board cells cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("board cells must be unique")
        width = info.data.get("width")
        height = info.data.get("height")
        if width is not None and height is not None:
            outside = [
                (row, col) for row, col in value if not (0 <= row < height and 0 <= col < width)
            ]
            if outside:
                raise ValueError(f"board cells outside width/height: {outside}")
        return sorted(value)

    def valid_cells(self) -> frozenset[Cell]:
        if self.cells is not None:
            return frozenset(self.cells)
        return frozenset((row, col) for row in range(self.height) for col in range(self.width))


class EffectSpec(StrictModel):
    type: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    config: dict[str, Any] = Field(default_factory=dict)


class ItemTypeSpec(StrictModel):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    count: PositiveInt = 1
    shape: list[Cell]
    rotations: list[Rotation] = Field(default_factory=default_rotations)
    category: str = Field(default="support", min_length=1)
    stats: dict[str, int] = Field(default_factory=dict)
    effects: list[EffectSpec] = Field(default_factory=list)

    @field_validator("shape")
    @classmethod
    def validate_shape(cls, value: list[Cell]) -> list[Cell]:
        if not value:
            raise ValueError("item shape cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("item shape cells must be unique")
        if min(row for row, _ in value) != 0 or min(col for _, col in value) != 0:
            raise ValueError("item shape must be normalized to a top-left origin")
        if any(row < 0 or col < 0 for row, col in value):
            raise ValueError("item shape cells cannot be negative")
        return sorted(value)

    @field_validator("rotations")
    @classmethod
    def validate_rotations(cls, value: list[Rotation]) -> list[Rotation]:
        if not value:
            raise ValueError("at least one rotation is required")
        if len(set(value)) != len(value):
            raise ValueError("rotations must be unique")
        return value


class ObjectiveSpec(StrictModel):
    type: str = Field(default="sum_stat", min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    config: dict[str, Any]


class ScenarioProvenance(StrictModel):
    generator_id: str
    generator_version: str
    seed: int
    candidate_index: int


class ScenarioSpec(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str = "1.0.0"
    title: str
    locale: Literal["zh-CN"] = "zh-CN"
    board: BoardSpec
    items: list[ItemTypeSpec]
    objective: ObjectiveSpec
    tags: list[str] = Field(default_factory=list)
    difficulty: Difficulty = "medium"
    provenance: ScenarioProvenance | None = None

    @model_validator(mode="after")
    def validate_scenario(self) -> ScenarioSpec:
        if not self.items:
            raise ValueError("scenario must contain at least one item type")
        ids = [item.id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("item type ids must be unique")
        if sum(item.count for item in self.items) > 32:
            raise ValueError("scenario supports at most 32 item instances")
        return self

    def expanded_item_ids(self) -> dict[str, ItemTypeSpec]:
        return {
            f"{item.id}_{index}": item for item in self.items for index in range(1, item.count + 1)
        }


class PlacementInput(StrictModel):
    item_id: StrictStr
    row: StrictInt
    col: StrictInt
    rotation: Rotation

    @field_validator("rotation", mode="before")
    @classmethod
    def rotation_is_plain_integer(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rotation must be an integer")
        return value


class PlacementAnswer(StrictModel):
    placements: list[PlacementInput]


class OracleArtifact(StrictModel):
    schema_version: Literal[1] = 1
    scenario_id: str
    scenario_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    solver_id: str
    solver_version: str
    exact: bool
    optimal_attack: int | None
    witness: PlacementAnswer | None
    nodes_evaluated: int
    elapsed_seconds: float
    timed_out: bool = False


class SuiteScenarioEntry(StrictModel):
    scenario: str
    oracle: str
    scenario_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    weight: PositiveFloat = 1.0


class SuiteSpec(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str
    title: str
    locale: Literal["zh-CN"] = "zh-CN"
    allowed_plugins: list[str]
    scenarios: list[SuiteScenarioEntry]

    @model_validator(mode="after")
    def validate_entries(self) -> SuiteSpec:
        if not self.scenarios:
            raise ValueError("suite cannot be empty")
        paths = [entry.scenario for entry in self.scenarios]
        if len(set(paths)) != len(paths):
            raise ValueError("suite scenario paths must be unique")
        if not self.allowed_plugins or len(set(self.allowed_plugins)) != len(self.allowed_plugins):
            raise ValueError("allowed_plugins must be non-empty and unique")
        return self


class GeneratorSpec(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    seed: int
    count: PositiveInt
    board_sizes: list[tuple[PositiveInt, PositiveInt]] = Field(
        default_factory=lambda: [(3, 3), (4, 4)]
    )
    max_item_instances: PositiveInt = Field(default=8, le=8)
    oracle_timeout_seconds: PositiveFloat = 60.0
    candidate_indices: list[PositiveInt] | None = None
    output_dir: str
    oracle_dir: str

    @model_validator(mode="after")
    def validate_candidate_indices(self) -> GeneratorSpec:
        if self.candidate_indices is not None:
            if len(self.candidate_indices) != self.count:
                raise ValueError("candidate_indices length must equal count")
            if self.candidate_indices != sorted(set(self.candidate_indices)):
                raise ValueError("candidate_indices must be unique and sorted")
        return self


class RequestParams(StrictModel):
    temperature: float | None = None
    max_tokens: PositiveInt | None = None
    thinking_effort: str | None = None
    thinking_mode: Literal["adaptive", "enabled", "disabled"] | None = None
    thinking_budget: PositiveInt | None = None
    thinking_display: Literal["summarized", "omitted"] | None = None
    json_mode: bool = False
    seed: int | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_extra_body(self) -> RequestParams:
        reserved = {"model", "messages"} & self.extra_body.keys()
        if reserved:
            raise ValueError(f"extra_body cannot override benchmark fields: {sorted(reserved)}")
        forbidden = credential_paths(self.extra_body)
        if forbidden:
            raise ValueError(
                f"credentials are forbidden in extra_body; use api_key_env: {forbidden}"
            )
        return self


class ProviderLimits(StrictModel):
    concurrency: PositiveInt = 1
    qps: PositiveFloat | None = None
    timeout_seconds: PositiveFloat = 120.0
    retries: int = Field(default=3, ge=0, le=10)


class PricingSpec(StrictModel):
    input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)
    reasoning_per_million: float | None = Field(default=None, ge=0)


class ModelProfile(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str | None = None
    protocol: ProviderProtocol
    base_url: HttpUrl
    endpoint: str | None = None
    model: str
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    auth_mode: Literal["bearer", "x-api-key", "both", "none"] | None = None
    params: RequestParams = Field(default_factory=RequestParams)
    limits: ProviderLimits = Field(default_factory=ProviderLimits)
    pricing: PricingSpec | None = None
    verify_tls: bool = True
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_profile(self) -> ModelProfile:
        url_values = [str(self.base_url)]
        if self.endpoint is not None:
            url_values.append(self.endpoint)
        for url in url_values:
            parsed = urlsplit(url)
            if parsed.username or parsed.password:
                raise ValueError("credentials are forbidden in endpoint URLs; use api_key_env")
            query_credentials = {
                name.lower() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
            } & CREDENTIAL_FIELD_NAMES
            if query_credentials:
                raise ValueError("credentials are forbidden in URL query; use api_key_env")
        for name in self.extra_headers:
            lowered = name.lower()
            if any(
                fragment in lowered for fragment in ("authorization", "api-key", "token", "secret")
            ):
                raise ValueError("secret headers are forbidden; use api_key_env")
        params = self.params
        if self.auth_mode != "none" and self.api_key_env is None:
            raise ValueError("api_key_env is required unless auth_mode is none")
        if self.protocol == "openai_chat" and any(
            value is not None
            for value in (params.thinking_mode, params.thinking_budget, params.thinking_display)
        ):
            raise ValueError("Anthropic thinking fields cannot be used with openai_chat")
        if self.protocol == "anthropic_messages":
            if params.thinking_budget is not None and params.thinking_mode is None:
                params.thinking_mode = "enabled"
            if params.thinking_mode == "enabled":
                if params.thinking_budget is None or params.thinking_budget < 1024:
                    raise ValueError("manual Anthropic thinking requires budget >= 1024")
                if params.max_tokens is None or params.thinking_budget >= params.max_tokens:
                    raise ValueError("manual thinking budget must be less than max_tokens")
        return self


class ModelsConfig(StrictModel):
    schema_version: Literal[1] = 1
    profiles: list[ModelProfile]

    @model_validator(mode="after")
    def validate_profiles(self) -> ModelsConfig:
        ids = [profile.id for profile in self.profiles]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("model profile ids must be non-empty and unique")
        return self


class RunPlan(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$")
    suite: str
    models: str
    model_ids: list[str] | None = None
    trials: PositiveInt = 1
    concurrency: PositiveInt = 1
    database: str = ".bbbench/results.sqlite3"
    artifacts: str = ".bbbench/artifacts"
    reports: str = ".bbbench/reports"


class GeneratorOutput(StrictModel):
    scenario_paths: list[str]
    oracle_paths: list[str]


def is_secret_like(value: str) -> bool:
    return bool(re.search(r"(?i)(sk-|api[_-]?key|bearer\s+)", value))
