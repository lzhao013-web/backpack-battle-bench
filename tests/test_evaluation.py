import json
from pathlib import Path

from backpack_bench.canonical import text_hash
from backpack_bench.evaluation import parse_and_score_output, validate_placement_answer
from backpack_bench.io import load_json, load_yaml
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import render_prompt
from backpack_bench.schemas import OracleArtifact, PlacementAnswer, ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]


def test_mixed_scenario_oracle_is_21(
    mixed_scenario: ScenarioSpec, registry: PluginRegistry
) -> None:
    oracle = load_json(ROOT / "oracles" / "curated" / "mixed_3x3.json", OracleArtifact)
    assert oracle.exact
    assert oracle.optimal_attack == 21
    assert oracle.witness is not None
    result = validate_placement_answer(mixed_scenario, oracle.witness, registry)
    assert result["valid"]
    assert result["actual_attack"] == 21


def test_prompt_is_generated_from_rules(
    mixed_scenario: ScenarioSpec, registry: PluginRegistry
) -> None:
    prompt = render_prompt(mixed_scenario, registry)
    assert "从它每个格子向上直到背包边缘" in prompt
    assert "紧挨在它每个格子左侧和右侧" in prompt
    assert "目标：让背包中所有武器的攻击总和尽可能高" in prompt
    assert "初始局部方向" not in prompt
    assert "同一来源对同一目标最多结算一次" not in prompt
    assert "类别：weapon" not in prompt
    assert "magic_circle_1" in prompt
    assert text_hash(prompt) == "ede44b5cf9b97a7ed922f071757b6c505ff052360e9dcd84064d753d1d02fbe0"


def test_strict_json_and_error_categories(
    mixed_scenario: ScenarioSpec, registry: PluginRegistry
) -> None:
    markdown = parse_and_score_output(mixed_scenario, "```json\n{}\n```", registry)
    assert markdown["error_type"] == "output_not_json"
    truncated = parse_and_score_output(mixed_scenario, "", registry, finish_reason="length")
    assert truncated["error_type"] == "output_truncated"
    valid_json_but_truncated = parse_and_score_output(
        mixed_scenario, '{"placements": []}', registry, finish_reason="length"
    )
    assert valid_json_but_truncated["error_type"] == "output_truncated"
    schema_error = parse_and_score_output(
        mixed_scenario,
        json.dumps({"placements": [{"item_id": "dagger_1", "row": True, "col": 0, "rotation": 0}]}),
        registry,
    )
    assert schema_error["error_type"] == "answer_schema"


def test_overlap_and_out_of_bounds(mixed_scenario: ScenarioSpec, registry: PluginRegistry) -> None:
    answer = PlacementAnswer.model_validate(
        {
            "placements": [
                {"item_id": "iron_sword_1", "row": 0, "col": 0, "rotation": 0},
                {"item_id": "gem_1", "row": 0, "col": 0, "rotation": 0},
                {"item_id": "dagger_1", "row": 3, "col": 0, "rotation": 0},
            ]
        }
    )
    result = validate_placement_answer(mixed_scenario, answer, registry)
    assert not result["valid"]
    assert {error["code"] for error in result["errors"]} == {"overlap", "out_of_bounds"}


def test_ray_bonus_is_applied_once_per_source_target(registry: PluginRegistry) -> None:
    scenario = load_yaml(ROOT / "scenarios" / "curated" / "ray_3x3.yaml", ScenarioSpec)
    oracle = load_json(ROOT / "oracles" / "curated" / "ray_3x3.json", OracleArtifact)
    assert oracle.witness is not None
    result = validate_placement_answer(scenario, oracle.witness, registry)
    assert result["actual_attack"] == 21
    assert len(result["effects"]) == 3
    assert {event["target"] for event in result["effects"]} == {
        "dagger_1",
        "dagger_2",
        "dagger_3",
    }
    assert all(event["amount"] == 4 for event in result["effects"])


def test_irregular_board_hole_is_out_of_bounds(registry: PluginRegistry) -> None:
    scenario = load_yaml(ROOT / "scenarios" / "curated" / "irregular_4x4.yaml", ScenarioSpec)
    answer = PlacementAnswer.model_validate(
        {"placements": [{"item_id": "gem_1", "row": 0, "col": 3, "rotation": 0}]}
    )
    result = validate_placement_answer(scenario, answer, registry)
    assert not result["valid"]
    assert result["errors"][0]["code"] == "out_of_bounds"
    assert result["errors"][0]["cells"] == [(0, 3)]
