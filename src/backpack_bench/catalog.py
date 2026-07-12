"""Resolve shared item catalogs, scenario inventories and frozen visual packs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backpack_bench.canonical import content_hash
from backpack_bench.io import load_yaml
from backpack_bench.schemas import (
    ItemCatalogSpec,
    ItemTypeSpec,
    ScenarioDocumentSpec,
    ScenarioSpec,
    VisualAssetSpec,
    VisualPackSpec,
)


def item_catalog_hash(catalog: ItemCatalogSpec) -> str:
    return content_hash(catalog)


def visual_pack_hash(pack: VisualPackSpec) -> str:
    return content_hash(pack)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ResolvedScenarioFile:
    path: Path
    document: ScenarioDocumentSpec
    catalog_path: Path
    catalog: ItemCatalogSpec
    catalog_hash: str
    scenario: ScenarioSpec


@dataclass(frozen=True)
class ResolvedVisualPack:
    path: Path
    spec: VisualPackSpec
    pack_hash: str
    asset_paths: dict[str, Path]
    card_paths: dict[str, Path]
    scenario_sheet_paths: dict[tuple[str, str], Path]

    def asset(self, item_id: str) -> tuple[VisualAssetSpec, Path]:
        definitions = {asset.item_id: asset for asset in self.spec.assets}
        try:
            return definitions[item_id], self.asset_paths[item_id]
        except KeyError as error:
            raise KeyError(f"visual pack has no asset for item: {item_id}") from error

    def card(self, item_id: str) -> tuple[VisualAssetSpec, Path]:
        definitions = {asset.item_id: asset for asset in self.spec.cards}
        try:
            return definitions[item_id], self.card_paths[item_id]
        except KeyError as error:
            raise KeyError(f"visual pack has no card for item: {item_id}") from error

    def scenario_sheet(
        self,
        scenario_id: str,
        scenario_hash: str,
        mode: Literal["visual_shape", "visual_full"] = "visual_full",
    ) -> Path:
        definitions = {
            (asset.scenario_id, asset.mode): asset for asset in self.spec.scenario_sheets
        }
        try:
            definition = definitions[(scenario_id, mode)]
            path = self.scenario_sheet_paths[(scenario_id, mode)]
        except KeyError as error:
            raise KeyError(f"visual pack has no sheet for scenario: {scenario_id}") from error
        if definition.scenario_hash != scenario_hash:
            raise ValueError(f"visual sheet scenario hash mismatch: {scenario_id}")
        return path


def load_item_catalog(path: Path) -> ItemCatalogSpec:
    return load_yaml(path.resolve(), ItemCatalogSpec)


def load_scenario(path: Path) -> ResolvedScenarioFile:
    path = path.resolve()
    document = load_yaml(path, ScenarioDocumentSpec)
    catalog_path = (path.parent / document.item_catalog).resolve()
    catalog = load_item_catalog(catalog_path)
    definitions = {item.id: item for item in catalog.items}
    items: list[ItemTypeSpec] = []
    missing: list[str] = []
    for entry in document.inventory:
        definition = definitions.get(entry.item_id)
        if definition is None:
            missing.append(entry.item_id)
            continue
        items.append(
            ItemTypeSpec(
                **definition.model_dump(mode="python", exclude={"id"}),
                id=entry.local_id(),
                catalog_id=definition.id,
                count=entry.count,
            )
        )
    if missing:
        raise ValueError(f"scenario references unknown catalog items: {sorted(set(missing))}")
    scenario = ScenarioSpec(
        id=document.id,
        version=document.version,
        title=document.title,
        locale=document.locale,
        board=document.board,
        item_catalog_id=catalog.id,
        items=items,
        objective=document.objective,
        tags=document.tags,
        difficulty=document.difficulty,
        provenance=document.provenance,
    )
    return ResolvedScenarioFile(
        path=path,
        document=document,
        catalog_path=catalog_path,
        catalog=catalog,
        catalog_hash=item_catalog_hash(catalog),
        scenario=scenario,
    )


def load_visual_pack(path: Path, catalog: ItemCatalogSpec) -> ResolvedVisualPack:
    path = path.resolve()
    spec = load_yaml(path, VisualPackSpec)
    expected_catalog_hash = item_catalog_hash(catalog)
    if spec.item_catalog_hash != expected_catalog_hash:
        raise ValueError("visual pack item catalog hash mismatch")
    expected_ids = {item.id for item in catalog.items}
    actual_ids = {asset.item_id for asset in spec.assets}
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(f"visual pack coverage mismatch; missing={missing}, extra={extra}")
    asset_paths: dict[str, Path] = {}
    card_paths: dict[str, Path] = {}
    scenario_sheet_paths: dict[tuple[str, str], Path] = {}

    def verify_asset(relative: str, expected_hash: str, label: str) -> Path:
        asset_path = (path.parent / relative).resolve()
        if not asset_path.is_file():
            raise ValueError(f"visual asset does not exist: {relative}")
        actual_hash = file_sha256(asset_path)
        if actual_hash != expected_hash:
            raise ValueError(f"visual asset hash mismatch: {label}")
        return asset_path

    for asset in spec.assets:
        asset_paths[asset.item_id] = verify_asset(asset.image, asset.sha256, asset.item_id)
    if spec.cards:
        card_ids = {asset.item_id for asset in spec.cards}
        if card_ids != expected_ids:
            raise ValueError("visual card coverage mismatch")
        for asset in spec.cards:
            card_paths[asset.item_id] = verify_asset(
                asset.image, asset.sha256, f"card:{asset.item_id}"
            )
    for scenario_asset in spec.scenario_sheets:
        scenario_sheet_paths[(scenario_asset.scenario_id, scenario_asset.mode)] = verify_asset(
            scenario_asset.image,
            scenario_asset.sha256,
            f"scenario:{scenario_asset.scenario_id}",
        )
    return ResolvedVisualPack(
        path=path,
        spec=spec,
        pack_hash=visual_pack_hash(spec),
        asset_paths=asset_paths,
        card_paths=card_paths,
        scenario_sheet_paths=scenario_sheet_paths,
    )
