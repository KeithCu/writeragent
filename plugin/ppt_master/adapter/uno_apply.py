# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply ShapeOp lists to Impress/Draw via UNO."""

from __future__ import annotations

import logging
from typing import Any

from plugin.contrib.ppt_master.shape_ops import ShapeOp, SlideBuildPlan
from plugin.draw.bridge import DrawBridge

log = logging.getLogger(__name__)

_RECT = "com.sun.star.drawing.RectangleShape"
_ELLIPSE = "com.sun.star.drawing.EllipseShape"
_LINE = "com.sun.star.drawing.LineShape"
_TEXT = "com.sun.star.drawing.TextShape"
_IMAGE = "com.sun.star.drawing.GraphicObjectShape"


def _file_url(path: str) -> str:
    from pathlib import Path

    return Path(path).resolve().as_uri()


def _apply_fill_line(shape, op: ShapeOp) -> None:
    try:
        if op.fill_color is not None:
            from com.sun.star.drawing import FillStyle

            shape.setPropertyValue("FillStyle", FillStyle.SOLID)
            shape.setPropertyValue("FillColor", int(op.fill_color))
    except Exception as exc:
        log.debug("fill: %s", exc)
    try:
        if op.line_color is not None:
            shape.setPropertyValue("LineColor", int(op.line_color))
        if op.line_width_hmm is not None:
            shape.setPropertyValue("LineWidth", int(op.line_width_hmm))
    except Exception as exc:
        log.debug("line: %s", exc)


def apply_shape_op(bridge: DrawBridge, page, op: ShapeOp) -> Any | None:
    """Create one shape on *page* from *op*."""
    kind = op.kind
    if kind == "rect":
        shape = bridge.create_shape(_RECT, op.x_hmm, op.y_hmm, op.w_hmm, op.h_hmm, page=page)
        _apply_fill_line(shape, op)
        return shape
    if kind == "ellipse":
        shape = bridge.create_shape(_ELLIPSE, op.x_hmm, op.y_hmm, op.w_hmm, op.h_hmm, page=page)
        _apply_fill_line(shape, op)
        return shape
    if kind == "line":
        shape = bridge.create_shape(_LINE, op.x_hmm, op.y_hmm, op.w_hmm, op.h_hmm, page=page)
        _apply_fill_line(shape, op)
        return shape
    if kind == "text":
        shape = bridge.create_shape(_TEXT, op.x_hmm, op.y_hmm, max(op.w_hmm, 500), max(op.h_hmm, 500), page=page)
        if op.text and hasattr(shape, "setString"):
            shape.setString(op.text)
        elif op.text and hasattr(shape, "getText"):
            shape.getText().setString(op.text)
        _apply_fill_line(shape, op)
        return shape
    if kind == "image" and op.image_path:
        shape = bridge.create_shape(_IMAGE, op.x_hmm, op.y_hmm, op.w_hmm, op.h_hmm, page=page)
        try:
            shape.setPropertyValue("GraphicURL", _file_url(op.image_path))
        except Exception as exc:
            log.warning("GraphicURL failed for %s: %s", op.image_path, exc)
        return shape
    if kind == "path" and op.path_points and len(op.path_points) >= 2:
        shape = bridge.create_shape(_LINE, op.x_hmm, op.y_hmm, op.w_hmm, op.h_hmm, page=page)
        _apply_fill_line(shape, op)
        return shape
    if kind == "group" and op.children:
        for child in op.children:
            apply_shape_op(bridge, page, child)
        return None
    return None


def apply_slide_plan(doc: Any, plan: SlideBuildPlan, *, page_index: int | None = None) -> dict[str, Any]:
    bridge = DrawBridge(doc)
    pages = bridge.get_pages()
    idx = plan.slide_index if page_index is None else page_index
    while pages.getCount() <= idx:
        bridge.create_slide(pages.getCount(), switch=False)
    page = pages.getByIndex(idx)
    bridge.set_current_page_index(idx)
    applied = 0
    for op in plan.shapes:
        if apply_shape_op(bridge, page, op) is not None:
            applied += 1
    if plan.notes_text and doc.supportsService("com.sun.star.presentation.PresentationDocument"):
        try:
            notes_page = page.getNotesPage()
            for i in range(notes_page.getCount()):
                shape = notes_page.getByIndex(i)
                if hasattr(shape, "setString"):
                    shape.setString(plan.notes_text)
                    break
        except Exception as exc:
            log.debug("notes: %s", exc)
    return {"status": "ok", "slide_index": idx, "shapes_applied": applied}


def apply_slide_plans(doc: Any, plans: list[SlideBuildPlan]) -> dict[str, Any]:
    results = []
    for plan in plans:
        results.append(apply_slide_plan(doc, plan))
    return {"status": "ok", "slides": len(results), "results": results}
