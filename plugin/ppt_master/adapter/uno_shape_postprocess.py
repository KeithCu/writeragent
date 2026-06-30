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
    "PolyPolygonBezier",
    "Geometry",
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
    "CharLetterSpacing",
)

# LO Break splits tspan runs onto one row when Y matches within this band (1/100 mm).
_SAME_LINE_Y_TOLERANCE_HMM = 150
_HEADER_BAND_MAX_Y_HMM = 2500
_TEXT_WIDTH_PADDING_HMM = 80


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


def _shape_uses_monospace_font(shape: Any) -> bool:
    try:
        text = shape.getText()
        enum = text.createEnumeration()
        while enum.hasMoreElements():
            portion = enum.nextElement()
            name = str(portion.getPropertyValue("CharFontName")).lower()
            if "consolas" in name or "mono" in name or "courier" in name:
                return True
    except Exception as exc:
        log.debug("monospace check: %s", exc)
    try:
        name = str(shape.getPropertyValue("CharFontName")).lower()
        return "consolas" in name or "mono" in name or "courier" in name
    except Exception as exc:
        log.debug("shape monospace check: %s", exc)
    return False


def _estimate_text_width_hmm(text: str, char_height_pt: float, *, monospace: bool = False) -> int:
    """Rough single-line width for Latin text (CharHeight is pt; convert to 1/100 mm)."""
    stripped = text.strip()
    if not stripped or char_height_pt <= 0:
        return _TEXT_WIDTH_PADDING_HMM
    width_factor = 0.62 if monospace else 0.55
    return max(int(len(stripped) * char_height_pt * 35.28 * width_factor) + _TEXT_WIDTH_PADDING_HMM, 80)


def _should_skip_width_tightening(shape: Any) -> bool:
    """Centered labels and badge digits must keep LO's frame for PDF alignment."""
    if "TextShape" not in shape.getShapeType():
        return False
    try:
        from com.sun.star.drawing import TextHorizontalAdjust

        if shape.getPropertyValue("TextHorizontalAdjust") == TextHorizontalAdjust.CENTER:
            return True
    except Exception as exc:
        log.debug("TextHorizontalAdjust: %s", exc)
    try:
        text_val = shape.getString().strip() if hasattr(shape, "getString") else shape.getText().getString().strip()
        if len(text_val) <= 2:
            return True
    except Exception as exc:
        log.debug("skip width text read: %s", exc)
    return False


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


def _fit_text_frame_width(shape: Any, *, max_width_hmm: int | None = None) -> None:
    """Shrink oversized LO text frames (tspan Break leaves full-line widths on fragments)."""
    if "TextShape" not in shape.getShapeType() or _should_skip_width_tightening(shape):
        return
    try:
        text_val = shape.getString() if hasattr(shape, "getString") else shape.getText().getString()
        if "\n" in text_val:
            return
        char_height_pt = float(shape.getPropertyValue("CharHeight"))
        if char_height_pt <= 0:
            return
        est_w = _estimate_text_width_hmm(text_val, char_height_pt, monospace=_shape_uses_monospace_font(shape))
        if max_width_hmm is not None:
            est_w = min(est_w, max_width_hmm)
        size = shape.getSize()
        if int(size.Width) <= est_w + 50:
            return
        from com.sun.star.awt import Size

        shape.setSize(Size(est_w, int(size.Height)))
    except Exception as exc:
        log.debug("fit text frame width: %s", exc)


def _collect_text_shapes(page: Any) -> list[tuple[int, int, Any]]:
    shapes: list[tuple[int, int, Any]] = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if "TextShape" not in shape.getShapeType():
            continue
        bbox = _shape_bbox(shape)
        if bbox is None:
            continue
        x, y, _w, _h = bbox
        shapes.append((y, x, shape))
    return shapes


def _tighten_same_line_text_fragments(page: Any) -> None:
    """Prevent adjacent tspan fragments on one row from overlapping in PDF export."""
    texts = _collect_text_shapes(page)
    if len(texts) < 2:
        return
    texts.sort(key=lambda item: (item[0], item[1]))
    line: list[tuple[int, int, Any]] = []
    line_y: int | None = None
    try:
        page_width = int(page.getPropertyValue("Width"))
    except Exception:
        page_width = DEFAULT_SLIDE_WIDTH_HMM

    def flush_line() -> None:
        if len(line) < 2:
            line.clear()
            return
        for idx, (y, x, shape) in enumerate(line):
            if _should_skip_width_tightening(shape):
                continue
            if idx + 1 < len(line):
                max_w = line[idx + 1][1] - x - 50
            else:
                max_w = page_width - x - 600
            _fit_text_frame_width(shape, max_width_hmm=max(80, max_w))
        line.clear()

    for y, x, shape in texts:
        if line_y is None or abs(y - line_y) <= _SAME_LINE_Y_TOLERANCE_HMM:
            line.append((y, x, shape))
            line_y = y if line_y is None else line_y
        else:
            flush_line()
            line = [(y, x, shape)]
            line_y = y
    flush_line()


def _fix_header_title_single_line(page: Any) -> None:
    """Keep slide title on one line — LO wraps when the frame is too narrow for CharHeight."""
    candidates: list[tuple[float, Any]] = []
    for _y, _x, shape in _collect_text_shapes(page):
        if _y > _HEADER_BAND_MAX_Y_HMM:
            continue
        try:
            char_h = float(shape.getPropertyValue("CharHeight"))
        except Exception:
            continue
        if char_h > 0:
            candidates.append((char_h, shape))
    if not candidates:
        return
    title = max(candidates, key=lambda item: item[0])[1]
    try:
        page_width = int(page.getPropertyValue("Width"))
        size = title.getSize()
        from com.sun.star.awt import Size

        # Wide frame + no auto-grow height stops mid-title wraps on long Georgia titles.
        title.setPropertyValue("TextAutoGrowHeight", False)
        try:
            title.setPropertyValue("TextAutoGrowWidth", False)
        except Exception as exc:
            log.debug("TextAutoGrowWidth: %s", exc)
        title.setSize(Size(max(page_width - 2400, int(size.Width)), int(size.Height)))
    except Exception as exc:
        log.debug("fix header title: %s", exc)


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
    _tighten_same_line_text_fragments(page)
    _fix_header_title_single_line(page)
    return {"shapes_adjusted": adjusted, "scale_x": scale_x, "scale_y": scale_y}
