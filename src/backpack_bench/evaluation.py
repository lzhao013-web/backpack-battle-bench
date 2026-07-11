"""Strict answer validation and deterministic scoring."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from backpack_bench.canonical import content_hash
from backpack_bench.domain import EffectEvent, EvaluationContext, PlacedItem
from backpack_bench.geometry import occupied_cells
from backpack_bench.plugins import PluginRegistry
from backpack_bench.schemas import PlacementAnswer, ScenarioSpec


def scenario_hash(scenario: ScenarioSpec, registry: PluginRegistry) -> str:
    effects = [effect for item in scenario.items for effect in item.effects]
    registry.validate_scenario(effects, scenario.objective)
    return content_hash(
        {
            "scenario": scenario,
            "plugins": registry.versions_for(effects, scenario.objective),
        }
    )


def score_evaluation_context(
    context: EvaluationContext,
    registry: PluginRegistry,
) -> tuple[dict[str, dict[str, int]], list[EffectEvent], int]:
    """Score an already validated, non-overlapping placement context."""
    stats = {placement.item_id: dict(placement.item.stats) for placement in context.placements}
    events: list[EffectEvent] = []
    for source in sorted(context.placements, key=lambda placement: placement.item_id):
        for effect in source.item.effects:
            handler = registry.effect(effect.type)
            for event in handler.apply(context, source, effect):
                stats[event.target][event.stat] = (
                    stats[event.target].get(event.stat, 0) + event.amount
                )
                events.append(event)
    objective = registry.objective(context.scenario.objective.type)
    attack = objective.score(context, stats, context.scenario.objective)
    return stats, events, attack


def validate_placement_answer(
    scenario: ScenarioSpec,
    answer: PlacementAnswer,
    registry: PluginRegistry,
) -> dict[str, Any]:
    registry.validate_scenario(
        [effect for item in scenario.items for effect in item.effects], scenario.objective
    )
    item_instances = scenario.expanded_item_ids()
    board_cells = scenario.board.valid_cells()
    errors: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    cell_owner: dict[tuple[int, int], str] = {}
    placements: list[PlacedItem] = []

    for index, raw in enumerate(answer.placements):
        item = item_instances.get(raw.item_id)
        if item is None:
            errors.append({"code": "unknown_item", "path": index, "item_id": raw.item_id})
            continue
        if raw.item_id in used_ids:
            errors.append({"code": "duplicate_item", "path": index, "item_id": raw.item_id})
            continue
        if raw.rotation not in item.rotations:
            errors.append(
                {
                    "code": "unsupported_rotation",
                    "path": index,
                    "item_id": raw.item_id,
                    "rotation": raw.rotation,
                }
            )
            continue
        cells = occupied_cells(item.shape, raw.row, raw.col, raw.rotation)
        outside = sorted(cells - board_cells)
        if outside:
            errors.append(
                {"code": "out_of_bounds", "path": index, "item_id": raw.item_id, "cells": outside}
            )
            continue
        collisions = sorted(cell for cell in cells if cell in cell_owner)
        if collisions:
            errors.append(
                {
                    "code": "overlap",
                    "path": index,
                    "item_id": raw.item_id,
                    "cells": collisions,
                    "with": sorted({cell_owner[cell] for cell in collisions}),
                }
            )
            continue
        placed = PlacedItem(
            item_id=raw.item_id,
            item=item,
            row=raw.row,
            col=raw.col,
            rotation=raw.rotation,
            cells=cells,
        )
        placements.append(placed)
        used_ids.add(raw.item_id)
        for cell in cells:
            cell_owner[cell] = raw.item_id

    if errors:
        return {
            "valid": False,
            "error_type": "illegal_placement",
            "actual_attack": 0,
            "errors": errors,
        }

    by_id = {placement.item_id: placement for placement in placements}
    context = EvaluationContext(
        scenario=scenario,
        placements=tuple(placements),
        by_id=by_id,
        cell_owner=cell_owner,
    )
    stats, events, attack = score_evaluation_context(context, registry)
    breakdown = []
    for placement in sorted(placements, key=lambda current: current.item_id):
        item_events = [event for event in events if event.target == placement.item_id]
        breakdown.append(
            {
                "item_id": placement.item_id,
                "category": placement.item.category,
                "base_stats": placement.item.stats,
                "bonuses": [
                    {
                        "source": event.source,
                        "stat": event.stat,
                        "amount": event.amount,
                        "reason": event.reason,
                    }
                    for event in item_events
                ],
                "final_stats": stats[placement.item_id],
            }
        )

    board: list[list[str | None]] = []
    valid_board = scenario.board.valid_cells()
    for row in range(scenario.board.height):
        board.append(
            [
                "#" if (row, col) not in valid_board else cell_owner.get((row, col))
                for col in range(scenario.board.width)
            ]
        )
    return {
        "valid": True,
        "error_type": None,
        "actual_attack": attack,
        "placements": len(placements),
        "used_cells": len(cell_owner),
        "empty_cells": len(valid_board) - len(cell_owner),
        "board": board,
        "breakdown": breakdown,
        "effects": [event.__dict__ for event in events],
    }


def parse_and_score_output(
    scenario: ScenarioSpec,
    model_output: str,
    registry: PluginRegistry,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    if finish_reason == "length":
        return {
            "valid": False,
            "error_type": "output_truncated",
            "actual_attack": 0,
            "errors": [
                {
                    "code": "output_truncated",
                    "message": "provider reported that the output token limit was reached",
                }
            ],
            "finish_reason": finish_reason,
        }
    try:
        value = json.loads(model_output)
    except json.JSONDecodeError as error:
        error_type = "output_not_json"
        return {
            "valid": False,
            "error_type": error_type,
            "actual_attack": 0,
            "errors": [{"code": error_type, "message": str(error)}],
        }
    try:
        answer = PlacementAnswer.model_validate(value)
    except ValidationError as error:
        return {
            "valid": False,
            "error_type": "answer_schema",
            "actual_attack": 0,
            "errors": error.errors(include_url=False, include_context=False, include_input=False),
        }
    result = validate_placement_answer(scenario, answer, registry)
    result["finish_reason"] = finish_reason
    return result
