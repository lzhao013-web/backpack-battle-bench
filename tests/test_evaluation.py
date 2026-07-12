import json
from pathlib import Path

from backpack_bench.canonical import text_hash
from backpack_bench.evaluation import parse_and_score_output, validate_placement_answer
from backpack_bench.io import load_json
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import render_prompt
from backpack_bench.schemas import OracleArtifact, PlacementAnswer, ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]


def test_smoke_scenario_oracle_is_18(
    smoke_scenario: ScenarioSpec, registry: PluginRegistry
) -> None:
    oracle = load_json(ROOT / "oracles" / "smoke" / "packing_3x3.json", OracleArtifact)
    assert oracle.exact
    assert oracle.optimal_attack == 18
    assert oracle.witness is not None
    result = validate_placement_answer(smoke_scenario, oracle.witness, registry)
    assert result["valid"]
    assert result["actual_attack"] == 18


def test_prompt_is_generated_from_rules(
    smoke_scenario: ScenarioSpec, registry: PluginRegistry
) -> None:
    prompt = render_prompt(smoke_scenario, registry)
    assert "目标：让背包中所有武器的攻击总和尽可能高" in prompt
    assert "初始局部方向" not in prompt
    assert "同一来源对同一目标最多结算一次" not in prompt
    assert "类别：weapon" not in prompt
    assert "great_blade_1" in prompt
    assert "大剑" in prompt and "短剑" in prompt and "护符" in prompt
    assert text_hash(prompt) == "643730d65261c0f09d3dafc57ac1bca7b38e4790cf1f502ad1944f6d7e2601e3"


def test_strict_json_and_error_categories(
    smoke_scenario: ScenarioSpec, registry: PluginRegistry
) -> None:
    markdown = parse_and_score_output(smoke_scenario, "```json\n{}\n```", registry)
    assert markdown["error_type"] == "output_not_json"
    truncated = parse_and_score_output(smoke_scenario, "", registry, finish_reason="length")
    assert truncated["error_type"] == "output_truncated"
    valid_json_but_truncated = parse_and_score_output(
        smoke_scenario, '{"placements": []}', registry, finish_reason="length"
    )
    assert valid_json_but_truncated["error_type"] == "output_truncated"
    schema_error = parse_and_score_output(
        smoke_scenario,
        json.dumps(
            {"placements": [{"item_id": "short_blade_1", "row": True, "col": 0, "rotation": 0}]}
        ),
        registry,
    )
    assert schema_error["error_type"] == "answer_schema"


def test_overlap_and_out_of_bounds(smoke_scenario: ScenarioSpec, registry: PluginRegistry) -> None:
    answer = PlacementAnswer.model_validate(
        {
            "placements": [
                {"item_id": "great_blade_1", "row": 0, "col": 0, "rotation": 0},
                {"item_id": "charm_1", "row": 0, "col": 0, "rotation": 0},
                {"item_id": "short_blade_1", "row": 3, "col": 0, "rotation": 0},
            ]
        }
    )
    result = validate_placement_answer(smoke_scenario, answer, registry)
    assert not result["valid"]
    assert {error["code"] for error in result["errors"]} == {"overlap", "out_of_bounds"}


def test_ray_bonus_is_applied_once_per_source_target(registry: PluginRegistry) -> None:
    scenario = ScenarioSpec.model_validate(
        {
            "id": "ray-once",
            "title": "射线结算",
            "board": {"width": 2, "height": 3},
            "items": [
                {
                    "id": "blade",
                    "display_name": "剑",
                    "count": 1,
                    "shape": [[0, 0]],
                    "category": "weapon",
                    "stats": {"attack": 3},
                },
                {
                    "id": "beacon",
                    "display_name": "信标",
                    "count": 1,
                    "shape": [[0, 0], [0, 1]],
                    "effects": [
                        {
                            "type": "ray_stat_bonus",
                            "config": {
                                "direction": [-1, 0],
                                "target_category": "weapon",
                                "stat": "attack",
                                "amount": 4,
                                "once_per_target": True,
                                "blocked": False,
                            },
                        }
                    ],
                },
            ],
            "objective": {"type": "sum_stat", "config": {"category": "weapon", "stat": "attack"}},
        }
    )
    answer = PlacementAnswer.model_validate(
        {
            "placements": [
                {"item_id": "blade_1", "row": 0, "col": 0, "rotation": 0},
                {"item_id": "beacon_1", "row": 2, "col": 0, "rotation": 0},
            ]
        }
    )
    result = validate_placement_answer(scenario, answer, registry)
    assert result["actual_attack"] == 7
    assert len(result["effects"]) == 1
    assert {event["target"] for event in result["effects"]} == {"blade_1"}
    assert all(event["amount"] == 4 for event in result["effects"])


def test_irregular_board_hole_is_out_of_bounds(registry: PluginRegistry) -> None:
    scenario = ScenarioSpec.model_validate(
        {
            "id": "irregular",
            "title": "不规则背包",
            "board": {"width": 2, "height": 2, "cells": [[0, 0], [1, 0], [1, 1]]},
            "items": [{"id": "gem", "display_name": "宝石", "shape": [[0, 0]]}],
            "objective": {"type": "sum_stat", "config": {"category": "weapon", "stat": "attack"}},
        }
    )
    answer = PlacementAnswer.model_validate(
        {"placements": [{"item_id": "gem_1", "row": 0, "col": 1, "rotation": 0}]}
    )
    result = validate_placement_answer(scenario, answer, registry)
    assert not result["valid"]
    assert result["errors"][0]["code"] == "out_of_bounds"
    assert result["errors"][0]["cells"] == [(0, 1)]
