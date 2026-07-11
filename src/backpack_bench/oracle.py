"""Deterministic exact enumerator for official small scenarios."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from backpack_bench.canonical import canonical_json, content_hash
from backpack_bench.domain import EvaluationContext, PlacedItem
from backpack_bench.evaluation import (
    scenario_hash,
    score_evaluation_context,
    validate_placement_answer,
)
from backpack_bench.geometry import occupied_cells, rotated_shape
from backpack_bench.plugins import OracleBoundProvider, PluginRegistry
from backpack_bench.schemas import (
    ItemTypeSpec,
    OracleArtifact,
    PlacementAnswer,
    PlacementInput,
    Rotation,
    ScenarioSpec,
)

SOLVER_ID = "enumeration_branch_bound"
SOLVER_VERSION = "1.2.0"


@dataclass(frozen=True)
class Candidate:
    item: ItemTypeSpec
    row: int
    col: int
    rotation: Rotation
    cells: frozenset[tuple[int, int]]


class SolverTimeout(RuntimeError):
    pass


def oracle_hash(oracle: OracleArtifact) -> str:
    """Hash the deterministic proof content, excluding wall-clock telemetry."""
    return content_hash(oracle.model_dump(mode="json", exclude={"elapsed_seconds"}))


def _effect_signature(item: ItemTypeSpec, rotation: int, registry: PluginRegistry) -> str:
    return canonical_json(
        [
            registry.effect(effect.type).orientation_signature(effect, rotation)
            for effect in item.effects
        ]
    )


def candidate_placements(
    scenario: ScenarioSpec,
    item: ItemTypeSpec,
    registry: PluginRegistry,
) -> list[Candidate]:
    board = scenario.board.valid_cells()
    candidates: list[Candidate] = []
    seen: set[tuple[Any, ...]] = set()
    for rotation in item.rotations:
        shape = rotated_shape(item.shape, rotation)
        max_row = max(row for row, _ in shape)
        max_col = max(col for _, col in shape)
        for row in range(scenario.board.height - max_row):
            for col in range(scenario.board.width - max_col):
                cells = occupied_cells(item.shape, row, col, rotation)
                if not cells <= board:
                    continue
                signature = (tuple(sorted(cells)), _effect_signature(item, rotation, registry))
                if signature in seen:
                    continue
                seen.add(signature)
                candidates.append(
                    Candidate(item=item, row=row, col=col, rotation=rotation, cells=cells)
                )
    return candidates


def materialize(selected: list[Candidate]) -> PlacementAnswer:
    counters: dict[str, int] = {}
    placements: list[PlacementInput] = []
    for candidate in selected:
        counters[candidate.item.id] = counters.get(candidate.item.id, 0) + 1
        placements.append(
            PlacementInput(
                item_id=f"{candidate.item.id}_{counters[candidate.item.id]}",
                row=candidate.row,
                col=candidate.col,
                rotation=candidate.rotation,
            )
        )
    return PlacementAnswer(placements=placements)


def _context_for_selected(
    scenario: ScenarioSpec,
    selected: list[Candidate],
) -> EvaluationContext:
    counters: dict[str, int] = {}
    placements: list[PlacedItem] = []
    cell_owner: dict[tuple[int, int], str] = {}
    for candidate in selected:
        counters[candidate.item.id] = counters.get(candidate.item.id, 0) + 1
        item_id = f"{candidate.item.id}_{counters[candidate.item.id]}"
        placement = PlacedItem(
            item_id=item_id,
            item=candidate.item,
            row=candidate.row,
            col=candidate.col,
            rotation=candidate.rotation,
            cells=candidate.cells,
        )
        placements.append(placement)
        for cell in candidate.cells:
            if cell in cell_owner:
                raise AssertionError("oracle candidate placements overlap")
            cell_owner[cell] = item_id
    return EvaluationContext(
        scenario=scenario,
        placements=tuple(placements),
        by_id={placement.item_id: placement for placement in placements},
        cell_owner=cell_owner,
    )


def _score_selected(
    scenario: ScenarioSpec,
    selected: list[Candidate],
    registry: PluginRegistry,
) -> int:
    return score_evaluation_context(_context_for_selected(scenario, selected), registry)[2]


def _optimistic_bound(
    scenario: ScenarioSpec,
    selected: list[Candidate],
    remaining_items: list[ItemTypeSpec],
    registry: PluginRegistry,
) -> int | None:
    # The generic solver only knows how to bound the built-in additive objective.
    # Unknown objectives and effects without an explicitly safe bound are still
    # solved exactly, but without branch-and-bound pruning.
    if scenario.objective.type != "sum_stat":
        return None
    objective_config = registry.objective(scenario.objective.type).validate_config(
        scenario.objective.config
    )
    category = getattr(objective_config, "category", "weapon")
    stat = getattr(objective_config, "stat", "attack")
    possible: list[tuple[ItemTypeSpec, int]] = []
    selected_counts: dict[str, int] = {}
    for candidate in selected:
        selected_counts[candidate.item.id] = selected_counts.get(candidate.item.id, 0) + 1
    for item in scenario.items:
        if item.id in selected_counts:
            possible.append((item, selected_counts[item.id]))
    possible.extend((item, item.count) for item in remaining_items)
    base = sum(
        max(0, item.stats.get(stat, 0)) * count
        for item, count in possible
        if item.category == category
    )
    target_count = sum(count for item, count in possible if item.category == category)
    target_cell_count = sum(
        len(item.shape) * count for item, count in possible if item.category == category
    )
    effect_bound = 0
    for item, count in possible:
        for effect in item.effects:
            handler = registry.effect(effect.type)
            if not isinstance(handler, OracleBoundProvider):
                return None
            bonus = handler.optimistic_bonus(
                effect,
                count,
                target_count,
                len(item.shape),
                target_cell_count,
            )
            if bonus is None:
                return None
            effect_bound += bonus
    return base + effect_bound


def solve_exact(
    scenario: ScenarioSpec,
    registry: PluginRegistry,
    timeout_seconds: float = 60.0,
) -> OracleArtifact:
    started = time.perf_counter()
    deadline = started + timeout_seconds
    registry.validate_scenario(
        [effect for item in scenario.items for effect in item.effects], scenario.objective
    )
    groups = sorted(
        scenario.items,
        key=lambda item: (-len(item.shape), -item.count, item.id),
    )
    candidates = {item.id: candidate_placements(scenario, item, registry) for item in groups}
    best_attack = -1
    best_answer = PlacementAnswer(placements=[])
    nodes = 0

    def check_timeout() -> None:
        if time.perf_counter() >= deadline:
            raise SolverTimeout

    def evaluate(selected: list[Candidate]) -> None:
        nonlocal best_attack, best_answer, nodes
        nodes += 1
        if nodes % 256 == 0:
            check_timeout()
        attack = _score_selected(scenario, selected, registry)
        if attack > best_attack:
            best_attack = attack
            best_answer = materialize(selected)

    def search_group(
        group_index: int,
        occupied: frozenset[tuple[int, int]],
        selected: list[Candidate],
    ) -> None:
        check_timeout()
        if group_index == len(groups):
            evaluate(selected)
            return
        remaining = groups[group_index:]
        bound = _optimistic_bound(scenario, selected, remaining, registry)
        if best_attack >= 0 and bound is not None and bound <= best_attack:
            return
        item = groups[group_index]
        item_candidates = candidates[item.id]

        def choose(
            start: int,
            count: int,
            current_occupied: frozenset[tuple[int, int]],
            chosen: list[Candidate],
        ) -> None:
            if count < item.count:
                for candidate_index in range(start, len(item_candidates)):
                    candidate = item_candidates[candidate_index]
                    if candidate.cells & current_occupied:
                        continue
                    choose(
                        candidate_index + 1,
                        count + 1,
                        current_occupied | candidate.cells,
                        chosen + [candidate],
                    )
            search_group(group_index + 1, current_occupied, selected + chosen)

        choose(0, 0, occupied, [])

    timed_out = False
    try:
        search_group(0, frozenset(), [])
    except SolverTimeout:
        timed_out = True
    elapsed = time.perf_counter() - started
    return OracleArtifact(
        scenario_id=scenario.id,
        scenario_hash=scenario_hash(scenario, registry),
        solver_id=SOLVER_ID,
        solver_version=SOLVER_VERSION,
        exact=not timed_out,
        optimal_attack=best_attack if best_attack >= 0 else None,
        witness=best_answer if best_attack >= 0 else None,
        nodes_evaluated=nodes,
        elapsed_seconds=elapsed,
        timed_out=timed_out,
    )


def verify_oracle(
    scenario: ScenarioSpec,
    oracle: OracleArtifact,
    registry: PluginRegistry,
) -> list[str]:
    errors: list[str] = []
    expected_hash = scenario_hash(scenario, registry)
    if oracle.scenario_hash != expected_hash:
        errors.append("oracle scenario_hash does not match scenario")
    if not oracle.exact or oracle.timed_out:
        errors.append("oracle is not an exact completed proof")
    if oracle.optimal_attack is None or oracle.optimal_attack <= 0:
        errors.append("oracle optimal_attack must be positive")
    if oracle.witness is None:
        errors.append("oracle witness is missing")
    else:
        result = validate_placement_answer(scenario, oracle.witness, registry)
        if not result["valid"]:
            errors.append("oracle witness is illegal")
        elif result["actual_attack"] != oracle.optimal_attack:
            errors.append("oracle witness attack does not equal optimal_attack")
    return errors
