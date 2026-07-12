from pathlib import Path

import pytest

from backpack_bench.evaluation import validate_placement_answer
from backpack_bench.oracle import _optimistic_bound, _score_selected, candidate_placements
from backpack_bench.plugins import PluginRegistry
from backpack_bench.schemas import BoardSpec, EffectSpec, ItemTypeSpec, ObjectiveSpec, ScenarioSpec
from backpack_bench.suite import load_suite

ROOT = Path(__file__).resolve().parents[1]


def test_expanded_ladder_has_three_distinct_focuses_per_level(
    registry: PluginRegistry,
) -> None:
    ladder = load_suite(ROOT / "suites" / "ladder-v2.yaml", registry)
    expected_focus = {
        "ladder-l1-adjacency": "regular-board",
        "ladder-l1-shape-lock": "exact-fill",
        "ladder-l1-compass": "asymmetric-adjacency",
        "ladder-l2-selection": "packing",
        "ladder-l2-resonance": "multi-contact",
        "ladder-l2-dual-focus": "multi-source",
        "ladder-l3-crossfire": "rotation",
        "ladder-l3-occlusion": "ordering",
        "ladder-l3-parallel-rays": "parallel-rays",
        "ladder-l4-curse-and-block": "curse",
        "ladder-l4-curse-screen": "forced-anchor",
        "ladder-l4-crossed-fields": "positive-negative-fields",
        "ladder-l5-prism-maze": "blocker",
        "ladder-l5-offset-web": "sparse-offsets",
        "ladder-l5-ray-labyrinth": "ray-labyrinth",
    }
    assert len(ladder.scenarios) == 15
    assert len(set(expected_focus.values())) == 15
    assert {entry.scenario.id for entry in ladder.scenarios} == set(expected_focus)

    level_counts = {f"L{level}": 0 for level in range(1, 6)}
    for entry in ladder.scenarios:
        scenario = entry.scenario
        level = next(tag for tag in scenario.tags if tag in level_counts)
        level_counts[level] += 1
        assert expected_focus[scenario.id] in scenario.tags
        assert scenario.board.width <= 5 and scenario.board.height <= 5
        assert entry.oracle.exact and not entry.oracle.timed_out
        assert entry.oracle.witness is not None
        replay = validate_placement_answer(scenario, entry.oracle.witness, registry)
        assert replay["valid"]
        assert replay["actual_attack"] == entry.oracle.optimal_attack
    assert set(level_counts.values()) == {3}


def test_new_ladder_scenarios_cover_non_overlapping_rule_boundaries(
    registry: PluginRegistry,
) -> None:
    ladder = load_suite(ROOT / "suites" / "ladder-v2.yaml", registry)
    scenarios = {entry.scenario.id: entry.scenario for entry in ladder.scenarios}

    shape_lock = scenarios["ladder-l1-shape-lock"]
    assert not any(item.effects for item in shape_lock.items)
    assert sum(len(item.shape) * item.count for item in shape_lock.items) == len(
        shape_lock.board.valid_cells()
    )

    compass = scenarios["ladder-l1-compass"]
    compass_directions = [
        tuple(direction) for direction in compass.items[-1].effects[0].config["directions"]
    ]
    assert compass_directions == [(-1, 0), (0, 1)]

    resonance = scenarios["ladder-l2-resonance"]
    assert resonance.items[-1].effects[0].config["once_per_target"] is False
    dual_focus = scenarios["ladder-l2-dual-focus"]
    assert sum(bool(item.effects) for item in dual_focus.items) == 2

    occlusion = scenarios["ladder-l3-occlusion"]
    occlusion_effect = next(effect for item in occlusion.items for effect in item.effects)
    assert occlusion_effect.config["amount"] > 0 and occlusion_effect.config["blocked"] is True
    parallel = scenarios["ladder-l3-parallel-rays"]
    parallel_effect = next(effect for item in parallel.items for effect in item.effects)
    assert parallel_effect.config["blocked"] is False
    assert parallel_effect.config["once_per_target"] is False

    screen = scenarios["ladder-l4-curse-screen"]
    screen_effect = next(effect for item in screen.items for effect in item.effects)
    assert screen_effect.config["amount"] < 0 and screen_effect.config["blocked"] is True
    assert any(item.category == "support" and not item.effects for item in screen.items)
    screen_entry = next(
        entry for entry in ladder.scenarios if entry.scenario.id == "ladder-l4-curse-screen"
    )
    assert screen_entry.oracle.witness is not None
    without_screens = screen_entry.oracle.witness.model_copy(
        update={
            "placements": [
                placement
                for placement in screen_entry.oracle.witness.placements
                if not placement.item_id.startswith("screen_")
            ]
        }
    )
    unshielded = validate_placement_answer(screen, without_screens, registry)
    assert unshielded["actual_attack"] < screen_entry.oracle.optimal_attack
    crossed = scenarios["ladder-l4-crossed-fields"]
    crossed_amounts = [effect.config["amount"] for item in crossed.items for effect in item.effects]
    assert any(amount > 0 for amount in crossed_amounts)
    assert any(amount < 0 for amount in crossed_amounts)

    offset_web = scenarios["ladder-l5-offset-web"]
    assert not any(
        effect.type == "ray_stat_bonus" for item in offset_web.items for effect in item.effects
    )
    assert any(
        max(abs(row), abs(col)) == 2
        for item in offset_web.items
        for effect in item.effects
        for row, col in effect.config["directions"]
    )
    labyrinth = scenarios["ladder-l5-ray-labyrinth"]
    ray_configs = [
        effect.config
        for item in labyrinth.items
        for effect in item.effects
        if effect.type == "ray_stat_bonus"
    ]
    assert any(
        config["amount"] > 0 and config["once_per_target"] is False for config in ray_configs
    )
    assert any(config["amount"] < 0 and config["blocked"] is True for config in ray_configs)


@pytest.mark.parametrize("effect_type", ["adjacent_stat_bonus", "ray_stat_bonus"])
def test_placement_aware_bound_is_safe_for_repeated_hits(
    effect_type: str, registry: PluginRegistry
) -> None:
    if effect_type == "adjacent_stat_bonus":
        board = BoardSpec(width=2, height=2)
        shape = [(0, 0), (0, 1)]
        source_row = 0
        effect = EffectSpec(
            type=effect_type,
            config={
                "directions": [(1, 0), (1, -1)],
                "target_category": "weapon",
                "stat": "attack",
                "amount": 5,
                "once_per_target": False,
            },
        )
        target_row = 1
    else:
        board = BoardSpec(width=1, height=3)
        shape = [(0, 0), (1, 0)]
        source_row = 1
        effect = EffectSpec(
            type=effect_type,
            config={
                "direction": (-1, 0),
                "target_category": "weapon",
                "stat": "attack",
                "amount": 5,
                "once_per_target": False,
                "blocked": True,
            },
        )
        target_row = 0

    source = ItemTypeSpec(
        id="source",
        display_name="效果源",
        shape=shape,
        rotations=[0],
        category="support",
        effects=[effect],
    )
    target = ItemTypeSpec(
        id="target",
        display_name="目标",
        shape=[(0, 0)],
        rotations=[0],
        category="weapon",
        stats={"attack": 1},
    )
    scenario = ScenarioSpec(
        id=f"bound-{effect_type}",
        title="布局感知上界安全性",
        board=board,
        items=[source, target],
        objective=ObjectiveSpec(type="sum_stat", config={"category": "weapon", "stat": "attack"}),
    )
    source_candidate = next(
        candidate
        for candidate in candidate_placements(scenario, source, registry)
        if candidate.row == source_row and candidate.col == 0
    )
    target_candidate = next(
        candidate
        for candidate in candidate_placements(scenario, target, registry)
        if candidate.row == target_row and candidate.col == 0
    )
    actual = _score_selected(scenario, [source_candidate, target_candidate], registry)
    bound = _optimistic_bound(scenario, [source_candidate], [target], registry)

    assert actual == 11
    assert bound is not None
    assert bound >= actual
