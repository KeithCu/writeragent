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

_ALIGNMENTS = frozenset(
    {"left", "center_horizontal", "right", "top", "center_vertical", "bottom"}
)


def align_boxes(boxes: Sequence[Box], alignment: str) -> list[Box]:
    """Return boxes with the same sizes, shifted onto a shared alignment axis."""
    if alignment not in _ALIGNMENTS:
        raise ValueError(f"Unknown alignment: {alignment}")
    if len(boxes) < 2:
        raise ValueError("Need at least two shapes to align")

    xs = [b[0] for b in boxes]
    ys = [b[1] for b in boxes]
    rights = [b[0] + b[2] for b in boxes]
    bottoms = [b[1] + b[3] for b in boxes]
    centers_x = [b[0] + b[2] // 2 for b in boxes]
    centers_y = [b[1] + b[3] // 2 for b in boxes]

    out: list[Box] = []
    for x, y, w, h in boxes:
        if alignment == "left":
            x = min(xs)
        elif alignment == "right":
            x = max(rights) - w
        elif alignment == "center_horizontal":
            x = int(sum(centers_x) / len(centers_x)) - w // 2
        elif alignment == "top":
            y = min(ys)
        elif alignment == "bottom":
            y = max(bottoms) - h
        elif alignment == "center_vertical":
            y = int(sum(centers_y) / len(centers_y)) - h // 2
        out.append((x, y, w, h))
    return out


def distribute_boxes(boxes: Sequence[Box], axis: str) -> list[Box]:
    """Evenly space boxes between the first and last along ``axis``.

    Order is left-to-right (horizontal) or top-to-bottom (vertical). The first
    and last boxes keep their positions; interiors are spaced so gaps between
    consecutive bounding boxes are equal.
    """
    if axis not in ("horizontal", "vertical"):
        raise ValueError(f"Unknown distribute axis: {axis}")
    if len(boxes) < 3:
        raise ValueError("Need at least three shapes to distribute")

    indexed = list(enumerate(boxes))
    if axis == "horizontal":
        indexed.sort(key=lambda item: (item[1][0], item[0]))
        first = indexed[0][1]
        last = indexed[-1][1]
        span_start = first[0]
        span_end = last[0] + last[2]
        total_w = sum(b[2] for _, b in indexed)
        gap = (span_end - span_start - total_w) / (len(indexed) - 1)
        cursor = float(span_start)
        placed: dict[int, Box] = {}
        for i, (orig_i, (x, y, w, h)) in enumerate(indexed):
            if i == 0:
                placed[orig_i] = (int(span_start), y, w, h)
                cursor = span_start + w + gap
            elif i == len(indexed) - 1:
                placed[orig_i] = (last[0], y, w, h)
            else:
                placed[orig_i] = (int(round(cursor)), y, w, h)
                cursor += w + gap
        return [placed[i] for i in range(len(boxes))]

    indexed.sort(key=lambda item: (item[1][1], item[0]))
    first = indexed[0][1]
    last = indexed[-1][1]
    span_start = first[1]
    span_end = last[1] + last[3]
    total_h = sum(b[3] for _, b in indexed)
    gap = (span_end - span_start - total_h) / (len(indexed) - 1)
    cursor = float(span_start)
    placed = {}
    for i, (orig_i, (x, y, w, h)) in enumerate(indexed):
        if i == 0:
            placed[orig_i] = (x, int(span_start), w, h)
            cursor = span_start + h + gap
        elif i == len(indexed) - 1:
            placed[orig_i] = (x, last[1], w, h)
        else:
            placed[orig_i] = (x, int(round(cursor)), w, h)
            cursor += h + gap
    return [placed[i] for i in range(len(boxes))]


def diagram_node_boxes(
    nodes: Sequence[dict[str, Any]],
    layout: str,
    page_width: int = 28000,
    page_height: int = 15750,
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

    def size_of(node: dict[str, Any]) -> tuple[int, int]:
        w = int(node.get("width") or default_width)
        h = int(node.get("height") or default_height)
        return w, h

    if layout == "custom":
        boxes = []
        for node in nodes:
            if "x" not in node or "y" not in node:
                raise ValueError("custom layout requires x and y on every node")
            w, h = size_of(node)
            boxes.append((int(node["x"]), int(node["y"]), w, h))
        return boxes

    if layout == "vertical_flow":
        boxes = []
        y = margin
        max_w = max(size_of(node)[0] for node in nodes)
        x0 = max(margin, (page_width - max_w) // 2)
        for node in nodes:
            w, h = size_of(node)
            boxes.append((x0, y, w, h))
            y += h + gap
        return boxes

    if layout == "grid":
        cols = max(1, int(ceil(n**0.5)))
        cell_w = max(size_of(node)[0] for node in nodes)
        cell_h = max(size_of(node)[1] for node in nodes)
        boxes = []
        for i, node in enumerate(nodes):
            w, h = size_of(node)
            col = i % cols
            row = i // cols
            x = margin + col * (cell_w + gap)
            y = margin + row * (cell_h + gap)
            boxes.append((x, y, w, h))
        return boxes

    # horizontal_flow (default)
    boxes = []
    x = margin
    max_h = max(size_of(node)[1] for node in nodes)
    y0 = max(margin, (page_height - max_h) // 2)
    for node in nodes:
        w, h = size_of(node)
        boxes.append((x, y0, w, h))
        x += w + gap
    return boxes
