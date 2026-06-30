# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Serializable shape operations produced from SVG (ppt-master adapter pipeline).

UPSTREAM NOTE (WriterAgent addition — not in upstream):
  Upstream emits DrawingML XML inside the venv install:
  ``<PPT_MASTER_DATA_ROOT>/scripts/svg_to_pptx/drawingml_converter.py``.
  ShapeOp / SlideBuildPlan are the UNO interchange format for Impress/Draw.

  # Upstream PPTX path (venv scripts — commented):
  #   from svg_to_pptx.pptx_builder import create_pptx_with_native_svg
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ShapeOp:
    """One native shape to create on an Impress/Draw page."""

    kind: str  # rect, ellipse, line, text, image, path, group
    x_hmm: int
    y_hmm: int
    w_hmm: int
    h_hmm: int
    fill_color: int | None = None
    line_color: int | None = None
    line_width_hmm: int | None = None
    text: str | None = None
    font_size_pt: float | None = None
    font_family: str | None = None
    image_path: str | None = None
    path_points: list[tuple[int, int]] | None = None
    children: list[ShapeOp] | None = None
    rotation_deg: float = 0.0
    opacity: float | None = None


@dataclass
class SlideBuildPlan:
    """One slide: dimensions + shape ops (+ optional notes/transition metadata)."""

    slide_index: int
    viewbox_width_px: float
    viewbox_height_px: float
    slide_width_hmm: int
    slide_height_hmm: int
    shapes: list[ShapeOp] = field(default_factory=list)
    notes_text: str | None = None
    transition: dict[str, Any] | None = None
    svg_source: str | None = None


def shape_op_to_dict(op: ShapeOp) -> dict[str, Any]:
    d = asdict(op)
    if op.children:
        d["children"] = [shape_op_to_dict(c) for c in op.children]
    return d


def shape_op_from_dict(data: dict[str, Any]) -> ShapeOp:
    children_raw = data.get("children")
    children = None
    if isinstance(children_raw, list):
        children = [shape_op_from_dict(c) for c in children_raw if isinstance(c, dict)]
    return ShapeOp(
        kind=str(data.get("kind", "rect")),
        x_hmm=int(data.get("x_hmm", 0)),
        y_hmm=int(data.get("y_hmm", 0)),
        w_hmm=int(data.get("w_hmm", 0)),
        h_hmm=int(data.get("h_hmm", 0)),
        fill_color=data.get("fill_color"),
        line_color=data.get("line_color"),
        line_width_hmm=data.get("line_width_hmm"),
        text=data.get("text"),
        font_size_pt=data.get("font_size_pt"),
        font_family=data.get("font_family"),
        image_path=data.get("image_path"),
        path_points=data.get("path_points"),
        children=children,
        rotation_deg=float(data.get("rotation_deg", 0.0)),
        opacity=data.get("opacity"),
    )


def slide_plan_to_dict(plan: SlideBuildPlan) -> dict[str, Any]:
    return {
        "slide_index": plan.slide_index,
        "viewbox_width_px": plan.viewbox_width_px,
        "viewbox_height_px": plan.viewbox_height_px,
        "slide_width_hmm": plan.slide_width_hmm,
        "slide_height_hmm": plan.slide_height_hmm,
        "shapes": [shape_op_to_dict(s) for s in plan.shapes],
        "notes_text": plan.notes_text,
        "transition": plan.transition,
        "svg_source": plan.svg_source,
    }


def slide_plan_from_dict(data: dict[str, Any]) -> SlideBuildPlan:
    shapes_raw = data.get("shapes") or []
    shapes = [shape_op_from_dict(s) for s in shapes_raw if isinstance(s, dict)]
    return SlideBuildPlan(
        slide_index=int(data.get("slide_index", 0)),
        viewbox_width_px=float(data.get("viewbox_width_px", 1280)),
        viewbox_height_px=float(data.get("viewbox_height_px", 720)),
        slide_width_hmm=int(data.get("slide_width_hmm", 25400)),
        slide_height_hmm=int(data.get("slide_height_hmm", 14288)),
        shapes=shapes,
        notes_text=data.get("notes_text"),
        transition=data.get("transition") if isinstance(data.get("transition"), dict) else None,
        svg_source=data.get("svg_source"),
    )
