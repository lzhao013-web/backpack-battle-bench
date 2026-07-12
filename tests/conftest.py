from pathlib import Path

import pytest

from backpack_bench.catalog import load_scenario
from backpack_bench.plugins import PluginRegistry
from backpack_bench.schemas import ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry() -> PluginRegistry:
    return PluginRegistry(load_external=False)


@pytest.fixture
def smoke_scenario() -> ScenarioSpec:
    return load_scenario(ROOT / "scenarios" / "smoke" / "packing_3x3.yaml").scenario
