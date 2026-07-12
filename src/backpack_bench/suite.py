"""Resolve and verify immutable public suite manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backpack_bench import __version__
from backpack_bench.canonical import content_hash, text_hash
from backpack_bench.catalog import (
    ResolvedVisualPack,
    item_catalog_hash,
    load_item_catalog,
    load_scenario,
    load_visual_pack,
)
from backpack_bench.evaluation import scenario_hash
from backpack_bench.io import load_json, load_yaml
from backpack_bench.oracle import oracle_hash, verify_oracle
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import PROMPT_TEMPLATE_VERSION, render_prompt
from backpack_bench.schemas import (
    ItemCatalogSpec,
    OracleArtifact,
    ScenarioSpec,
    SuiteScenarioEntry,
    SuiteSpec,
)


@dataclass(frozen=True)
class ResolvedScenario:
    entry: SuiteScenarioEntry
    scenario_path: Path
    oracle_path: Path
    scenario: ScenarioSpec
    oracle: OracleArtifact
    prompt: str
    prompt_hash: str


@dataclass(frozen=True)
class ResolvedSuite:
    path: Path
    spec: SuiteSpec
    scenarios: tuple[ResolvedScenario, ...]
    suite_hash: str
    catalog: ItemCatalogSpec
    catalog_hash: str
    visual_pack: ResolvedVisualPack


def suite_hash(spec: SuiteSpec) -> str:
    return content_hash(
        {
            "suite": spec,
            "engine_version": __version__,
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        }
    )


def load_suite(
    path: Path,
    registry: PluginRegistry,
    verify: bool = True,
) -> ResolvedSuite:
    path = path.resolve()
    spec = load_yaml(path, SuiteSpec)
    resolved: list[ResolvedScenario] = []
    errors: list[str] = []
    catalog_path = (path.parent / spec.item_catalog).resolve()
    visual_pack_path = (path.parent / spec.visual_pack).resolve()
    try:
        catalog = load_item_catalog(catalog_path)
        actual_catalog_hash = item_catalog_hash(catalog)
        if actual_catalog_hash != spec.item_catalog_hash:
            errors.append("suite item catalog hash mismatch")
        visual_pack = load_visual_pack(visual_pack_path, catalog)
        if visual_pack.pack_hash != spec.visual_pack_hash:
            errors.append("suite visual pack hash mismatch")
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid suite assets: {error}") from error
    for entry in spec.scenarios:
        scenario_path = (path.parent / entry.scenario).resolve()
        oracle_path = (path.parent / entry.oracle).resolve()
        try:
            loaded = load_scenario(scenario_path)
            scenario = loaded.scenario
            if loaded.catalog_path != catalog_path:
                errors.append(f"{scenario.id}: scenario uses a different item catalog path")
            if loaded.catalog_hash != actual_catalog_hash:
                errors.append(f"{scenario.id}: scenario item catalog hash mismatch")
            oracle = load_json(oracle_path, OracleArtifact)
            actual_scenario_hash = scenario_hash(scenario, registry)
            actual_oracle_hash = oracle_hash(oracle)
            if actual_scenario_hash != entry.scenario_hash:
                errors.append(f"{scenario.id}: scenario hash mismatch")
            if actual_oracle_hash != entry.oracle_hash:
                errors.append(f"{scenario.id}: oracle hash mismatch")
            used_plugins = {effect.type for item in scenario.items for effect in item.effects} | {
                scenario.objective.type
            }
            forbidden = sorted(used_plugins - set(spec.allowed_plugins))
            if forbidden:
                errors.append(f"{scenario.id}: plugins not allowed by suite: {forbidden}")
            if verify:
                errors.extend(
                    f"{scenario.id}: {error}" for error in verify_oracle(scenario, oracle, registry)
                )
            prompt = render_prompt(scenario, registry)
            resolved.append(
                ResolvedScenario(
                    entry=entry,
                    scenario_path=scenario_path,
                    oracle_path=oracle_path,
                    scenario=scenario,
                    oracle=oracle,
                    prompt=prompt,
                    prompt_hash=text_hash(prompt),
                )
            )
        except (OSError, ValueError) as error:
            errors.append(f"{entry.scenario}: {error}")
    if errors:
        raise ValueError("invalid suite:\n- " + "\n- ".join(errors))
    return ResolvedSuite(
        path=path,
        spec=spec,
        scenarios=tuple(resolved),
        suite_hash=suite_hash(spec),
        catalog=catalog,
        catalog_hash=actual_catalog_hash,
        visual_pack=visual_pack,
    )
