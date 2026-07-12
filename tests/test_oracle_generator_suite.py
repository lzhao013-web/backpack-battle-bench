import random
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image

from backpack_bench.canonical import canonical_json
from backpack_bench.catalog import load_scenario
from backpack_bench.domain import EffectEvent, EvaluationContext, PlacedItem
from backpack_bench.evaluation import scenario_hash
from backpack_bench.generator import _candidate, generate
from backpack_bench.io import load_json
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


def test_exact_solver_proves_smoke_optimum(registry: PluginRegistry) -> None:
    scenario = load_scenario(ROOT / "scenarios" / "smoke" / "packing_3x3.yaml").scenario
    oracle = solve_exact(scenario, registry, timeout_seconds=10)
    assert oracle.exact
    assert oracle.optimal_attack == 18


def test_oracle_hash_excludes_wall_clock_telemetry() -> None:
    oracle = load_json(ROOT / "oracles" / "smoke" / "packing_3x3.json", OracleArtifact)
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
        catalog_path="catalog.yaml",
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
        catalog_path="first/catalog.yaml",
    )
    first = generate(base, tmp_path, registry)
    second = generate(
        base.model_copy(
            update={
                "output_dir": "second/scenarios",
                "oracle_dir": "second/oracles",
                "catalog_path": "second/catalog.yaml",
            }
        ),
        tmp_path,
        registry,
    )
    first_path = Path(first.scenario_paths[0])
    second_path = Path(second.scenario_paths[0])
    assert first_path.read_bytes() == second_path.read_bytes()
    assert Path(first.catalog_path).read_bytes() == Path(second.catalog_path).read_bytes()
    first_scenario = load_scenario(first_path).scenario
    second_scenario = load_scenario(second_path).scenario
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
    ladder = load_suite(ROOT / "suites" / "ladder-v2.yaml", registry)
    assert len(smoke.scenarios) == 1
    assert len(ladder.scenarios) == 15
    assert all(item.entry.weight == 1.0 for item in smoke.scenarios)
    assert all(item.entry.weight == 1.0 for item in ladder.scenarios)
    assert smoke.suite_hash == "294ab4ca73139e74ca7d97c94fdb4f661776689edf6f739662e9a07d828752ad"
    assert ladder.suite_hash == "8026a2e88feabb8622c7de67991068541ce99cd59d5527157e2629ae089b0f0d"
    assert all(item.oracle.exact and item.oracle.optimal_attack for item in smoke.scenarios)
    assert all(item.oracle.exact and item.oracle.optimal_attack for item in ladder.scenarios)
    assert smoke.catalog_hash == ladder.catalog_hash
    assert smoke.visual_pack.pack_hash != ladder.visual_pack.pack_hash
    assert smoke.visual_pack.spec.status == "placeholder"
    assert ladder.visual_pack.spec.status == "final"
    assert ladder.visual_pack.spec.renderer_id == "visual_art_compositor"
    assert len(smoke.catalog.items) == len(ladder.visual_pack.spec.assets) == 57
    assert len(ladder.visual_pack.spec.cards) == 57
    assert len(ladder.visual_pack.spec.scenario_sheets) == 32

    sources = ROOT / "visual-packs" / "visual-art-v1" / "sources"
    assert len(list(sources.glob("*.png"))) == 57
    for item in ladder.catalog.items:
        _, path = ladder.visual_pack.asset(item.id)
        with Image.open(path) as image:
            alpha = image.convert("RGBA").getchannel("A")
            height = max(row for row, _ in item.shape) + 1
            width = max(col for _, col in item.shape) + 1
            assert image.size == (width * 256, height * 256)
            occupied = set(item.shape)
            for row in range(height):
                for col in range(width):
                    expected = 255 if (row, col) in occupied else 0
                    assert alpha.getpixel((col * 256 + 128, row * 256 + 128)) == expected
