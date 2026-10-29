# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure geometry helpers for Draw/Impress shape alignment and diagram layout.

Coordinates are LibreOffice 1/100 mm. These functions do not touch UNO so they
can be unit-tested without a document.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Sequence


def coerce_int(value: Any, default: int) -> int:
    """Parse a tool arg as int, using ``default`` when missing/null."""
    if value is None:
        return default
    return int(value)


# Box: (x, y, width, height)
Box = tuple[int, int, int, int]

DEFAULT_PAGE_WIDTH = 28000
DEFAULT_PAGE_HEIGHT = 15750

ALIGNMENTS = ("left", "center_horizontal", "right", "top", "center_vertical", "bottom")
DISTRIBUTE_AXES = ("horizontal", "vertical")
DIAGRAM_LAYOUTS = ("horizontal_flow", "vertical_flow", "grid", "custom")

_AXIS = {"horizontal": (0, 2), "vertical": (1, 3)}


def align_boxes(boxes: Sequence[Box], alignment: str) -> list[Box]:
    """Return boxes with the same sizes, shifted onto a shared alignment axis."""
    if alignment not in ALIGNMENTS:
        raise ValueError(f"Unknown alignment: {alignment}")
    if len(boxes) < 2:
        raise ValueError("Need at least two shapes to align")

    if alignment == "left":
        edge = min(b[0] for b in boxes)
        return [(edge, b[1], b[2], b[3]) for b in boxes]
    if alignment == "right":
        edge = max(b[0] + b[2] for b in boxes)
        return [(edge - b[2], b[1], b[2], b[3]) for b in boxes]
    if alignment == "center_horizontal":
        centers = [b[0] + b[2] // 2 for b in boxes]
        anchor = int(sum(centers) / len(centers))
        return [(anchor - b[2] // 2, b[1], b[2], b[3]) for b in boxes]
    if alignment == "top":
        edge = min(b[1] for b in boxes)
        return [(b[0], edge, b[2], b[3]) for b in boxes]
    if alignment == "bottom":
        edge = max(b[1] + b[3] for b in boxes)
        return [(b[0], edge - b[3], b[2], b[3]) for b in boxes]
    centers = [b[1] + b[3] // 2 for b in boxes]
    anchor = int(sum(centers) / len(centers))
    return [(b[0], anchor - b[3] // 2, b[2], b[3]) for b in boxes]


def distribute_boxes(boxes: Sequence[Box], axis: str) -> list[Box]:
    """Evenly space boxes between the first and last along ``axis``.

    Order is left-to-right (horizontal) or top-to-bottom (vertical). The first
    and last boxes keep their positions; interiors are spaced so gaps between
    consecutive bounding boxes are equal.
    """
    if axis not in DISTRIBUTE_AXES:
        raise ValueError(f"Unknown distribute axis: {axis}")
    if len(boxes) < 3:
        raise ValueError("Need at least three shapes to distribute")

    start_idx, extent_idx = _AXIS[axis]
    indexed = list(enumerate(boxes))
    indexed.sort(key=lambda item: (item[1][start_idx], item[0]))
    first = indexed[0][1]
    last = indexed[-1][1]
    span_start = first[start_idx]
    span_end = max(b[start_idx] + b[extent_idx] for _, b in indexed)
    gap = (span_end - span_start - sum(b[extent_idx] for _, b in indexed)) / (len(indexed) - 1)
    cursor = float(span_start)
    placed: dict[int, Box] = {}
    for i, (orig_i, box) in enumerate(indexed):
        extent = box[extent_idx]
        if i == 0:
            pos = int(span_start)
            cursor = span_start + extent + gap
        elif i == len(indexed) - 1:
            pos = last[start_idx]
        else:
            pos = int(round(cursor))
            cursor += extent + gap
        placed[orig_i] = (pos, box[1], box[2], box[3]) if start_idx == 0 else (box[0], pos, box[2], box[3])
    return [placed[i] for i in range(len(boxes))]


def _flow_boxes(
    sizes: Sequence[tuple[int, int]],
    vertical: bool,
    page_width: int,
    page_height: int,
    margin: int,
    gap: int,
) -> list[Box]:
    pos = margin
    boxes: list[Box] = []
    if vertical:
        cross0 = max(margin, (page_width - max(w for w, _ in sizes)) // 2)
        for w, h in sizes:
            boxes.append((cross0, pos, w, h))
            pos += h + gap
    else:
        cross0 = max(margin, (page_height - max(h for _, h in sizes)) // 2)
        for w, h in sizes:
            boxes.append((pos, cross0, w, h))
            pos += w + gap
    return boxes


def diagram_node_boxes(
    nodes: Sequence[dict[str, Any]],
    layout: str,
    page_width: int = DEFAULT_PAGE_WIDTH,
    page_height: int = DEFAULT_PAGE_HEIGHT,
    default_width: int = 4000,
    default_height: int = 2000,
    margin: int = 1500,
    gap: int = 800,
) -> list[Box]:
    """Compute node bounding boxes for a batch diagram layout.

    ``custom`` uses each node's ``x``/``y`` (required). Other layouts ignore
    per-node coordinates except explicit width/height.
    """
    n = len(nodes)
    if n == 0:
        return []

    sizes = [(int(node.get("width") or default_width), int(node.get("height") or default_height)) for node in nodes]

    if layout == "custom":
        boxes = []
        for node, (w, h) in zip(nodes, sizes):
            if "x" not in node or "y" not in node:
                raise ValueError("custom layout requires x and y on every node")
            boxes.append((int(node["x"]), int(node["y"]), w, h))
        return boxes

    if layout == "grid":
        cols = max(1, int(ceil(n**0.5)))
        cell_w = max(w for w, _ in sizes)
        cell_h = max(h for _, h in sizes)
        boxes = []
        for i, (w, h) in enumerate(sizes):
            col = i % cols
            row = i // cols
            x = margin + col * (cell_w + gap)
            y = margin + row * (cell_h + gap)
            boxes.append((x, y, w, h))
        return boxes

    return _flow_boxes(sizes, vertical=(layout == "vertical_flow"), page_width=page_width, page_height=page_height, margin=margin, gap=gap)
