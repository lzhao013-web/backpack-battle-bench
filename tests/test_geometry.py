from backpack_bench.geometry import occupied_cells, rotate_vector, rotated_shape


def test_clockwise_rotation() -> None:
    assert rotate_vector((-1, 0), 90) == (0, 1)
    assert rotate_vector((0, -1), 90) == (-1, 0)
    assert rotated_shape([(0, 0), (0, 1), (0, 2)], 90) == frozenset({(0, 0), (1, 0), (2, 0)})


def test_anchor_is_rotated_bounding_box_top_left() -> None:
    assert occupied_cells([(0, 0), (0, 1)], 1, 2, 90) == frozenset({(1, 2), (2, 2)})
