"""Deterministic shape-aware placeholder assets for future multimodal suites."""

from __future__ import annotations

import hashlib
import textwrap
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from backpack_bench.catalog import file_sha256, item_catalog_hash
from backpack_bench.geometry import shape_size
from backpack_bench.io import atomic_write_bytes, atomic_write_text, atomic_write_yaml
from backpack_bench.plugins import PluginRegistry, category_label_zh, stat_label_zh
from backpack_bench.schemas import (
    ItemCatalogSpec,
    ItemDefinitionSpec,
    ScenarioSpec,
    ScenarioVisualAssetSpec,
    VisualAssetSpec,
    VisualPackSpec,
)

PLACEHOLDER_RENDERER_ID = "shape_svg"
PLACEHOLDER_RENDERER_VERSION = "1.0.0"
CARD_RENDERER_ID = "visual_card_png"
CARD_RENDERER_VERSION = "1.0.0"
ART_RENDERER_ID = "visual_art_compositor"
ART_RENDERER_VERSION = "1.0.0"

INK = (31, 41, 55, 255)
MUTED = (91, 103, 120, 255)
PAPER = (247, 249, 252, 255)
PANEL = (255, 255, 255, 255)
GRID = (105, 119, 137, 255)
INVALID = (226, 230, 236, 255)
WEAPON = (222, 99, 70, 255)
SUPPORT = (63, 132, 181, 255)
POSITIVE = (26, 145, 94, 255)
NEGATIVE = (205, 62, 73, 255)


def _font_path() -> str:
    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "DejaVuSans.ttf"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    if bold:
        bold_candidates = (
            Path("C:/Windows/Fonts/msyhbd.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        )
        for candidate in bold_candidates:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), size)
    return ImageFont.truetype(_font_path(), size)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    return buffer.getvalue()


def _item_color(category: str) -> tuple[int, int, int, int]:
    return WEAPON if category == "weapon" else SUPPORT


def render_item_sprite_png(item: ItemDefinitionSpec, cell_size: int = 128) -> Image.Image:
    height, width = shape_size(item.shape)
    image = Image.new("RGBA", (width * cell_size, height * cell_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = _item_color(item.category)
    digest = hashlib.sha256(item.id.encode("utf-8")).digest()
    for index, (row, col) in enumerate(item.shape):
        x0, y0 = col * cell_size + 5, row * cell_size + 5
        x1, y1 = (col + 1) * cell_size - 5, (row + 1) * cell_size - 5
        shade = digest[index % len(digest)] % 28
        fill = tuple(max(0, part - shade) for part in color[:3]) + (255,)
        draw.rounded_rectangle((x0, y0, x1, y1), 16, fill=fill, outline=INK, width=4)
        draw.line((x0 + 20, y1 - 24, x1 - 20, y0 + 24), fill=(255, 255, 255, 175), width=10)
        draw.ellipse(
            (
                x0 + cell_size // 2 - 10,
                y0 + cell_size // 2 - 10,
                x0 + cell_size // 2 + 10,
                y0 + cell_size // 2 + 10,
            ),
            fill=(255, 255, 255, 220),
        )
    return image


def compose_art_sprite_png(
    item: ItemDefinitionSpec,
    source: Image.Image,
    cell_size: int = 256,
) -> Image.Image:
    """Resize generated art and enforce the catalog footprint as hard alpha."""
    height, width = shape_size(item.shape)
    target_size = (width * cell_size, height * cell_size)
    artwork = source.convert("RGBA").resize(target_size, Image.Resampling.LANCZOS)
    mask = Image.new("L", target_size, 0)
    draw = ImageDraw.Draw(mask)
    radius = max(8, cell_size // 14)
    inset = max(1, cell_size // 128)
    for row, col in item.shape:
        draw.rounded_rectangle(
            (
                col * cell_size + inset,
                row * cell_size + inset,
                (col + 1) * cell_size - inset,
                (row + 1) * cell_size - inset,
            ),
            radius=radius,
            fill=255,
        )
    artwork.putalpha(mask)
    return artwork


def _draw_arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    color: tuple[int, int, int, int],
    width: int = 8,
) -> None:
    draw.line((start, end), fill=color, width=width)
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = max(1.0, (dx * dx + dy * dy) ** 0.5)
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    tip = end
    left = (end[0] - ux * 18 + px * 12, end[1] - uy * 18 + py * 12)
    right = (end[0] - ux * 18 - px * 12, end[1] - uy * 18 - py * 12)
    draw.polygon((tip, left, right), fill=color)


def _draw_footprint(
    draw: ImageDraw.ImageDraw,
    item: ItemDefinitionSpec,
    box: tuple[int, int, int, int],
    effects: bool = True,
) -> None:
    left, top, right, bottom = box
    height, width = shape_size(item.shape)
    cell = min(64, (right - left - 80) // max(1, width), (bottom - top - 80) // max(1, height))
    origin_x = (left + right - width * cell) // 2
    origin_y = (top + bottom - height * cell) // 2
    occupied = set(item.shape)
    for row in range(height):
        for col in range(width):
            x0, y0 = origin_x + col * cell, origin_y + row * cell
            rectangle = (x0, y0, x0 + cell, y0 + cell)
            if (row, col) in occupied:
                draw.rounded_rectangle(
                    rectangle, 8, fill=_item_color(item.category), outline=INK, width=3
                )
                draw.line(
                    (x0 + 12, y0 + cell - 12, x0 + cell - 12, y0 + 12),
                    fill=(255, 255, 255, 210),
                    width=7,
                )
            else:
                draw.rectangle(rectangle, fill=INVALID, outline=GRID, width=2)
                draw.line((x0 + 8, y0 + 8, x0 + cell - 8, y0 + cell - 8), fill=GRID, width=3)
                draw.line((x0 + cell - 8, y0 + 8, x0 + 8, y0 + cell - 8), fill=GRID, width=3)
    draw.text((left + 12, top + 8), "0° ↑", font=_font(25, True), fill=INK)
    if not effects:
        return
    if effects:
        _draw_effect_arrows(draw, item, origin_x, origin_y, width, height, cell)


def _draw_effect_arrows(
    draw: ImageDraw.ImageDraw,
    item: ItemDefinitionSpec,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
    cell: int,
) -> None:
    center = ((origin_x * 2 + width * cell) / 2, (origin_y * 2 + height * cell) / 2)
    for effect in item.effects:
        amount = int(effect.config.get("amount", 0))
        color = POSITIVE if amount >= 0 else NEGATIVE
        if effect.type == "adjacent_stat_bonus":
            directions = effect.config.get("directions", [])
            scale = cell * 1.15
        elif effect.type == "ray_stat_bonus":
            directions = [effect.config.get("direction", [-1, 0])]
            scale = cell * 1.75
        else:
            continue
        for row, col in directions:
            end = (center[0] + float(col) * scale, center[1] + float(row) * scale)
            _draw_arrow(draw, center, end, color)
            draw.text(
                (end[0] + 5, end[1] - 17),
                f"{amount:+d}",
                font=_font(23, True),
                fill=color,
            )


def _draw_art_sprite(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    item: ItemDefinitionSpec,
    sprite: Image.Image,
    box: tuple[int, int, int, int],
    *,
    effects: bool,
) -> None:
    left, top, right, bottom = box
    height, width = shape_size(item.shape)
    cell = min(96, (right - left - 36) // max(1, width), (bottom - top - 54) // max(1, height))
    rendered = sprite.resize((width * cell, height * cell), Image.Resampling.LANCZOS)
    origin_x = (left + right - rendered.width) // 2
    origin_y = (top + bottom - rendered.height) // 2 + 12
    image.alpha_composite(rendered, (origin_x, origin_y))
    draw.text((left + 12, top + 8), "0° ↑", font=_font(25, True), fill=INK)
    if effects:
        _draw_effect_arrows(draw, item, origin_x, origin_y, width, height, cell)


def _wrapped_lines(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False) or [""]


def render_item_card_png(
    item: ItemDefinitionSpec,
    registry: PluginRegistry,
    *,
    label: str | None = None,
    count: int | None = None,
    local_id: str | None = None,
    detail: bool = True,
    sprite: Image.Image | None = None,
) -> Image.Image:
    card_height = 560
    image = Image.new("RGBA", (720, card_height), PANEL)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 717, card_height - 3), 18, fill=PANEL, outline=GRID, width=3)
    label_text = f"{label}  " if label else ""
    count_text = f" ×{count}" if count is not None else ""
    draw.text(
        (28, 18), f"{label_text}{item.display_name}{count_text}", font=_font(34, True), fill=INK
    )
    if local_id:
        draw.text((28, 61), local_id, font=_font(20), fill=MUTED)
    rotations = "/".join(f"{value}°" for value in item.rotations)
    draw.text((475, 27), f"旋转 {rotations}", font=_font(21), fill=MUTED)
    footprint_box = (24, 92, 330 if detail else 696, card_height - 25)
    if sprite is None:
        _draw_footprint(draw, item, footprint_box, effects=detail)
    else:
        _draw_art_sprite(image, draw, item, sprite, footprint_box, effects=detail)
    if not detail:
        draw.text(
            (355, 112),
            "只从彩色格读取占用形状",
            font=_font(25, True),
            fill=INK,
        )
        draw.text((355, 154), "属性和效果见文字提示", font=_font(22), fill=MUTED)
        return image
    category = category_label_zh(item.category)
    stats = (
        " · ".join(
            f"{stat_label_zh(name)} {value:+d}" for name, value in sorted(item.stats.items())
        )
        or "无基础属性"
    )
    draw.text(
        (355, 104), f"类别  {category}", font=_font(27, True), fill=_item_color(item.category)
    )
    draw.text((355, 148), stats, font=_font(28, True), fill=INK)
    draw.line((355, 194, 688, 194), fill=INVALID, width=3)
    descriptions = [registry.effect(effect.type).render_zh(effect) for effect in item.effects]
    if not descriptions:
        descriptions = ["无效果"]
    wrapped = [_wrapped_lines(description, 18) for description in descriptions]
    total_lines = sum(len(lines) for lines in wrapped)
    if total_lines <= 11:
        effect_font_size = 18
    else:
        effect_font_size = 15
        wrapped = [_wrapped_lines(description, 21) for description in descriptions]
    line_height = effect_font_size + 10
    y = 210
    for lines in wrapped:
        for line in lines:
            draw.text((355, y), line, font=_font(effect_font_size), fill=INK)
            y += line_height
        y += 5
    return image


def render_scenario_sheet_png(
    scenario: ScenarioSpec,
    registry: PluginRegistry,
    mode: str = "visual_full",
    sprites: dict[str, Image.Image] | None = None,
) -> Image.Image:
    cards = [
        render_item_card_png(
            item,
            registry,
            label=chr(ord("A") + index),
            count=item.count,
            local_id=item.id,
            detail=mode == "visual_full",
            sprite=(sprites or {}).get(getattr(item, "catalog_id", None) or item.id),
        )
        for index, item in enumerate(scenario.items)
    ]
    columns = 2
    card_gap = 24
    sheet_width = 1536
    card_width = 720
    card_height = 560
    rows = (len(cards) + columns - 1) // columns
    header_height = 470
    sheet_height = header_height + rows * (card_height + card_gap) + 40
    image = Image.new("RGBA", (sheet_width, sheet_height), PAPER)
    draw = ImageDraw.Draw(image)
    draw.text((48, 30), scenario.title, font=_font(44, True), fill=INK)
    draw.text(
        (48, 90),
        f"背包 {scenario.board.height} 行 × {scenario.board.width} 列 · 左上角 (0,0)",
        font=_font(27),
        fill=MUTED,
    )
    board_cell = min(62, 320 // max(scenario.board.width, scenario.board.height))
    board_left, board_top = 70, 150
    valid = scenario.board.valid_cells()
    for row in range(scenario.board.height):
        for col in range(scenario.board.width):
            x0, y0 = board_left + col * board_cell, board_top + row * board_cell
            rectangle = (x0, y0, x0 + board_cell, y0 + board_cell)
            if (row, col) in valid:
                draw.rectangle(rectangle, fill=PANEL, outline=INK, width=3)
                draw.text((x0 + 7, y0 + 5), f"{row},{col}", font=_font(17), fill=MUTED)
            else:
                draw.rectangle(rectangle, fill=INVALID, outline=GRID, width=2)
                draw.line(
                    (x0 + 7, y0 + 7, x0 + board_cell - 7, y0 + board_cell - 7), fill=GRID, width=4
                )
                draw.line(
                    (x0 + board_cell - 7, y0 + 7, x0 + 7, y0 + board_cell - 7), fill=GRID, width=4
                )
    draw.rounded_rectangle((480, 145, 1480, 405), 18, fill=PANEL, outline=GRID, width=3)
    legend = ["读图规则", "• 彩色格 = 物品占用格；灰色叉格 = 外接矩形中的空洞"]
    if mode == "visual_full":
        legend.extend(
            [
                "• 0° ↑ 是图片中的初始方向；形状和箭头随 rotation 一起顺时针旋转",
                "• 绿箭头表示增加攻击，红箭头表示降低攻击",
                "• 短箭头表示相邻效果，长箭头表示一直延伸到边界的射线效果",
            ]
        )
    else:
        legend.extend(
            [
                "• 本图只提供背包和物品占格形状",
                "• 类别、攻击、允许旋转和效果请读取文字提示",
            ]
        )
    y = 165
    for index, line in enumerate(legend):
        draw.text((510, y), line, font=_font(28 if index == 0 else 23, index == 0), fill=INK)
        y += 46
    for index, card in enumerate(cards):
        row, col = divmod(index, columns)
        x = 36 + col * (card_width + card_gap)
        y = header_height + row * (card_height + card_gap)
        image.alpha_composite(card, (x, y))
    return image.convert("RGB")


def render_placeholder_svg(item: ItemDefinitionSpec, cell_size: int = 128) -> str:
    height, width = shape_size(item.shape)
    digest = hashlib.sha256(item.id.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    saturation = 62 if item.category == "weapon" else 48
    lightness = 52 if item.category == "weapon" else 58
    accent_hue = (hue + 48 + digest[2] % 96) % 360
    inset = max(5, cell_size // 18)
    radius = max(8, cell_size // 10)
    elements: list[str] = []
    for index, (row, col) in enumerate(item.shape):
        x = col * cell_size
        y = row * cell_size
        variant = (digest[(index + 3) % len(digest)] % 18) - 9
        elements.extend(
            [
                (
                    f'<rect x="{x + inset}" y="{y + inset}" '
                    f'width="{cell_size - inset * 2}" height="{cell_size - inset * 2}" '
                    f'rx="{radius}" fill="hsl({hue} {saturation}% {lightness + variant / 3:.1f}%)" '
                    'stroke="rgba(20,24,22,.72)" stroke-width="4"/>'
                ),
                (
                    f'<path d="M {x + cell_size * 0.24:.1f} {y + cell_size * 0.70:.1f} '
                    f'L {x + cell_size * 0.70:.1f} {y + cell_size * 0.24:.1f}" '
                    f'stroke="hsl({accent_hue} 72% 78%)" stroke-width="10" '
                    'stroke-linecap="round" opacity=".72"/>'
                ),
                (
                    f'<circle cx="{x + cell_size / 2:.1f}" cy="{y + cell_size / 2:.1f}" '
                    f'r="{cell_size * 0.105:.1f}" fill="hsl({accent_hue} 78% 86%)" opacity=".9"/>'
                ),
            ]
        )
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width * cell_size}" '
                f'height="{height * cell_size}" viewBox="0 0 {width * cell_size} '
                f'{height * cell_size}">'
            ),
            *elements,
            "</svg>",
            "",
        ]
    )


def scaffold_visual_pack(
    catalog: ItemCatalogSpec,
    output_dir: Path,
    pack_id: str,
    version: str = "1.0.0",
    cell_size: int = 128,
) -> Path:
    output_dir = output_dir.resolve()
    item_dir = output_dir / "items"
    assets: list[VisualAssetSpec] = []
    for item in catalog.items:
        image_path = item_dir / f"{item.id}.svg"
        atomic_write_text(image_path, render_placeholder_svg(item, cell_size))
        assets.append(
            VisualAssetSpec(
                item_id=item.id,
                image=f"items/{item.id}.svg",
                sha256=file_sha256(image_path),
            )
        )
    pack = VisualPackSpec(
        id=pack_id,
        version=version,
        status="placeholder",
        item_catalog_hash=item_catalog_hash(catalog),
        renderer_id=PLACEHOLDER_RENDERER_ID,
        renderer_version=PLACEHOLDER_RENDERER_VERSION,
        cell_size=cell_size,
        assets=assets,
    )
    manifest_path = output_dir / "manifest.yaml"
    atomic_write_yaml(manifest_path, pack)
    return manifest_path


def render_card_visual_pack(
    catalog: ItemCatalogSpec,
    scenarios: list[tuple[ScenarioSpec, str]],
    registry: PluginRegistry,
    output_dir: Path,
    pack_id: str = "visual-card-v1",
    version: str = "1.0.0",
    cell_size: int = 128,
    art_sources: Path | None = None,
) -> Path:
    """Render catalog sprites/cards plus one frozen multimodal sheet per scenario."""
    output_dir = output_dir.resolve()
    assets: list[VisualAssetSpec] = []
    cards: list[VisualAssetSpec] = []
    scenario_sheets: list[ScenarioVisualAssetSpec] = []
    rendered_sprites: dict[str, Image.Image] = {}
    if art_sources is not None:
        art_sources = art_sources.resolve()
    for item in catalog.items:
        sprite_path = output_dir / "items" / f"{item.id}.png"
        card_path = output_dir / "cards" / f"{item.id}.png"
        if art_sources is None:
            sprite = render_item_sprite_png(item, cell_size)
        else:
            source_path = art_sources / f"{item.id}.png"
            if not source_path.is_file():
                raise ValueError(f"missing generated art source: {source_path}")
            with Image.open(source_path) as source:
                sprite = compose_art_sprite_png(item, source, cell_size)
        rendered_sprites[item.id] = sprite
        atomic_write_bytes(sprite_path, _png_bytes(sprite))
        atomic_write_bytes(
            card_path,
            _png_bytes(render_item_card_png(item, registry, sprite=sprite)),
        )
        assets.append(
            VisualAssetSpec(
                item_id=item.id,
                image=f"items/{item.id}.png",
                sha256=file_sha256(sprite_path),
            )
        )
        cards.append(
            VisualAssetSpec(
                item_id=item.id,
                image=f"cards/{item.id}.png",
                sha256=file_sha256(card_path),
            )
        )
    for scenario, current_hash in scenarios:
        for mode in ("visual_shape", "visual_full"):
            sheet_path = output_dir / "scenarios" / mode / f"{scenario.id}.png"
            atomic_write_bytes(
                sheet_path,
                _png_bytes(
                    render_scenario_sheet_png(
                        scenario,
                        registry,
                        mode,
                        rendered_sprites,
                    )
                ),
            )
            scenario_sheets.append(
                ScenarioVisualAssetSpec(
                    scenario_id=scenario.id,
                    scenario_hash=current_hash,
                    mode=mode,
                    image=f"scenarios/{mode}/{scenario.id}.png",
                    sha256=file_sha256(sheet_path),
                )
            )
    pack = VisualPackSpec(
        id=pack_id,
        version=version,
        status="final" if art_sources is not None else "placeholder",
        item_catalog_hash=item_catalog_hash(catalog),
        renderer_id=ART_RENDERER_ID if art_sources is not None else CARD_RENDERER_ID,
        renderer_version=(
            ART_RENDERER_VERSION if art_sources is not None else CARD_RENDERER_VERSION
        ),
        cell_size=cell_size,
        assets=assets,
        cards=cards,
        scenario_sheets=scenario_sheets,
    )
    manifest_path = output_dir / "manifest.yaml"
    atomic_write_yaml(manifest_path, pack)
    return manifest_path
