# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cut a picture down to a region and mark passages on it, in the picture's own pixels.

Why this exists: a lawyer pastes a cropped excerpt of a document (a screenshot, a page of a
contract) into a brief and marks the passage that matters. With only ``crop_*_mm`` and loose
drawing shapes, the agent had to turn the pixels it saw into millimetres of the page and guess
where the frame sat, so crops missed and marks drifted off the text. Here every box is in the
picture's own pixels (or percent of it), and the result is baked into one picture: the marks
cannot drift, and the cut-away part is no longer stored in the file (a crop of a client
document does not carry the hidden rest along).

How: LibreOffice draws it. A hidden Draw document gets a page the size of the region, the
whole picture shifted so the region lies on the page, and a rectangle per box or underline;
``GraphicExportFilter`` renders that page to a PNG at exactly the region's pixel size, clipping
whatever sticks out of the page. Highlights are painted afterwards on the pixels themselves, as
a highlighter does: paper turns yellow and ink stays black (Draw has no multiply blend, and a
translucent yellow over the text turned the letters olive). This handles every format
LibreOffice reads (1-bit scans, JPEG, palette PNG, SVG) with no imaging library in the
extension's Python.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, cast

from plugin.doc import visual_helpers

STYLES = ("highlight", "box", "underline")
# The highlight color is what white paper turns into; black ink stays black.
_COLORS = {"highlight": 0xFFEB3B, "box": 0xE00000, "underline": 0xE00000}
# Printed width of box and underline strokes, so they look the same at any picture resolution.
_STROKE_MM = 0.6
# Scratch-page scale: 1 picture pixel = 10 (1/100 mm). Any scale works because the export sets
# the pixel size; this one keeps a 3000 px scan at 30 cm, inside Draw's page limits.
_UNITS_PER_PX = 10
# Vector pictures (no pixels) get a 96 DPI pixel frame: 1 px = 2540 / 96 (1/100 mm).
_HMM_PER_PX_96DPI = 2540 / 96

Box = tuple[int, int, int, int]


def pixel_size(graphic: Any) -> tuple[int, int]:
    """The picture's pixel frame that every box refers to.

    Vector pictures (SVG, WMF) report no pixels; they get a 96 DPI frame from their logical
    size, so pixel and percent boxes still have one meaning for them."""
    px = graphic.getPropertyValue("SizePixel")
    if px.Width > 0 and px.Height > 0:
        return int(px.Width), int(px.Height)
    hmm = graphic.getPropertyValue("Size100thMM")
    return max(1, round(hmm.Width / _HMM_PER_PX_96DPI)), max(1, round(hmm.Height / _HMM_PER_PX_96DPI))


def to_pixel_box(box: Any, units: str, width_px: int, height_px: int) -> Box:
    """``[x, y, width, height]`` in px (or percent of the picture) -> pixel box clipped to it.

    Raises ValueError with a message the agent can act on."""
    if units not in ("px", "percent"):
        raise ValueError("units must be 'px' or 'percent', not %r." % (units,))
    if not isinstance(box, (list, tuple)) or len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in box):
        raise ValueError("A box is [x, y, width, height] (four numbers), got %r." % (box,))
    x, y, w, h = (float(v) for v in box)
    if units == "percent":
        x, w = x * width_px / 100.0, w * width_px / 100.0
        y, h = y * height_px / 100.0, h * height_px / 100.0
    x0, y0 = max(0, round(x)), max(0, round(y))
    x1, y1 = min(width_px, round(x + w)), min(height_px, round(y + h))
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise ValueError("Box %s %s is empty or outside the picture (%d x %d px)." % (list(box), units, width_px, height_px))
    return x0, y0, x1 - x0, y1 - y0


def parse_marks(highlights: Any, units: str, width_px: int, height_px: int, region: Box) -> list[tuple[Box, str, int]]:
    """Validate the ``highlights`` argument -> ``[(pixel box, style, rgb)]``.

    A mark that misses the kept region is an error, not a silent no-op: the agent asked for a
    mark it would never see."""
    if not isinstance(highlights, (list, tuple)):
        raise ValueError("highlights must be a list of {box, style, color} objects.")
    marks = []
    rx, ry, rw, rh = region
    for item in highlights:
        if not isinstance(item, dict):
            raise ValueError("Each highlight is an object like {\"box\": [x, y, width, height], \"style\": \"highlight\"}.")
        style = item.get("style") or "highlight"
        if style not in STYLES:
            raise ValueError("Unknown highlight style %r. Use one of: %s." % (style, ", ".join(STYLES)))
        box = to_pixel_box(item.get("box"), units, width_px, height_px)
        x, y, w, h = box
        if x >= rx + rw or y >= ry + rh or x + w <= rx or y + h <= ry:
            raise ValueError("Highlight box %s is outside the kept region %s (pixels)." % (list(box), list(region)))
        color = item.get("color")
        rgb = visual_helpers.parse_color_to_uno_int(color) if color is not None else None
        if color is not None and rgb is None:
            raise ValueError("Unknown highlight color %r. Use '#RRGGBB' or a color name." % (color,))
        marks.append((box, style, _COLORS[style] if rgb is None else rgb))
    return marks


def visible_region(crop: Any, actual_size: Any, graphic: Any) -> Box:
    """The part of the picture a ``GraphicCrop`` leaves visible, as a pixel box.

    Baking keeps what the reader sees: a picture already cropped with ``crop_*_mm`` (or by hand)
    stays cropped the same way when only marks are added. The crop is in 1/100 mm of the frame's
    ``ActualSize`` (Writer's own measure of the picture). The graphic's ``Size100thMM`` is not a
    substitute: it is 0 for a JPEG without a physical size, and Writer then measures the picture
    at the screen's DPI (about 92 on the Mac this was checked on), so assuming 96 DPI put the crop
    in the wrong place."""
    width_px, height_px = pixel_size(graphic)
    if crop is None or not (crop.Left > 0 or crop.Top > 0 or crop.Right > 0 or crop.Bottom > 0):
        return 0, 0, width_px, height_px
    if actual_size is None or actual_size.Width <= 0 or actual_size.Height <= 0:
        actual_size = graphic.getPropertyValue("Size100thMM")
    if actual_size.Width <= 0 or actual_size.Height <= 0:
        raise ValueError("This picture is cropped but its crop cannot be measured in pixels; pass crop_box.")
    sx, sy = width_px / actual_size.Width, height_px / actual_size.Height
    # Negative crop values pad the picture instead of cutting it; there is nothing to cut there.
    left, top = max(0, crop.Left) * sx, max(0, crop.Top) * sy
    right, bottom = max(0, crop.Right) * sx, max(0, crop.Bottom) * sy
    return to_pixel_box([left, top, width_px - left - right, height_px - top - bottom], "px", width_px, height_px)


def stroke_px(region_width_px: int, display_width_mm: float) -> int:
    """Stroke width in picture pixels that prints at about ``_STROKE_MM`` at the display width."""
    if display_width_mm <= 0:
        return 2
    return max(2, round(_STROKE_MM * region_width_px / display_width_mm))


def mark_rectangles(marks: list[tuple[Box, str, int]], region: Box, stroke: int) -> list[tuple[Box, str, int]]:
    """Where each mark's rectangle goes on the scratch page (pixels, region origin at 0,0).

    A box gets its stroke drawn just outside the requested area, so a tight box around a line of
    text does not cross the letters; an underline sits right below the box."""
    rx, ry = region[0], region[1]
    out = []
    for (x, y, w, h), style, rgb in marks:
        x, y = x - rx, y - ry
        if style == "box":
            # The stroke is centred on the rectangle edge: move the edge out by the stroke plus a
            # small gap so the whole stroke lands outside the box.
            pad = stroke
            out.append(((x - pad, y - pad, w + 2 * pad, h + 2 * pad), style, rgb))
        elif style == "underline":
            out.append(((x, y + h, w, stroke), style, rgb))
        else:
            out.append(((x, y, w, h), style, rgb))
    return out


def _pv(name: str, value: Any) -> Any:
    from com.sun.star.beans import PropertyValue

    return PropertyValue(Name=name, Value=value)


def bake(ctx: Any, graphic: Any, region: Box, marks: list[tuple[Box, str, int]], stroke: int) -> Any:
    """Return a new XGraphic: *graphic* cut to *region* (pixels) with *marks* drawn on it."""
    import uno
    from com.sun.star.awt import Point, Size
    from com.sun.star.drawing.FillStyle import NONE as FILL_NONE
    from com.sun.star.drawing.LineStyle import NONE as LINE_NONE

    smgr = ctx.ServiceManager
    width_px, height_px = pixel_size(graphic)
    rx, ry, rw, rh = region
    u = _UNITS_PER_PX

    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    draw = desktop.loadComponentFromURL("private:factory/sdraw", "_blank", 0, (_pv("Hidden", True),))
    tmp_path = None
    try:
        page = draw.getDrawPages().getByIndex(0)
        for border in ("BorderLeft", "BorderRight", "BorderTop", "BorderBottom"):
            page.setPropertyValue(border, 0)
        page.setPropertyValue("Width", rw * u)
        page.setPropertyValue("Height", rh * u)

        picture = draw.createInstance("com.sun.star.drawing.GraphicObjectShape")
        page.add(picture)
        picture.setPropertyValue("Graphic", graphic)
        # The whole picture, scaled to 1 px = u, shifted so the region sits on the page; the
        # export clips the rest. No GraphicCrop: it is measured in 1/100 mm of a size LibreOffice
        # derives from the screen DPI when the file has none (JPEG), so it cut the wrong rows.
        picture.setPosition(Point(-rx * u, -ry * u))
        picture.setSize(Size(width_px * u, height_px * u))

        rectangles = mark_rectangles(marks, region, stroke)
        for (x, y, w, h), style, rgb in rectangles:
            if style == "highlight":
                continue  # painted on the pixels below
            rect = draw.createInstance("com.sun.star.drawing.RectangleShape")
            page.add(rect)
            rect.setPosition(Point(x * u, y * u))
            rect.setSize(Size(max(1, w) * u, max(1, h) * u))
            if style == "box":
                rect.setPropertyValue("FillStyle", FILL_NONE)
                rect.setPropertyValue("LineColor", rgb)
                rect.setPropertyValue("LineWidth", stroke * u)
            else:
                rect.setPropertyValue("LineStyle", LINE_NONE)
                rect.setPropertyValue("FillColor", rgb)

        fd, tmp_path = tempfile.mkstemp(prefix="wa_image_mark_", suffix=".png")
        os.close(fd)
        export = smgr.createInstanceWithContext("com.sun.star.drawing.GraphicExportFilter", ctx)
        export.setSourceDocument(page)
        # FilterData must travel as a typed Any through uno.invoke; a plain tuple of
        # PropertyValue is dropped and the page comes out at 96 DPI instead of rw x rh px.
        # (The BMP export ignores PixelWidth even then, so the page always goes out as PNG.)
        uno_any = getattr(uno, "Any")  # pyuno helper the stubs do not export (same as rich_text)
        filter_data = uno_any("[]com.sun.star.beans.PropertyValue", cast("Any", (_pv("PixelWidth", rw), _pv("PixelHeight", rh))))
        args = (
            _pv("URL", uno.systemPathToFileUrl(tmp_path)),
            _pv("MediaType", "image/png"),
            _pv("FilterData", filter_data),
        )
        if not uno.invoke(export, "filter", cast("Any", (args,))):
            raise RuntimeError("LibreOffice could not render the marked picture (GraphicExportFilter returned false).")
        provider = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        result = _load(provider, tmp_path)
        highlights = [r for r in rectangles if r[1] == "highlight"]
        if highlights:
            result = _paint_highlights(provider, result, highlights, tmp_path)
        return result
    finally:
        draw.close(True)
        if tmp_path:
            for path in (tmp_path, tmp_path + ".bmp"):
                try:
                    os.remove(path)
                except OSError:
                    pass


def _load(provider: Any, path: str) -> Any:
    import uno

    graphic = provider.queryGraphic((_pv("URL", uno.systemPathToFileUrl(path)),))
    if graphic is None:
        raise RuntimeError("LibreOffice could not read back the marked picture.")
    return graphic


def _store(provider: Any, graphic: Any, path: str, mime: str) -> None:
    import uno

    provider.storeGraphic(graphic, (_pv("URL", uno.systemPathToFileUrl(path)), _pv("MimeType", mime)))


def _paint_highlights(provider: Any, graphic: Any, highlights: list[tuple[Box, str, int]], png_path: str) -> Any:
    """Round-trip through an uncompressed BMP (raw pixels) to multiply the highlights in, then
    back to PNG so the document does not store a BMP."""
    bmp_path = png_path + ".bmp"
    # GraphicProvider matches MIME types case-sensitively and knows BMP only as "image/x-MS-bmp";
    # "image/bmp" silently writes nothing.
    _store(provider, graphic, bmp_path, "image/x-MS-bmp")
    with open(bmp_path, "rb") as f:
        data = bytearray(f.read())
    multiply_bmp(data, highlights)
    with open(bmp_path, "wb") as f:
        f.write(data)
    _store(provider, _load(provider, bmp_path), png_path, "image/png")
    return _load(provider, png_path)


def multiply_bmp(data: bytearray, rectangles: list[tuple[Box, str, int]]) -> None:
    """Multiply each rectangle's color into an uncompressed 24/32-bit BMP, in place.

    Multiply is what ink does: white becomes the color, black stays black, so the letters under
    a highlight stay as sharp and dark as the rest. Raises RuntimeError on any other BMP layout."""
    if data[:2] != b"BM" or len(data) < 54:
        raise RuntimeError("LibreOffice did not write a bitmap to paint the highlights on.")
    offset = int.from_bytes(data[10:14], "little")
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    bits = int.from_bytes(data[28:30], "little")
    compression = int.from_bytes(data[30:34], "little")
    # 32-bit BI_BITFIELDS (3) is BGRA as LibreOffice writes it; anything else is not raw BGR.
    if bits not in (24, 32) or compression not in (0, 3) or (bits == 24 and compression != 0):
        raise RuntimeError("Unexpected bitmap layout from LibreOffice (%d bits, compression %d)." % (bits, compression))
    step = bits // 8
    stride = (width * step + 3) & ~3
    rows = abs(height)
    for (x, y, w, h), _style, rgb in rectangles:
        x0, x1 = max(0, x), min(width, x + w)
        y0, y1 = max(0, y), min(rows, y + h)
        if x0 >= x1 or y0 >= y1:
            continue
        # BMP pixels are B, G, R: one lookup table per channel.
        tables = [bytes(v * c // 255 for v in range(256)) for c in (rgb & 0xFF, (rgb >> 8) & 0xFF, rgb >> 16)]
        for yy in range(y0, y1):
            # A positive height means the rows are stored bottom-up.
            row = rows - 1 - yy if height > 0 else yy
            start = offset + row * stride
            for channel in range(3):
                cells = slice(start + x0 * step + channel, start + x1 * step, step)
                data[cells] = data[cells].translate(tables[channel])
