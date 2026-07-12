"""Generate the official zh-CN single-turn prompt from scenario semantics."""

from __future__ import annotations

from backpack_bench.geometry import shape_size
from backpack_bench.plugins import PluginRegistry, category_label_zh, stat_label_zh
from backpack_bench.schemas import ScenarioSpec

PROMPT_TEMPLATE_VERSION = "zh-CN-spatial-v2"
VISUAL_PROMPT_TEMPLATE_VERSION = "zh-CN-visual-card-v1"


def render_prompt(scenario: ScenarioSpec, registry: PluginRegistry) -> str:
    registry.validate_scenario(
        [effect for item in scenario.items for effect in item.effects], scenario.objective
    )
    lines = [
        "你正在完成一个纯文字二维空间规划题。",
        registry.objective(scenario.objective.type).render_zh(scenario.objective),
        "",
        "## 坐标系与背包",
        "",
        "- 行号 row 从上到下递增，列号 col 从左到右递增，左上角是 (0,0)。",
        "- 输出坐标是物品旋转后的外接矩形左上角。",
        f"- 背包外接矩形为 {scenario.board.height} 行 × {scenario.board.width} 列。",
    ]
    rectangle_cells = scenario.board.width * scenario.board.height
    if len(scenario.board.valid_cells()) != rectangle_cells:
        cells = ", ".join(f"({row},{col})" for row, col in sorted(scenario.board.valid_cells()))
        lines.append(f"- 仅以下格子可用：{cells}。")
    else:
        lines.append("- 外接矩形中的所有格子均可用。")
    lines.extend(
        [
            "",
            "## 旋转与合法摆放",
            "",
            "- rotation 为相对初始方向顺时针旋转，只能使用物品列出的角度。",
            "- 旋转会同时旋转形状和局部效果方向。",
            "- 所有占用格必须在可用背包格内，物品之间不能重叠，可以不使用某些物品并留下空格。",
            "- 每个物品实例最多使用一次；未使用实例不要出现在输出中。",
            "",
            "## 可用物品",
            "",
        ]
    )
    for item in scenario.items:
        item_ids = "、".join(f"{item.id}_{index}" for index in range(1, item.count + 1))
        height, width = shape_size(item.shape)
        shape = ", ".join(f"({row},{col})" for row, col in item.shape)
        stats = (
            "、".join(
                f"{stat_label_zh(name)}={value}" for name, value in sorted(item.stats.items())
            )
            or "无"
        )
        rotations = ", ".join(str(rotation) for rotation in item.rotations)
        lines.extend(
            [
                f"### {item.display_name}",
                f"- 实例 ID：{item_ids}",
                (
                    f"- 类别：{category_label_zh(item.category)}；数量：{item.count}；"
                    f"基础属性：{stats}"
                ),
                f"- 初始形状外接尺寸：{height}×{width}；占用格偏移：{shape}",
                f"- 允许 rotation：{rotations}",
            ]
        )
        if item.effects:
            lines.append("- 效果：")
            for effect in item.effects:
                lines.append(f"  - {registry.effect(effect.type).render_zh(effect)}")
        else:
            lines.append("- 效果：无")
        lines.append("")
    lines.extend(
        [
            "## 输出格式",
            "",
            "只输出一个严格合法的 JSON 对象，不要输出 Markdown、解释、注释或你计算的分数。",
            "顶层只能包含 placements 字段，每个元素只能包含 item_id、row、col、rotation：",
            "",
            "{",
            '  "placements": [',
            "    {",
            f'      "item_id": "{scenario.items[0].id}_1",',
            '      "row": 0,',
            '      "col": 0,',
            '      "rotation": 0',
            "    }",
            "  ]",
            "}",
            "",
            "placements 的顺序不影响结果。请输出你认为能取得最高目标值的合法摆放。",
        ]
    )
    return "\n".join(lines)


def render_visual_prompt(
    scenario: ScenarioSpec,
    registry: PluginRegistry,
    mode: str = "visual_full",
) -> str:
    """Render a prompt that deliberately leaves shape or all item facts in the image."""
    if mode not in {"visual_shape", "visual_full"}:
        raise ValueError(f"unsupported visual prompt mode: {mode}")
    registry.validate_scenario(
        [effect for item in scenario.items for effect in item.effects], scenario.objective
    )
    lines = [
        "你正在完成一个视觉二维空间规划题。",
        "题目图片中的彩色格、属性和效果图示都是题目数据，不是装饰。",
        registry.objective(scenario.objective.type).render_zh(scenario.objective),
        "",
        "## 坐标与摆放规则",
        "",
        "- 行号 row 从上到下递增，列号 col 从左到右递增，左上角是 (0,0)。",
        "- 输出坐标是物品旋转后的外接矩形左上角。",
        (
            f"- 背包外接矩形为 {scenario.board.height} 行 × "
            f"{scenario.board.width} 列；可用格以图片为准。"
        ),
        "- rotation 为相对图片中 0° 初始方向顺时针旋转。",
        "- 旋转会同时旋转物品形状和箭头表示的局部效果方向。",
        "- 所有彩色占用格必须在可用背包格内，物品之间不能重叠。",
        "- 可以不使用某些物品；每个实例最多使用一次。",
        "",
        "## 图片标签与实例 ID",
        "",
    ]
    for index, item in enumerate(scenario.items):
        label = chr(ord("A") + index)
        item_ids = "、".join(f"{item.id}_{number}" for number in range(1, item.count + 1))
        lines.append(f"- 图片中的 {label} 对应实例：{item_ids}")
        if mode == "visual_shape":
            stats = (
                "、".join(
                    f"{stat_label_zh(name)}={value}" for name, value in sorted(item.stats.items())
                )
                or "无"
            )
            rotations = ", ".join(str(rotation) for rotation in item.rotations)
            lines.append(
                f"  类别：{category_label_zh(item.category)}；基础属性：{stats}；"
                f"允许 rotation：{rotations}"
            )
            if item.effects:
                for effect in item.effects:
                    lines.append(f"  效果：{registry.effect(effect.type).render_zh(effect)}")
            else:
                lines.append("  效果：无")
    if mode == "visual_full":
        lines.extend(
            [
                "",
                "每种物品的占用格、允许旋转、类别、基础攻击和效果均只在图片卡片中给出。",
                "请先读图，再进行摆放。灰色叉格不是物品占用格。",
            ]
        )
    lines.extend(
        [
            "",
            "## 输出格式",
            "",
            "只输出一个严格合法的 JSON 对象，不要输出 Markdown、解释、注释或分数。",
            "顶层只能包含 placements，每个元素只能包含 item_id、row、col、rotation：",
            "",
            "{",
            '  "placements": [',
            "    {",
            f'      "item_id": "{scenario.items[0].id}_1",',
            '      "row": 0,',
            '      "col": 0,',
            '      "rotation": 0',
            "    }",
            "  ]",
            "}",
            "",
            "placements 的顺序不影响结果。请输出你认为目标值最高的合法摆放。",
        ]
    )
    return "\n".join(lines)
