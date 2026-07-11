from pathlib import Path

import pytest

from backpack_bench.io import load_yaml
from backpack_bench.plugins import PluginRegistry
from backpack_bench.schemas import ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry() -> PluginRegistry:
    return PluginRegistry(load_external=False)


@pytest.fixture
def mixed_scenario() -> ScenarioSpec:
    return load_yaml(ROOT / "scenarios" / "curated" / "mixed_3x3.yaml", ScenarioSpec)
