"""Stable seeded scenario generation with exact-oracle admission."""

from __future__ import annotations

import os
import random
from pathlib import Path

from backpack_bench.io import atomic_write_json, atomic_write_yaml
from backpack_bench.oracle import solve_exact
from backpack_bench.plugins import PluginRegistry
from backpack_bench.schemas import (
    BoardSpec,
    EffectSpec,
    GeneratorOutput,
    GeneratorSpec,
    InventoryEntrySpec,
    ItemCatalogSpec,
    ItemDefinitionSpec,
    ItemTypeSpec,
    ObjectiveSpec,
    ScenarioDocumentSpec,
    ScenarioProvenance,
    ScenarioSpec,
)

GENERATOR_VERSION = "1.0.0"
SHAPES: tuple[list[tuple[int, int]], ...] = (
    [(0, 0)],
    [(0, 0), (0, 1)],
    [(0, 0), (0, 1), (0, 2)],
    [(0, 0), (1, 0), (1, 1)],
    [(0, 0), (0, 1), (1, 0), (1, 1)],
)


def _candidate(spec: GeneratorSpec, rng: random.Random, candidate_index: int) -> ScenarioSpec:
    catalog_prefix = spec.id.replace("-", "_").replace(".", "_")
    height, width = rng.choice(spec.board_sizes)
    board = BoardSpec(width=width, height=height)
    weapon_a_shape = rng.choice([shape for shape in SHAPES if len(shape) <= 3])
    weapon_b_shape = rng.choice([shape for shape in SHAPES if 1 <= len(shape) <= 4])
    count_a = rng.randint(1, 2)
    count_b = rng.randint(1, 2)
    remaining = spec.max_item_instances - count_a - count_b
    adjacent_count = 1 if remaining >= 1 else 0
    ray_count = 1 if remaining >= 2 else 0
    items = [
        ItemTypeSpec(
            id="item_a",
            catalog_id=f"{catalog_prefix}_{candidate_index:04d}_item_a",
            display_name="物品A",
            count=count_a,
            shape=weapon_a_shape,
            category="weapon",
            stats={"attack": rng.randint(3, 7)},
        ),
        ItemTypeSpec(
            id="item_b",
            catalog_id=f"{catalog_prefix}_{candidate_index:04d}_item_b",
            display_name="物品B",
            count=count_b,
            shape=weapon_b_shape,
            category="weapon",
            stats={"attack": rng.randint(2, 6)},
        ),
    ]
    if adjacent_count:
        base_directions = rng.choice([[(0, -1), (0, 1)], [(-1, 0), (1, 0)]])
        items.append(
            ItemTypeSpec(
                id="item_c",
                catalog_id=f"{catalog_prefix}_{candidate_index:04d}_item_c",
                display_name="物品C",
                count=adjacent_count,
                shape=[(0, 0)],
                effects=[
                    EffectSpec(
                        type="adjacent_stat_bonus",
                        config={
                            "directions": base_directions,
                            "target_category": "weapon",
                            "stat": "attack",
                            "amount": rng.randint(1, 3),
                            "once_per_target": True,
                        },
                    )
                ],
            )
        )
    if ray_count:
        ray_length = 2 if min(height, width) >= 2 else 1
        items.append(
            ItemTypeSpec(
                id="item_d",
                catalog_id=f"{catalog_prefix}_{candidate_index:04d}_item_d",
                display_name="物品D",
                count=ray_count,
                shape=[(0, col) for col in range(ray_length)],
                effects=[
                    EffectSpec(
                        type="ray_stat_bonus",
                        config={
                            "direction": [-1, 0],
                            "target_category": "weapon",
                            "stat": "attack",
                            "amount": rng.randint(2, 5),
                            "once_per_target": True,
                            "blocked": False,
                        },
                    )
                ],
            )
        )
    return ScenarioSpec(
        id=f"{spec.id}.{candidate_index:04d}",
        title=f"公开生成题 {candidate_index:04d}",
        board=board,
        item_catalog_id=f"{spec.id}-items",
        items=items,
        objective=ObjectiveSpec(type="sum_stat", config={"category": "weapon", "stat": "attack"}),
        tags=["generated", f"board-{height}x{width}"],
        difficulty="medium" if height * width <= 9 else "hard",
        provenance=ScenarioProvenance(
            generator_id=spec.id,
            generator_version=GENERATOR_VERSION,
            seed=spec.seed,
            candidate_index=candidate_index,
        ),
    )


def generate(
    spec: GeneratorSpec,
    config_dir: Path,
    registry: PluginRegistry,
) -> GeneratorOutput:
    scenario_dir = (config_dir / spec.output_dir).resolve()
    oracle_dir = (config_dir / spec.oracle_dir).resolve()
    catalog_path = (config_dir / spec.catalog_path).resolve()
    rng = random.Random(spec.seed)
    scenarios: list[str] = []
    oracles: list[str] = []
    catalog_items: list[ItemDefinitionSpec] = []
    candidate_index = 0
    fixed_indices = set(spec.candidate_indices or [])
    max_candidates = max(fixed_indices) if fixed_indices else spec.count * 100
    while len(scenarios) < spec.count and candidate_index < max_candidates:
        candidate_index += 1
        scenario = _candidate(spec, rng, candidate_index)
        if fixed_indices and candidate_index not in fixed_indices:
            continue
        oracle = solve_exact(scenario, registry, spec.oracle_timeout_seconds)
        if not oracle.exact or oracle.optimal_attack is None or oracle.optimal_attack <= 0:
            if fixed_indices:
                raise RuntimeError(
                    f"frozen candidate {candidate_index} did not finish an exact positive proof"
                )
            continue
        scenario_path = scenario_dir / f"{scenario.id}.yaml"
        oracle_path = oracle_dir / f"{scenario.id}.json"
        for item in scenario.items:
            catalog_items.append(
                ItemDefinitionSpec(
                    **item.model_dump(
                        mode="python",
                        exclude={"id", "catalog_id", "count"},
                    ),
                    id=item.catalog_id or item.id,
                )
            )
        relative_catalog = os.path.relpath(catalog_path, scenario_path.parent).replace("\\", "/")
        document = ScenarioDocumentSpec(
            id=scenario.id,
            version=scenario.version,
            title=scenario.title,
            locale=scenario.locale,
            board=scenario.board,
            item_catalog=relative_catalog,
            inventory=[
                InventoryEntrySpec(
                    item_id=item.catalog_id or item.id,
                    count=item.count,
                    instance_prefix=item.id,
                )
                for item in scenario.items
            ],
            objective=scenario.objective,
            tags=scenario.tags,
            difficulty=scenario.difficulty,
            provenance=scenario.provenance,
        )
        atomic_write_yaml(scenario_path, document)
        atomic_write_json(oracle_path, oracle)
        scenarios.append(str(scenario_path))
        oracles.append(str(oracle_path))
    if len(scenarios) != spec.count:
        raise RuntimeError(
            f"generated {len(scenarios)} exact scenarios after {candidate_index} candidates; "
            f"requested {spec.count}"
        )
    catalog = ItemCatalogSpec(
        id=f"{spec.id}-items",
        version=GENERATOR_VERSION,
        items=catalog_items,
    )
    atomic_write_yaml(catalog_path, catalog)
    return GeneratorOutput(
        scenario_paths=scenarios,
        oracle_paths=oracles,
        catalog_path=str(catalog_path),
    )
