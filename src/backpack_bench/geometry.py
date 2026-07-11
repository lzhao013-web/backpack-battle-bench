"""Grid geometry shared by validation, effects and the exact solver."""

from __future__ import annotations

from collections.abc import Iterable

Cell = tuple[int, int]


def rotate_vector(vector: Cell, rotation: int) -> Cell:
    row, col = vector
    for _ in range(rotation // 90):
        row, col = col, -row
    return row, col


def rotated_shape(shape: Iterable[Cell], rotation: int) -> frozenset[Cell]:
    rotated = [rotate_vector(cell, rotation) for cell in shape]
    min_row = min(row for row, _ in rotated)
    min_col = min(col for _, col in rotated)
    return frozenset((row - min_row, col - min_col) for row, col in rotated)


def occupied_cells(shape: Iterable[Cell], row: int, col: int, rotation: int) -> frozenset[Cell]:
    return frozenset(
        (row + delta_row, col + delta_col)
        for delta_row, delta_col in rotated_shape(shape, rotation)
    )


def shape_size(shape: Iterable[Cell], rotation: int = 0) -> tuple[int, int]:
    cells = rotated_shape(shape, rotation)
    return max(row for row, _ in cells) + 1, max(col for _, col in cells) + 1
