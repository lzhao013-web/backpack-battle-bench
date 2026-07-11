import json
from pathlib import Path

from pydantic import BaseModel

from backpack_bench.schemas import (
    GeneratorSpec,
    ModelsConfig,
    PlacementAnswer,
    RunPlan,
    ScenarioSpec,
    SuiteSpec,
)

ROOT = Path(__file__).resolve().parents[1]


def test_committed_json_schemas_are_current() -> None:
    models: dict[str, type[BaseModel]] = {
        "scenario.schema.json": ScenarioSpec,
        "suite.schema.json": SuiteSpec,
        "models.schema.json": ModelsConfig,
        "run.schema.json": RunPlan,
        "generator.schema.json": GeneratorSpec,
        "answer.schema.json": PlacementAnswer,
    }
    for name, model in models.items():
        committed = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
        assert committed == model.model_json_schema(), f"run `bbbench schema export`: {name}"
