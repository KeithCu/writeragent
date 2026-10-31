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


# Box: (x, y, width, height)
Box = tuple[int, int, int, int]

DEFAULT_PAGE_WIDTH = 28000
DEFAULT_PAGE_HEIGHT = 15750

ALIGNMENTS = ("left", "center_horizontal", "right", "top", "center_vertical", "bottom")
DISTRIBUTE_AXES = ("horizontal", "vertical")
DIAGRAM_LAYOUTS = ("horizontal_flow", "vertical_flow", "grid", "custom")

_AXIS = {"horizontal": (0, 2), "vertical": (1, 3)}


def _with_start(box: Box, start_idx: int, pos: int) -> Box:
    if start_idx == 0:
        return (pos, box[1], box[2], box[3])
    return (box[0], pos, box[2], box[3])


def align_boxes(boxes: Sequence[Box], alignment: str) -> list[Box]:
    """Return boxes with the same sizes, shifted onto a shared alignment axis."""
    if alignment not in ALIGNMENTS:
        raise ValueError(f"Unknown alignment: {alignment}")
    if len(boxes) < 2:
        raise ValueError("Need at least two shapes to align")

    start, extent = (0, 2) if alignment in ("left", "right", "center_horizontal") else (1, 3)
    if alignment in ("left", "top"):
        edge = min(b[start] for b in boxes)
        return [_with_start(b, start, edge) for b in boxes]
    if alignment in ("right", "bottom"):
        edge = max(b[start] + b[extent] for b in boxes)
        return [_with_start(b, start, edge - b[extent]) for b in boxes]
    centers = [b[start] + b[extent] // 2 for b in boxes]
    anchor = int(sum(centers) / len(centers))
    return [_with_start(b, start, anchor - b[extent] // 2) for b in boxes]


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
    first_i, first = indexed[0]
    last_i, last = indexed[-1]
    span_start = first[start_idx]
    span_end = max(b[start_idx] + b[extent_idx] for _, b in indexed)
    gap = (span_end - span_start - sum(b[extent_idx] for _, b in indexed)) / (len(indexed) - 1)
    placed: dict[int, Box] = {
        first_i: _with_start(first, start_idx, int(span_start)),
        last_i: _with_start(last, start_idx, last[start_idx]),
    }
    cursor = span_start + first[extent_idx] + gap
    for orig_i, box in indexed[1:-1]:
        placed[orig_i] = _with_start(box, start_idx, int(round(cursor)))
        cursor += box[extent_idx] + gap
    return [placed[i] for i in range(len(boxes))]


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
        if any("x" not in node or "y" not in node for node in nodes):
            raise ValueError("custom layout requires x and y on every node")
        return [(int(node["x"]), int(node["y"]), w, h) for node, (w, h) in zip(nodes, sizes)]

    if layout == "grid":
        cols = max(1, int(ceil(n**0.5)))
        cell_w = max(w for w, _ in sizes)
        cell_h = max(h for _, h in sizes)
        return [
            (margin + (i % cols) * (cell_w + gap), margin + (i // cols) * (cell_h + gap), w, h)
            for i, (w, h) in enumerate(sizes)
        ]

    pos = margin
    boxes: list[Box] = []
    if layout == "vertical_flow":
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
