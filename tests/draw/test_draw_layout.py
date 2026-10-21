# WriterAgent — unit tests for Draw/Impress layout helpers
# SPDX-License-Identifier: GPL-3.0-or-later

import pytest

from plugin.draw.layout import (
    ALIGNMENTS,
    DEFAULT_PAGE_HEIGHT,
    DEFAULT_PAGE_WIDTH,
    DIAGRAM_LAYOUTS,
    DISTRIBUTE_AXES,
    align_boxes,
    diagram_node_boxes,
    distribute_boxes,
)


def test_layout_constants():
    assert ALIGNMENTS == ("left", "center_horizontal", "right", "top", "center_vertical", "bottom")
    assert DISTRIBUTE_AXES == ("horizontal", "vertical")
    assert DIAGRAM_LAYOUTS == ("horizontal_flow", "vertical_flow", "grid", "custom")
    assert DEFAULT_PAGE_WIDTH == 28000
    assert DEFAULT_PAGE_HEIGHT == 15750


def test_align_left():
    boxes = [(100, 10, 50, 20), (200, 40, 50, 20)]
    out = align_boxes(boxes, "left")
    assert out[0][0] == 100
    assert out[1][0] == 100
    assert out[0][1] == 10 and out[1][1] == 40


def test_align_right():
    boxes = [(100, 10, 50, 20), (200, 40, 50, 20)]
    out = align_boxes(boxes, "right")
    assert out[0][0] == 200
    assert out[1][0] == 200


def test_align_center_horizontal():
    boxes = [(0, 0, 100, 10), (100, 0, 100, 10)]
    out = align_boxes(boxes, "center_horizontal")
    # centers at 50 and 150, average 100 → first x=50, second x=50
    assert out[0][0] == 50
    assert out[1][0] == 50


def test_align_top():
    boxes = [(10, 100, 20, 50), (40, 200, 20, 50)]
    out = align_boxes(boxes, "top")
    assert out[0][1] == 100
    assert out[1][1] == 100


def test_align_bottom():
    boxes = [(10, 100, 20, 50), (40, 200, 20, 50)]
    out = align_boxes(boxes, "bottom")
    assert out[0][1] == 200
    assert out[1][1] == 200


def test_align_center_vertical():
    boxes = [(0, 0, 10, 100), (0, 100, 10, 100)]
    out = align_boxes(boxes, "center_vertical")
    # centers_y at 50 and 150, average 100 → first y=50, second y=50
    assert out[0][1] == 50
    assert out[1][1] == 50


def test_align_requires_two():
    with pytest.raises(ValueError):
        align_boxes([(0, 0, 10, 10)], "left")


def test_distribute_horizontal():
    boxes = [(0, 5, 10, 10), (50, 5, 10, 10), (200, 5, 10, 10)]
    out = distribute_boxes(boxes, "horizontal")
    assert out[0][0] == 0
    assert out[2][0] == 200
    assert out[1][0] == 100


def test_distribute_vertical():
    boxes = [(5, 0, 10, 10), (5, 50, 10, 10), (5, 200, 10, 10)]
    out = distribute_boxes(boxes, "vertical")
    assert out[0][1] == 0
    assert out[2][1] == 200
    assert out[1][1] == 100


def test_distribute_requires_three():
    with pytest.raises(ValueError):
        distribute_boxes([(0, 0, 10, 10), (20, 0, 10, 10)], "horizontal")


def test_diagram_horizontal_flow():
    nodes = [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]
    boxes = diagram_node_boxes(nodes, "horizontal_flow", page_width=28000, page_height=10000)
    assert boxes[0][0] < boxes[1][0]
    assert boxes[0][2] == 4000


def test_diagram_vertical_flow():
    nodes = [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]
    boxes = diagram_node_boxes(nodes, "vertical_flow", page_width=10000, page_height=28000)
    assert boxes[0][1] < boxes[1][1]
    assert boxes[0][3] == 2000
    assert boxes[0][0] == 3000
    assert boxes[1][0] == 3000


def test_diagram_grid():
    nodes = [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]
    boxes = diagram_node_boxes(nodes, "grid", margin=1500, gap=800)
    assert len(boxes) == 2
    assert boxes[0] == (1500, 1500, 4000, 2000)
    assert boxes[1] == (1500 + 4000 + 800, 1500, 4000, 2000)


def test_diagram_unknown_layout_defaults_to_horizontal_flow():
    nodes = [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]
    h_flow = diagram_node_boxes(nodes, "horizontal_flow")
    assert diagram_node_boxes(nodes, "nope") == h_flow
    assert diagram_node_boxes(nodes, "") == h_flow


def test_diagram_custom_requires_xy():
    with pytest.raises(ValueError):
        diagram_node_boxes([{"id": "a", "text": "A"}], "custom")


def test_diagram_custom():
    nodes = [{"id": "a", "text": "A", "x": 10, "y": 20, "width": 30, "height": 40}]
    assert diagram_node_boxes(nodes, "custom") == [(10, 20, 30, 40)]
