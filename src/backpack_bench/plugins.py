"""Built-in effects/objective and entry-point based extension registry."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import Field

from backpack_bench.domain import EffectEvent, EvaluationContext, PlacedItem
from backpack_bench.geometry import Cell, rotate_vector
from backpack_bench.schemas import EffectSpec, ObjectiveSpec, StrictModel

CATEGORY_LABELS_ZH = {"weapon": "武器", "support": "辅助物品"}
STAT_LABELS_ZH = {"attack": "攻击"}


def category_label_zh(value: str) -> str:
    return CATEGORY_LABELS_ZH.get(value, value)


def stat_label_zh(value: str) -> str:
    return STAT_LABELS_ZH.get(value, value)


def _stat_change_zh(amount: int, stat: str) -> str:
    label = stat_label_zh(stat)
    return f"获得 +{amount} {label}" if amount >= 0 else f"失去 {abs(amount)} {label}"


def _join_zh(values: list[str]) -> str:
    if len(values) < 2:
        return "".join(values)
    return "、".join(values[:-1]) + "和" + values[-1]


def _nearby_position_zh(direction: Cell) -> str:
    names = {
        (-1, 0): "上方",
        (1, 0): "下方",
        (0, -1): "左侧",
        (0, 1): "右侧",
        (-1, -1): "左上方",
        (-1, 1): "右上方",
        (1, -1): "左下方",
        (1, 1): "右下方",
    }
    if direction in names:
        return names[direction]
    row, col = direction
    moves: list[str] = []
    if row:
        moves.append(f"向{'上' if row < 0 else '下'} {abs(row)} 格")
    if col:
        moves.append(f"向{'左' if col < 0 else '右'} {abs(col)} 格")
    return "、".join(moves) + "的位置"


def _ray_direction_zh(direction: Cell) -> str:
    names = {(-1, 0): "向上", (1, 0): "向下", (0, -1): "向左", (0, 1): "向右"}
    return names.get(direction, f"沿方向 {direction}")


def _repeat_rule_zh(category: str, once_per_target: bool, source: str) -> str:
    target = category_label_zh(category)
    if once_per_target:
        return f"每件{target}最多只获得一次这个物品的效果。"
    return f"同一件{target}如果被多个{source}命中，效果会重复计算。"


class EffectHandler(Protocol):
    kind: ClassVar[str]
    version: ClassVar[str]

    def validate_config(self, value: dict[str, Any]) -> StrictModel: ...

    def apply(
        self,
        context: EvaluationContext,
        source: PlacedItem,
        effect: EffectSpec,
    ) -> list[EffectEvent]: ...

    def render_zh(self, effect: EffectSpec) -> str: ...

    def orientation_signature(self, effect: EffectSpec, rotation: int) -> Any: ...


@runtime_checkable
class OracleBoundProvider(Protocol):
    """Optional capability; handlers without it force exhaustive enumeration."""

    def optimistic_bonus(
        self,
        effect: EffectSpec,
        source_count: int,
        target_count: int,
        source_cell_count: int,
        target_cell_count: int,
    ) -> int | None: ...


class ObjectiveHandler(Protocol):
    kind: ClassVar[str]
    version: ClassVar[str]

    def validate_config(self, value: dict[str, Any]) -> StrictModel: ...

    def score(
        self,
        context: EvaluationContext,
        stats: dict[str, dict[str, int]],
        objective: ObjectiveSpec,
    ) -> int: ...

    def render_zh(self, objective: ObjectiveSpec) -> str: ...


class AdjacentBonusConfig(StrictModel):
    directions: list[Cell] = Field(default_factory=lambda: [(0, -1), (0, 1)])
    target_category: str = "weapon"
    stat: str = "attack"
    amount: int = 1
    once_per_target: bool = True


class RayBonusConfig(StrictModel):
    direction: Cell = (-1, 0)
    target_category: str = "weapon"
    stat: str = "attack"
    amount: int = 4
    once_per_target: bool = True
    blocked: bool = False


class SumStatConfig(StrictModel):
    category: str = "weapon"
    stat: str = "attack"


class AdjacentStatBonusHandler:
    kind: ClassVar[str] = "adjacent_stat_bonus"
    version: ClassVar[str] = "1.0.0"

    def validate_config(self, value: dict[str, Any]) -> AdjacentBonusConfig:
        config = AdjacentBonusConfig.model_validate(value)
        if not config.directions:
            raise ValueError("adjacent directions cannot be empty")
        if any(direction == (0, 0) for direction in config.directions):
            raise ValueError("adjacent direction cannot be zero")
        return config

    def apply(
        self,
        context: EvaluationContext,
        source: PlacedItem,
        effect: EffectSpec,
    ) -> list[EffectEvent]:
        config = self.validate_config(effect.config)
        target_ids: list[str] = []
        for source_cell in sorted(source.cells):
            for base_direction in config.directions:
                delta_row, delta_col = rotate_vector(base_direction, source.rotation)
                target_cell = (source_cell[0] + delta_row, source_cell[1] + delta_col)
                target_id = context.cell_owner.get(target_cell)
                if target_id is None or target_id == source.item_id:
                    continue
                target = context.by_id[target_id]
                if target.item.category == config.target_category:
                    target_ids.append(target_id)
        if config.once_per_target:
            target_ids = sorted(set(target_ids))
        return [
            EffectEvent(
                source=source.item_id,
                target=target_id,
                stat=config.stat,
                amount=config.amount,
                reason="相邻方向加成",
            )
            for target_id in target_ids
        ]

    def render_zh(self, effect: EffectSpec) -> str:
        config = self.validate_config(effect.config)
        positions = _join_zh([_nearby_position_zh(value) for value in config.directions])
        category = category_label_zh(config.target_category)
        all_nearby = all(max(abs(row), abs(col)) == 1 for row, col in config.directions)
        targets = (
            f"紧挨在它每个格子{positions}的{category}"
            if all_nearby
            else f"位于它每个格子{positions}的{category}"
        )
        return (
            f"未旋转时，{targets}{_stat_change_zh(config.amount, config.stat)}。"
            "旋转后，生效方向也会跟着旋转。"
            f"{_repeat_rule_zh(config.target_category, config.once_per_target, '生效位置')}"
        )

    def orientation_signature(self, effect: EffectSpec, rotation: int) -> Any:
        config = self.validate_config(effect.config)
        return tuple(sorted(rotate_vector(direction, rotation) for direction in config.directions))

    def optimistic_bonus(
        self,
        effect: EffectSpec,
        source_count: int,
        target_count: int,
        source_cell_count: int,
        target_cell_count: int,
    ) -> int:
        config = self.validate_config(effect.config)
        contact_slots = source_cell_count * len(config.directions)
        contacts = (
            min(target_count, contact_slots)
            if config.once_per_target
            else contact_slots
            if target_count
            else 0
        )
        return max(0, config.amount) * source_count * contacts


class RayStatBonusHandler:
    kind: ClassVar[str] = "ray_stat_bonus"
    version: ClassVar[str] = "1.0.0"

    def validate_config(self, value: dict[str, Any]) -> RayBonusConfig:
        config = RayBonusConfig.model_validate(value)
        if config.direction == (0, 0):
            raise ValueError("ray direction cannot be zero")
        if config.direction[0] != 0 and config.direction[1] != 0:
            raise ValueError("v1 ray direction must be orthogonal")
        return config

    def apply(
        self,
        context: EvaluationContext,
        source: PlacedItem,
        effect: EffectSpec,
    ) -> list[EffectEvent]:
        config = self.validate_config(effect.config)
        delta_row, delta_col = rotate_vector(config.direction, source.rotation)
        target_ids: list[str] = []
        board = context.scenario.board.valid_cells()
        for start_row, start_col in sorted(source.cells):
            row, col = start_row + delta_row, start_col + delta_col
            while (row, col) in board:
                owner = context.cell_owner.get((row, col))
                if owner is not None and owner != source.item_id:
                    target = context.by_id[owner]
                    if target.item.category == config.target_category:
                        target_ids.append(owner)
                    if config.blocked:
                        break
                row += delta_row
                col += delta_col
        if config.once_per_target:
            target_ids = sorted(set(target_ids))
        return [
            EffectEvent(
                source=source.item_id,
                target=target_id,
                stat=config.stat,
                amount=config.amount,
                reason="方向射线区域加成",
            )
            for target_id in target_ids
        ]

    def render_zh(self, effect: EffectSpec) -> str:
        config = self.validate_config(effect.config)
        blocked = (
            "遇到第一个物品就会停止，不会继续穿过它。"
            if config.blocked
            else "中间有其他物品也不会挡住效果。"
        )
        category = category_label_zh(config.target_category)
        return (
            f"未旋转时，从它每个格子{_ray_direction_zh(config.direction)}直到背包边缘的"
            f"直线上，所有{category}{_stat_change_zh(config.amount, config.stat)}。"
            f"旋转后，生效方向也会跟着旋转。{blocked}"
            f"{_repeat_rule_zh(config.target_category, config.once_per_target, '直线')}"
        )

    def orientation_signature(self, effect: EffectSpec, rotation: int) -> Any:
        config = self.validate_config(effect.config)
        return rotate_vector(config.direction, rotation), config.blocked

    def optimistic_bonus(
        self,
        effect: EffectSpec,
        source_count: int,
        target_count: int,
        source_cell_count: int,
        target_cell_count: int,
    ) -> int:
        config = self.validate_config(effect.config)
        if config.blocked:
            ray_slots = source_cell_count
            hits = (
                min(target_count, ray_slots)
                if config.once_per_target
                else ray_slots
                if target_count
                else 0
            )
        else:
            hits = target_count if config.once_per_target else target_cell_count * source_cell_count
        return max(0, config.amount) * source_count * hits


class SumStatObjectiveHandler:
    kind: ClassVar[str] = "sum_stat"
    version: ClassVar[str] = "1.0.0"

    def validate_config(self, value: dict[str, Any]) -> SumStatConfig:
        return SumStatConfig.model_validate(value)

    def score(
        self,
        context: EvaluationContext,
        stats: dict[str, dict[str, int]],
        objective: ObjectiveSpec,
    ) -> int:
        config = self.validate_config(objective.config)
        return sum(
            item_stats.get(config.stat, 0)
            for item_id, item_stats in stats.items()
            if context.by_id[item_id].item.category == config.category
        )

    def render_zh(self, objective: ObjectiveSpec) -> str:
        config = self.validate_config(objective.config)
        return (
            f"目标：让背包中所有{category_label_zh(config.category)}的"
            f"{stat_label_zh(config.stat)}总和尽可能高。"
        )


class PluginRegistry:
    def __init__(self, load_external: bool = True) -> None:
        self.effects: dict[str, EffectHandler] = {}
        self.objectives: dict[str, ObjectiveHandler] = {}
        self.register_effect(AdjacentStatBonusHandler())
        self.register_effect(RayStatBonusHandler())
        self.register_objective(SumStatObjectiveHandler())
        if load_external:
            self._load_entry_points()

    def register_effect(self, handler: EffectHandler) -> None:
        if handler.kind in self.effects:
            raise ValueError(f"duplicate effect handler: {handler.kind}")
        self.effects[handler.kind] = handler

    def register_objective(self, handler: ObjectiveHandler) -> None:
        if handler.kind in self.objectives:
            raise ValueError(f"duplicate objective handler: {handler.kind}")
        self.objectives[handler.kind] = handler

    def _load_entry_points(self) -> None:
        for entry_point in entry_points(group="backpack_bench.effects"):
            loaded = entry_point.load()
            self.register_effect(loaded() if isinstance(loaded, type) else loaded)
        for entry_point in entry_points(group="backpack_bench.objectives"):
            loaded = entry_point.load()
            self.register_objective(loaded() if isinstance(loaded, type) else loaded)

    def effect(self, kind: str) -> EffectHandler:
        try:
            return self.effects[kind]
        except KeyError as error:
            raise ValueError(f"unknown effect plugin: {kind}") from error

    def objective(self, kind: str) -> ObjectiveHandler:
        try:
            return self.objectives[kind]
        except KeyError as error:
            raise ValueError(f"unknown objective plugin: {kind}") from error

    def validate_scenario(self, effects: list[EffectSpec], objective: ObjectiveSpec) -> None:
        for effect in effects:
            self.effect(effect.type).validate_config(effect.config)
        self.objective(objective.type).validate_config(objective.config)

    def versions_for(self, effects: list[EffectSpec], objective: ObjectiveSpec) -> dict[str, str]:
        result = {effect.type: self.effect(effect.type).version for effect in effects}
        result[objective.type] = self.objective(objective.type).version
        return dict(sorted(result.items()))
