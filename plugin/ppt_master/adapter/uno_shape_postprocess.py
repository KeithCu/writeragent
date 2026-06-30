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
    "GraphicURL",
    "Graphic",
)

# Text frame layout props (must copy; setString after setSize expands height when AutoGrow is on).
_TEXT_LAYOUT_PROPS = (
    "TextAutoGrowHeight",
    "TextVerticalAdjust",
    "TextHorizontalAdjust",
    "TextFitToSize",
)

# Character props copied per text run (shape-level CharHeight/CharFontName alone are not enough).
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


def _scale_text_char_heights(shape: Any, scale: float) -> None:
    """Scale font size when the slide is resized (position/size scaling skips CharHeight)."""
    if scale == 1.0 or "TextShape" not in shape.getShapeType():
        return
    try:
        text = shape.getText()
        enum = text.createEnumeration()
        while enum.hasMoreElements():
            portion = enum.nextElement()
            try:
                height = float(portion.getPropertyValue("CharHeight"))
                if height > 0:
                    portion.setPropertyValue("CharHeight", height * scale)
            except Exception as exc:
                log.debug("scale portion CharHeight: %s", exc)
        try:
            height = float(shape.getPropertyValue("CharHeight"))
            if height > 0:
                shape.setPropertyValue("CharHeight", height * scale)
        except Exception as exc:
            log.debug("scale shape CharHeight: %s", exc)
    except Exception as exc:
        log.debug("scale text char heights: %s", exc)


def _fit_text_frame_height(shape: Any) -> None:
    """Shrink bloated LO text frames so consecutive SVG lines do not overlap."""
    if "TextShape" not in shape.getShapeType():
        return
    try:
        text_val = shape.getString() if hasattr(shape, "getString") else shape.getText().getString()
        if "\n" in text_val:
            return
        char_height_pt = float(shape.getPropertyValue("CharHeight"))
        if char_height_pt <= 0:
            return
        # CharHeight is pt; 1 pt ≈ 35.28 hundredths-mm. LO Break frames are often ~1.3× too tall.
        tight_h = max(int(char_height_pt * 35.28 * 1.05), 80)
        size = shape.getSize()
        if int(size.Height) <= tight_h + 50:
            return
        from com.sun.star.awt import Size

        shape.setSize(Size(int(size.Width), tight_h))
    except Exception as exc:
        log.debug("fit text frame: %s", exc)


def _scale_shape(shape: Any, scale_x: float, scale_y: float) -> None:
    if scale_x == 1.0 and scale_y == 1.0:
        return
    try:
        pos = shape.getPosition()
        size = shape.getSize()
        from com.sun.star.awt import Point, Size

        shape.setPosition(Point(int(pos.X * scale_x), int(pos.Y * scale_y)))
        shape.setSize(Size(max(1, int(size.Width * scale_x)), max(1, int(size.Height * scale_y))))
        _scale_text_char_heights(shape, min(scale_x, scale_y))
    except Exception as exc:
        log.debug("scale shape: %s", exc)


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
    """Copy draw/impress text with character formatting (plain setString resets fonts)."""
    if hasattr(source_shape, "getText") and hasattr(dest_shape, "getText"):
        src_text = source_shape.getText()
        dst_text = dest_shape.getText()
        dst_text.setString(src_text.getString())
        for prop in _TEXT_CHAR_PROPS:
            try:
                dest_shape.setPropertyValue(prop, source_shape.getPropertyValue(prop))
            except Exception as exc:
                log.debug("copy shape text prop %s: %s", prop, exc)
        src_enum = src_text.createEnumeration()
        dst_enum = dst_text.createEnumeration()
        while src_enum.hasMoreElements() and dst_enum.hasMoreElements():
            src_portion = src_enum.nextElement()
            dst_portion = dst_enum.nextElement()
            if not src_portion.getString() and not dst_portion.getString():
                continue
            _copy_text_portion_props(src_portion, dst_portion)
        return
    if hasattr(source_shape, "getString") and hasattr(dest_shape, "setString"):
        dest_shape.setString(source_shape.getString())


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
                # LO defaults True; setString then grows the frame and breaks SVG line spacing.
                new_shape.setPropertyValue("TextAutoGrowHeight", False)
            except Exception as exc:
                log.debug("TextAutoGrowHeight: %s", exc)
        _copy_shape_text(source_shape, new_shape)
        # setString with TextAutoGrowHeight expands the frame; restore LO's broken-apart box size.
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
        _fit_text_frame_height(shape)
        # Text shapes sometimes import with CharHeight 0; nudge if empty.
        try:
            if "TextShape" in shape.getShapeType():
                height = float(shape.getPropertyValue("CharHeight"))
                if height <= 0:
                    shape.setPropertyValue("CharHeight", 14.0)
        except Exception as exc:
            log.debug("text postprocess: %s", exc)
    return {"shapes_adjusted": adjusted, "scale_x": scale_x, "scale_y": scale_y}
