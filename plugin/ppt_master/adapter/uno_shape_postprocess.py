# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Post-import UNO tweaks on shapes copied from draw_svg_import."""

from __future__ import annotations

import logging
from typing import Any

from plugin.contrib.ppt_master.coords import DEFAULT_SLIDE_HEIGHT_HMM, DEFAULT_SLIDE_WIDTH_HMM

log = logging.getLogger(__name__)

# Properties safe to copy between shapes (skip refs to source doc objects).
_COPY_PROPS = (
    "FillStyle",
    "FillColor",
    "FillTransparence",
    "LineStyle",
    "LineColor",
    "LineWidth",
    "LineTransparence",
    "RotateAngle",
    "PolyPolygon",
    "Polygon",
    "CustomShapeGeometry",
    "CharHeight",
    "CharFontName",
    "CharColor",
    "GraphicURL",
    "Graphic",
)


def _page_size_hmm(page: Any) -> tuple[int, int]:
    try:
        w = int(page.getPropertyValue("Width"))
        h = int(page.getPropertyValue("Height"))
        if w > 0 and h > 0:
            return w, h
    except Exception as exc:
        log.debug("page size: %s", exc)
    return DEFAULT_SLIDE_WIDTH_HMM, DEFAULT_SLIDE_HEIGHT_HMM


def _shape_bbox(shape: Any) -> tuple[int, int, int, int] | None:
    try:
        pos = shape.getPosition()
        size = shape.getSize()
        return int(pos.X), int(pos.Y), int(size.Width), int(size.Height)
    except Exception:
        return None


def _scale_shape(shape: Any, scale_x: float, scale_y: float) -> None:
    if scale_x == 1.0 and scale_y == 1.0:
        return
    try:
        pos = shape.getPosition()
        size = shape.getSize()
        from com.sun.star.awt import Point, Size

        shape.setPosition(Point(int(pos.X * scale_x), int(pos.Y * scale_y)))
        shape.setSize(Size(max(1, int(size.Width * scale_x)), max(1, int(size.Height * scale_y))))
    except Exception as exc:
        log.debug("scale shape: %s", exc)


def _copy_property(source: Any, dest: Any, name: str) -> None:
    try:
        dest.setPropertyValue(name, source.getPropertyValue(name))
    except Exception as exc:
        log.debug("copy prop %s: %s", name, exc)


def clone_shape_to_page(source_shape: Any, target_doc: Any, target_page: Any) -> Any | None:
    """Clone one shape from a temp import doc onto *target_page* in *target_doc*."""
    try:
        shape_type = source_shape.getShapeType()
        new_shape = target_doc.createInstance(shape_type)
        if new_shape is None:
            return None
        target_page.add(new_shape)
        if hasattr(source_shape, "getPosition") and hasattr(new_shape, "setPosition"):
            new_shape.setPosition(source_shape.getPosition())
        if hasattr(source_shape, "getSize") and hasattr(new_shape, "setSize"):
            new_shape.setSize(source_shape.getSize())
        for prop in _COPY_PROPS:
            if hasattr(source_shape, "getPropertyValue") and hasattr(new_shape, "setPropertyValue"):
                _copy_property(source_shape, new_shape, prop)
        if hasattr(source_shape, "getString") and hasattr(new_shape, "setString"):
            try:
                new_shape.setString(source_shape.getString())
            except Exception as exc:
                log.debug("copy string: %s", exc)
        elif hasattr(source_shape, "getText") and hasattr(new_shape, "getText"):
            try:
                new_shape.getText().setString(source_shape.getText().getString())
            except Exception as exc:
                log.debug("copy text: %s", exc)
        return new_shape
    except Exception as exc:
        log.warning("clone_shape_to_page failed: %s", exc)
        return None


def copy_shapes_to_page(source_page: Any, target_doc: Any, target_page: Any) -> int:
    """Copy all shapes from *source_page* to *target_page*; return count copied."""
    copied = 0
    for i in range(source_page.getCount()):
        if clone_shape_to_page(source_page.getByIndex(i), target_doc, target_page) is not None:
            copied += 1
    return copied


def clear_page_shapes(page: Any) -> None:
    """Remove all shapes from a draw page (for re-export)."""
    while page.getCount() > 0:
        try:
            page.remove(page.getByIndex(page.getCount() - 1))
        except Exception as exc:
            log.debug("clear_page_shapes: %s", exc)
            break


def postprocess_slide_shapes(
    page: Any,
    *,
    source_page_width_hmm: int | None = None,
    source_page_height_hmm: int | None = None,
    target_width_hmm: int = DEFAULT_SLIDE_WIDTH_HMM,
    target_height_hmm: int = DEFAULT_SLIDE_HEIGHT_HMM,
) -> dict[str, Any]:
    """Scale and normalize shapes after import copy."""
    src_w = source_page_width_hmm or target_width_hmm
    src_h = source_page_height_hmm or target_height_hmm
    scale_x = target_width_hmm / src_w if src_w > 0 else 1.0
    scale_y = target_height_hmm / src_h if src_h > 0 else 1.0
    adjusted = 0
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        _scale_shape(shape, scale_x, scale_y)
        adjusted += 1
        # Text shapes sometimes import with CharHeight 0; nudge if empty.
        try:
            if "TextShape" in shape.getShapeType() and hasattr(shape, "getCharHeight"):
                if float(shape.getCharHeight()) <= 0:
                    shape.setCharHeight(14.0)
        except Exception as exc:
            log.debug("text postprocess: %s", exc)
    return {"shapes_adjusted": adjusted, "scale_x": scale_x, "scale_y": scale_y}
