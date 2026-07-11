"""Runtime domain objects kept separate from public file schemas."""

from __future__ import annotations

from dataclasses import dataclass

from backpack_bench.geometry import Cell
from backpack_bench.schemas import ItemTypeSpec, ScenarioSpec


@dataclass(frozen=True)
class PlacedItem:
    item_id: str
    item: ItemTypeSpec
    row: int
    col: int
    rotation: int
    cells: frozenset[Cell]


@dataclass(frozen=True)
class EffectEvent:
    source: str
    target: str
    stat: str
    amount: int
    reason: str


@dataclass(frozen=True)
class EvaluationContext:
    scenario: ScenarioSpec
    placements: tuple[PlacedItem, ...]
    by_id: dict[str, PlacedItem]
    cell_owner: dict[Cell, str]
