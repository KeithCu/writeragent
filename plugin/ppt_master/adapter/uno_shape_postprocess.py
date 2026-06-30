# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clone Impress/Draw shapes between pages (used by PPTX import)."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

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
    "PolyPolygonBezier",
    "Geometry",
    "CustomShapeGeometry",
    "GraphicURL",
    "Graphic",
)

_TEXT_LAYOUT_PROPS = (
    "TextAutoGrowHeight",
    "TextVerticalAdjust",
    "TextHorizontalAdjust",
    "TextFitToSize",
)

_TEXT_CHAR_PROPS = (
    "CharHeight",
    "CharFontName",
    "CharWeight",
    "CharPosture",
    "CharColor",
    "CharUnderline",
    "CharStrikeout",
    "CharEscapement",
    "CharKerning",
    "CharLetterSpacing",
)


def _copy_property(source: Any, dest: Any, name: str) -> None:
    try:
        dest.setPropertyValue(name, source.getPropertyValue(name))
    except Exception as exc:
        log.debug("copy prop %s: %s", name, exc)


def _copy_text_portion_props(source_portion: Any, dest_portion: Any) -> None:
    for prop in _TEXT_CHAR_PROPS:
        try:
            dest_portion.setPropertyValue(prop, source_portion.getPropertyValue(prop))
        except Exception as exc:
            log.debug("copy text prop %s: %s", prop, exc)


def _copy_shape_text(source_shape: Any, dest_shape: Any) -> None:
    """Copy draw/impress text with character formatting."""
    if hasattr(source_shape, "getText") and hasattr(dest_shape, "getText"):
        src_text = source_shape.getText()
        dst_text = dest_shape.getText()
        dst_text.setString(src_text.getString())
        for prop in _TEXT_CHAR_PROPS:
            try:
                dest_shape.setPropertyValue(prop, source_shape.getPropertyValue(prop))
            except Exception as exc:
                log.debug("copy shape text prop %s: %s", prop, exc)
        src_portions = []
        src_enum = src_text.createEnumeration()
        while src_enum.hasMoreElements():
            src_portions.append(src_enum.nextElement())
        dst_enum = dst_text.createEnumeration()
        dst_portions = []
        while dst_enum.hasMoreElements():
            dst_portions.append(dst_enum.nextElement())
        fallback = src_portions[-1] if src_portions else None
        for idx, dst_portion in enumerate(dst_portions):
            src_portion = src_portions[idx] if idx < len(src_portions) else fallback
            if src_portion is not None:
                _copy_text_portion_props(src_portion, dst_portion)
        return
    if hasattr(source_shape, "getString") and hasattr(dest_shape, "setString"):
        dest_shape.setString(source_shape.getString())


def clone_shape_to_page(source_shape: Any, target_doc: Any, target_page: Any) -> Any | None:
    """Clone one shape from a source page onto *target_page* in *target_doc*."""
    try:
        shape_type = source_shape.getShapeType()
        new_shape = target_doc.createInstance(shape_type)
        if new_shape is None:
            return None
        target_page.add(new_shape)
        if hasattr(source_shape, "getPosition") and hasattr(new_shape, "setPosition"):
            new_shape.setPosition(source_shape.getPosition())
        for prop in _COPY_PROPS:
            if hasattr(source_shape, "getPropertyValue") and hasattr(new_shape, "setPropertyValue"):
                _copy_property(source_shape, new_shape, prop)
        for prop in _TEXT_LAYOUT_PROPS:
            if prop == "TextAutoGrowHeight":
                continue
            if hasattr(source_shape, "getPropertyValue") and hasattr(new_shape, "setPropertyValue"):
                _copy_property(source_shape, new_shape, prop)
        if hasattr(new_shape, "setPropertyValue"):
            try:
                new_shape.setPropertyValue("TextAutoGrowHeight", False)
            except Exception as exc:
                log.debug("TextAutoGrowHeight: %s", exc)
        _copy_shape_text(source_shape, new_shape)
        if hasattr(source_shape, "getSize") and hasattr(new_shape, "setSize"):
            new_shape.setSize(source_shape.getSize())
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
