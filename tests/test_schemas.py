import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from backpack_bench.schemas import (
    GeneratorSpec,
    ItemCatalogSpec,
    ModelsConfig,
    PlacementAnswer,
    ProviderLimits,
    RequestParams,
    RunPlan,
    ScenarioDocumentSpec,
    SuiteSpec,
    VisualPackSpec,
)

ROOT = Path(__file__).resolve().parents[1]


def test_global_execution_defaults() -> None:
    limits = ProviderLimits()
    assert limits.timeout_seconds == 1800
    assert limits.concurrency == 10
    plan = RunPlan(id="defaults", suite="suite.yaml", models="models.yaml")
    assert plan.concurrency == 10
    assert RequestParams().json_mode is True
    with pytest.raises(ValidationError):
        ProviderLimits(concurrency=11)
    with pytest.raises(ValidationError):
        RunPlan(id="too-wide", suite="suite.yaml", models="models.yaml", concurrency=11)


def test_committed_json_schemas_are_current() -> None:
    models: dict[str, type[BaseModel]] = {
        "scenario.schema.json": ScenarioDocumentSpec,
        "item-catalog.schema.json": ItemCatalogSpec,
        "visual-pack.schema.json": VisualPackSpec,
        "suite.schema.json": SuiteSpec,
        "models.schema.json": ModelsConfig,
        "run.schema.json": RunPlan,
        "generator.schema.json": GeneratorSpec,
        "answer.schema.json": PlacementAnswer,
    }
    for name, model in models.items():
        committed = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
        assert committed == model.model_json_schema(), f"run `bbbench schema export`: {name}"
