import random
from pathlib import Path
from typing import Any, ClassVar

from backpack_bench.canonical import canonical_json
from backpack_bench.domain import EffectEvent, EvaluationContext, PlacedItem
from backpack_bench.evaluation import scenario_hash
from backpack_bench.generator import _candidate, generate
from backpack_bench.io import load_json, load_yaml
from backpack_bench.oracle import oracle_hash, solve_exact
from backpack_bench.plugins import PluginRegistry
from backpack_bench.schemas import (
    BoardSpec,
    EffectSpec,
    GeneratorSpec,
    ItemTypeSpec,
    ObjectiveSpec,
    OracleArtifact,
    ScenarioSpec,
    StrictModel,
)
from backpack_bench.suite import load_suite

ROOT = Path(__file__).resolve().parents[1]


class EmptyConfig(StrictModel):
    pass


class NoBoundEffectHandler:
    kind: ClassVar[str] = "test_no_bound"
    version: ClassVar[str] = "1.0.0"

    def validate_config(self, value: dict[str, Any]) -> EmptyConfig:
        return EmptyConfig.model_validate(value)

    def apply(
        self,
        context: EvaluationContext,
        source: PlacedItem,
        effect: EffectSpec,
    ) -> list[EffectEvent]:
        return []

    def render_zh(self, effect: EffectSpec) -> str:
        return "无额外效果。"

    def orientation_signature(self, effect: EffectSpec, rotation: int) -> None:
        return None


def test_exact_solver_proves_ray_optimum(registry: PluginRegistry) -> None:
    scenario = load_yaml(ROOT / "scenarios" / "curated" / "ray_3x3.yaml", ScenarioSpec)
    oracle = solve_exact(scenario, registry, timeout_seconds=10)
    assert oracle.exact
    assert oracle.optimal_attack == 21


def test_oracle_hash_excludes_wall_clock_telemetry() -> None:
    oracle = load_json(ROOT / "oracles" / "curated" / "ray_3x3.json", OracleArtifact)
    slower = oracle.model_copy(update={"elapsed_seconds": oracle.elapsed_seconds + 123.0})
    assert oracle_hash(oracle) == oracle_hash(slower)


def test_generator_candidate_is_seed_deterministic(registry: PluginRegistry) -> None:
    spec = GeneratorSpec(
        id="test-generator",
        seed=1234,
        count=1,
        board_sizes=[(3, 3), (4, 4)],
        output_dir="out",
        oracle_dir="oracle",
    )
    first = _candidate(spec, random.Random(spec.seed), 1)
    second = _candidate(spec, random.Random(spec.seed), 1)
    assert canonical_json(first) == canonical_json(second)


def test_full_generator_writes_identical_scenarios(
    tmp_path: Path, registry: PluginRegistry
) -> None:
    base = GeneratorSpec(
        id="determinism",
        seed=42,
        count=1,
        board_sizes=[(3, 3)],
        max_item_instances=4,
        oracle_timeout_seconds=10,
        output_dir="first/scenarios",
        oracle_dir="first/oracles",
    )
    first = generate(base, tmp_path, registry)
    second = generate(
        base.model_copy(update={"output_dir": "second/scenarios", "oracle_dir": "second/oracles"}),
        tmp_path,
        registry,
    )
    first_path = Path(first.scenario_paths[0])
    second_path = Path(second.scenario_paths[0])
    assert first_path.read_bytes() == second_path.read_bytes()
    first_scenario = load_yaml(first_path, ScenarioSpec)
    second_scenario = load_yaml(second_path, ScenarioSpec)
    assert scenario_hash(first_scenario, registry) == scenario_hash(second_scenario, registry)


def test_solver_falls_back_when_plugin_has_no_upper_bound(registry: PluginRegistry) -> None:
    registry.register_effect(NoBoundEffectHandler())
    scenario = ScenarioSpec(
        id="no-bound",
        title="无上界插件测试",
        board=BoardSpec(width=1, height=1),
        items=[
            ItemTypeSpec(
                id="weapon",
                display_name="武器",
                shape=[(0, 0)],
                category="weapon",
                stats={"attack": 2},
                effects=[EffectSpec(type="test_no_bound", config={})],
            )
        ],
        objective=ObjectiveSpec(type="sum_stat", config={"category": "weapon", "stat": "attack"}),
    )
    oracle = solve_exact(scenario, registry, timeout_seconds=5)
    assert oracle.exact
    assert oracle.optimal_attack == 2


def test_public_suites_are_exact_and_complete(registry: PluginRegistry) -> None:
    smoke = load_suite(ROOT / "suites" / "smoke-v1.yaml", registry)
    core = load_suite(ROOT / "suites" / "core-v1.yaml", registry)
    ladder = load_suite(ROOT / "suites" / "ladder-v1.yaml", registry)
    expanded_ladder = load_suite(ROOT / "suites" / "ladder-v2.yaml", registry)
    assert len(smoke.scenarios) == 4
    assert len(core.scenarios) == 20
    assert len(ladder.scenarios) == 5
    assert len(expanded_ladder.scenarios) == 15
    assert all(item.entry.weight == 1.0 for item in core.scenarios)
    assert all(item.entry.weight == 1.0 for item in ladder.scenarios)
    assert all(item.entry.weight == 1.0 for item in expanded_ladder.scenarios)
    assert smoke.suite_hash == "607017adff4960d25efbfeec5772d3b3a52300714e1a32fc73671f81cb176904"
    assert core.suite_hash == "90f9ddc8182a3597d0c7a13f9ca02fcd66ff587f25a24d2ba8b48f671b152468"
    assert ladder.suite_hash == "4e838684c27018b78e09de86a63a543d6543151d99b7e19e9c62e7d416b60918"
    assert (
        expanded_ladder.suite_hash
        == "ff68e7cacf578531b7e16d7457b87b727a9ea3cd315d87dc562bacf3a595c2ba"
    )
    assert all(item.oracle.exact and item.oracle.optimal_attack for item in core.scenarios)
    assert all(item.oracle.exact and item.oracle.optimal_attack for item in ladder.scenarios)
    assert all(
        item.oracle.exact and item.oracle.optimal_attack for item in expanded_ladder.scenarios
    )
    generated = [item.scenario for item in core.scenarios if item.scenario.provenance is not None]
    assert len(generated) == 15
    assert sum(item.scenario.provenance is None for item in core.scenarios) == 5
    assert all((item.board.height, item.board.width) in {(3, 3), (4, 4)} for item in generated)
    assert all(sum(part.count for part in item.items) <= 8 for item in generated)
    assert all(1 <= len(part.shape) <= 4 for item in generated for part in item.items)
