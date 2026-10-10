# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clone Impress/Draw shapes between pages (used by PPTX import).

Pictures are reimported through ``GraphicProvider``. ``Graphic`` and
``GraphicURL`` on a shape from the hidden PPTX document are SfxItems in
that document's pool; assigning them and then closing the source aborts
soffice. Same rule as ``plugin/draw/designs.py`` ``_reimport_graphic``.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, cast

log = logging.getLogger(__name__)

# Primitives only. Graphic / GraphicURL / FillBitmap / Background are SfxItems
# from the hidden source pool — copying them and then closing the source
# aborts soffice (GetUserOrPoolDefaultItem). Pictures go through
# ``_reimport_shape_graphic`` instead. See plugin/draw/designs.py.
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


def _is_graphic_shape(shape: Any) -> bool:
    try:
        shape_type = str(shape.getShapeType())
    except Exception:
        return False
    return "GraphicObject" in shape_type


def _shape_graphic(source_shape: Any) -> Any | None:
    try:
        graphic = source_shape.getPropertyValue("Graphic")
    except Exception:
        try:
            graphic = source_shape.Graphic
        except Exception:
            return None
    if graphic is None:
        return None
    return graphic


def _graphic_load_prop(name: str, value: Any) -> Any:
    import uno

    # createUnoStruct is typed as a struct whose Name/Value are not str/Any.
    # Cast before assignment, same as writer.format.create_property_value.
    prop = cast("Any", uno.createUnoStruct("com.sun.star.beans.PropertyValue"))
    prop.Name = name
    prop.Value = value
    return prop


def _assign_graphic(dest_shape: Any, graphic: Any) -> bool:
    try:
        dest_shape.setPropertyValue("Graphic", graphic)
        return True
    except Exception:
        log.debug("setPropertyValue Graphic failed", exc_info=True)
    try:
        dest_shape.Graphic = graphic
        return True
    except Exception:
        log.debug("assign Graphic attribute failed", exc_info=True)
        return False


def _reimport_shape_graphic(uno_ctx: Any | None, source_shape: Any, dest_shape: Any) -> bool:
    """Install a dest-owned picture instead of the hidden source ``Graphic``.

    ``Graphic`` and ``GraphicURL`` are SfxItems from the hidden PPTX
    document's pool. ``import_pptx_to_doc`` then closes that document, so
    the target still points at a disposed pool and soffice aborts in
    ``GetUserOrPoolDefaultItem``. ``plugin/draw/designs.py`` documents the
    same anti-pattern for design masters.

    While the source is still open, ``GraphicProvider.storeGraphic`` writes
    the pixels to a temp file and ``queryGraphic`` loads a new ``XGraphic``
    the target owns. The hidden pool item is never assigned. This is the
    file round-trip in ``designs._reimport_graphic`` (a PPTX picture is not
    a shipped ``.otp`` ``Pictures/`` member, so there is nothing earlier to
    extract).
    """
    if uno_ctx is None:
        return False
    graphic = _shape_graphic(source_shape)
    if graphic is None:
        return False
    try:
        smgr = getattr(uno_ctx, "ServiceManager", None) or uno_ctx.getServiceManager()
    except Exception:
        log.debug("graphic reimport: no service manager", exc_info=True)
        return False
    if smgr is None:
        return False
    try:
        provider = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", uno_ctx)
    except Exception:
        log.debug("GraphicProvider create failed", exc_info=True)
        return False
    if provider is None:
        return False

    from plugin.framework.url_utils import path_to_file_url

    # SVG first, then PNG, matching designs._reimport_graphic. Raster PPTX
    # pictures usually fail the SVG store and succeed as PNG.
    for suffix, mime in ((".svg", "image/svg+xml"), (".png", "image/png")):
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            os.close(fd)
            url = path_to_file_url(path)
            provider.storeGraphic(graphic, (_graphic_load_prop("URL", url), _graphic_load_prop("MimeType", mime)))
            new_graphic = provider.queryGraphic((_graphic_load_prop("URL", url),))
            # queryGraphic must be a new object. Assigning the same Graphic
            # we just read would still alias the hidden pool.
            if new_graphic is None or new_graphic is graphic:
                continue
            if _assign_graphic(dest_shape, new_graphic):
                return True
        except Exception:
            log.debug("graphic reimport %s failed", mime, exc_info=True)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return False


def clone_shape_to_page(source_shape: Any, target_doc: Any, target_page: Any, uno_ctx: Any | None = None) -> Any | None:
    """Clone one shape from a source page onto *target_page* in *target_doc*.

    *uno_ctx* is the extension component context. Picture shapes need it so
    ``GraphicProvider`` can build a graphic the target document owns.
    """
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
        if _is_graphic_shape(source_shape) and not _reimport_shape_graphic(uno_ctx, source_shape, new_shape):
            # Leave the picture empty. Assigning the live Graphic would tie
            # the target to the hidden pool that import closes next.
            log.warning("PPTX picture reimport failed; left Graphic unset rather than alias the hidden source pool")

        if shape_type == "com.sun.star.drawing.GroupShape":
            for i in range(source_shape.getCount()):
                child = source_shape.getByIndex(i)
                clone_shape_to_page(child, target_doc, new_shape, uno_ctx=uno_ctx)

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


def copy_shapes_to_page(source_page: Any, target_doc: Any, target_page: Any, uno_ctx: Any | None = None) -> int:
    """Copy all shapes from *source_page* to *target_page*; return count copied."""
    copied = 0
    for i in range(source_page.getCount()):
        if clone_shape_to_page(source_page.getByIndex(i), target_doc, target_page, uno_ctx=uno_ctx) is not None:
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
