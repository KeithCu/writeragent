# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shape cloning and picture reimport between documents or pages."""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, NamedTuple

from plugin.doc.text_helpers import iter_elements
from plugin.framework.errors import reraise_if_disposed
from plugin.framework.uno_context import create_property_value, get_service_manager
from plugin.framework.url_utils import path_to_file_url

log = logging.getLogger(__name__)


class ClonedShape(NamedTuple):
    shape: Any
    shape_type: str
    reimported: bool | None  # None if not a GraphicObject
    graphic_shapes: int  # this shape plus descendants
    graphic_reimports: int
    empty_group_message: str | None  # set, not raised


SHAPE_CLONE_PROPS: tuple[str, ...] = (
    "Name",
    "Visible",
    "ZOrder",
    "LayerID",
    "FillStyle",
    "FillColor",
    "FillTransparence",
    "LineStyle",
    "LineColor",
    "LineWidth",
    "LineTransparence",
    "CharColor",
    "CharHeight",
    "CharWeight",
    "CharFontName",
    "CharPosture",
    "CharUnderline",
    "CharStrikeout",
    "CharEscapement",
    "CharKerning",
    "CharLetterSpacing",
    "TextAutoGrowHeight",
    "TextAutoGrowWidth",
    "TextHorizontalAdjust",
    "TextVerticalAdjust",
    "TextFitToSize",
    "CustomShapeGeometry",
    "Geometry",
    "Polygon",
    "PolyPolygon",
    "PolyPolygonBezier",
    "RotateAngle",
    "ParaAdjust",
    "ParaLeftMargin",
    "ParaRightMargin",
    "ParaTopMargin",
    "ParaBottomMargin",
)

TEXT_CHAR_PROPS: tuple[str, ...] = (
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

_FILL_STYLE_NAMES = {0: "none", 1: "solid", 2: "gradient", 3: "hatch", 4: "bitmap"}


def fill_style_kind(val: Any) -> str:
    """Resolve FillStyle enum or int to standard kind name."""
    raw = getattr(val, "value", val)
    if isinstance(raw, str):
        raw_upper = raw.strip().upper()
        if raw_upper in ("NONE", "SOLID", "GRADIENT", "HATCH", "BITMAP"):
            return raw_upper.lower()
        try:
            raw = int(raw)
        except (ValueError, TypeError):
            return "unknown"
    if isinstance(raw, int) or hasattr(raw, "__index__"):
        return _FILL_STYLE_NAMES.get(int(raw), "unknown")
    return "unknown"


def copy_prop(dest: Any, src: Any, name: str) -> str:
    """Copy one UNO property or interface attribute.

    Returns "copied", "missing", or "rejected". Properties in XPropertySet
    use getPropertyValue/setPropertyValue; geometry falls back to getattr/setattr.
    """
    try:
        value = src.getPropertyValue(name)
    except Exception as exc:
        reraise_if_disposed(exc)
        try:
            value = getattr(src, name)
        except Exception as inner_exc:
            reraise_if_disposed(inner_exc)
            return "missing"
    try:
        dest.setPropertyValue(name, value)
        return "copied"
    except Exception as exc:
        reraise_if_disposed(exc)
    try:
        setattr(dest, name, value)
        return "copied"
    except Exception as exc:
        reraise_if_disposed(exc)
        if name in ("Position", "Size"):
            raise RuntimeError("Failed to set %s on cloned shape" % name)
        return "rejected"


def _assign_graphic(dest_shape: Any, graphic: Any) -> bool:
    try:
        dest_shape.setPropertyValue("Graphic", graphic)
        return True
    except Exception as exc:
        reraise_if_disposed(exc)
    try:
        setattr(dest_shape, "Graphic", graphic)
        return True
    except Exception as exc:
        reraise_if_disposed(exc)
    return False


def reimport_graphic(
    uno_ctx: Any,
    src_shape: Any,
    dest_shape: Any,
    *,
    image_path: str | None = None,
) -> bool:
    """Attach chrome without sharing the Hidden source SfxItemPool."""
    if uno_ctx is None:
        return False

    smgr = get_service_manager(uno_ctx)
    if smgr is None:
        return False
    provider = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", uno_ctx)
    if provider is None:
        return False

    # 1) Extracted image path — never opens the Hidden Graphic.
    if image_path and os.path.isfile(image_path):
        url = path_to_file_url(image_path)
        log.debug("clone graphic via extracted file path=%s", image_path)
        new_graphic = None
        try:
            new_graphic = provider.queryGraphic((create_property_value("URL", url),))
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("GraphicProvider.queryGraphic failed url=%s", url, exc_info=True)
        if new_graphic is not None and _assign_graphic(dest_shape, new_graphic):
            return True

    # 2) Round-trip the live Graphic.
    graphic = None
    try:
        graphic = src_shape.getPropertyValue("Graphic")
    except Exception as exc:
        reraise_if_disposed(exc)
        try:
            graphic = getattr(src_shape, "Graphic", None)
        except Exception as inner_exc:
            reraise_if_disposed(inner_exc)
            graphic = None
    if graphic is None:
        return False

    log.debug("clone graphic via storeGraphic fallback")
    for suffix, mime in ((".svg", "image/svg+xml"), (".png", "image/png")):
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            os.close(fd)
            url = path_to_file_url(path)
            provider.storeGraphic(
                graphic,
                (
                    create_property_value("URL", url),
                    create_property_value("MimeType", mime),
                ),
            )
            new_graphic = provider.queryGraphic((create_property_value("URL", url),))
            if new_graphic is None or new_graphic is graphic:
                continue
            if _assign_graphic(dest_shape, new_graphic):
                return True
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("graphic reimport %s failed", mime, exc_info=True)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return False


def clone_shape(
    dest_doc: Any,
    dest_container: Any,
    src_shape: Any,
    *,
    uno_ctx: Any = None,
    image_path: str | None = None,
    image_paths: list[str | None] | None = None,
    shape_index: int = 0,
    warnings: list[str] | None = None,
) -> ClonedShape:
    """Create a dest-owned shape, add it, then copy visual props + geometry."""
    shape_type = getattr(src_shape, "ShapeType", "")
    if not (isinstance(shape_type, str) and shape_type):
        get_st = getattr(src_shape, "getShapeType", None)
        if callable(get_st):
            try:
                st_val = get_st()
                shape_type = str(st_val or "")
            except Exception as exc:
                reraise_if_disposed(exc)
                shape_type = ""
        else:
            shape_type = ""
    shape_type = str(shape_type)
    if not shape_type.startswith("com.sun.star."):
        raise RuntimeError("Unsupported shape type: %s" % shape_type)

    clone = dest_doc.createInstance(shape_type)
    if clone is None:
        raise RuntimeError(f"createInstance returned no shape for {shape_type}")

    dest_container.add(clone)
    shape_warnings: list[str] = []
    for prop in SHAPE_CLONE_PROPS:
        res = copy_prop(clone, src_shape, prop)
        if res == "rejected":
            token = f"rejected {prop} on shape {shape_index}"
            shape_warnings.append(token)
            if warnings is not None:
                warnings.append(token)
        elif res == "copied" and prop == "FillStyle":
            try:
                val = src_shape.getPropertyValue("FillStyle")
            except Exception as exc:
                reraise_if_disposed(exc)
                val = getattr(src_shape, "FillStyle", None)
            fill_kind = fill_style_kind(val)
            if fill_kind == "gradient":
                if copy_prop(clone, src_shape, "FillGradient") != "copied":
                    token = f"fill gradient not copied on shape {shape_index}"
                    shape_warnings.append(token)
                    if warnings is not None:
                        warnings.append(token)
                else:
                    copy_prop(clone, src_shape, "FillTransparenceGradient")
            elif fill_kind == "hatch":
                if copy_prop(clone, src_shape, "FillHatch") != "copied":
                    token = f"fill hatch not copied on shape {shape_index}"
                    shape_warnings.append(token)
                    if warnings is not None:
                        warnings.append(token)
            elif fill_kind == "bitmap":
                token = f"fill bitmap not copied on shape {shape_index}"
                shape_warnings.append(token)
                if warnings is not None:
                    warnings.append(token)
            elif fill_kind not in ("none", "solid"):
                token = f"fill {fill_kind} not copied on shape {shape_index}"
                shape_warnings.append(token)
                if warnings is not None:
                    warnings.append(token)

    reimported: bool | None = None
    graphic_shapes = 0
    graphic_reimports = 0
    if "GraphicObject" in shape_type:
        img_path = (image_paths.pop(0) if image_paths else None) if isinstance(image_paths, list) else image_path
        reimported = reimport_graphic(
            uno_ctx,
            src_shape,
            clone,
            image_path=img_path,
        )
        graphic_shapes += 1
        if reimported is True:
            graphic_reimports += 1
        log.debug("clone graphic_reimport ok=%s type=%s", reimported, shape_type)

    empty_group_message: str | None = None
    if "GroupShape" in shape_type:
        src_child_count = 0
        try:
            src_child_count = int(src_shape.getCount())
        except Exception as exc:
            reraise_if_disposed(exc)
            src_child_count = 0
        for child_idx in range(src_child_count):
            src_child = src_shape.getByIndex(child_idx)
            child_result = clone_shape(
                dest_doc,
                clone,
                src_child,
                uno_ctx=uno_ctx,
                image_paths=image_paths,
                shape_index=child_idx,
                warnings=warnings,
            )
            graphic_shapes += child_result.graphic_shapes
            graphic_reimports += child_result.graphic_reimports
            if child_result.empty_group_message is not None:
                copy_prop(clone, src_shape, "Position")
                copy_prop(clone, src_shape, "Size")
                return ClonedShape(
                    shape=clone,
                    shape_type=shape_type,
                    reimported=reimported,
                    graphic_shapes=graphic_shapes,
                    graphic_reimports=graphic_reimports,
                    empty_group_message=child_result.empty_group_message,
                )
        dest_child_count = 0
        try:
            dest_child_count = int(clone.getCount())
        except Exception as exc:
            reraise_if_disposed(exc)
            dest_child_count = 0
        if src_child_count > 0 and dest_child_count == 0:
            empty_group_message = (
                f"Cloned group on shape {shape_index} has no children (expected {src_child_count})."
            )

    try:
        src_get_text: Any = getattr(src_shape, "getText", None)
        dst_get_text: Any = getattr(clone, "getText", None)
        if callable(src_get_text) and callable(dst_get_text):
            src_text: Any = src_get_text()
            dst_text: Any = dst_get_text()
            if src_text is not None and dst_text is not None:
                dst_text.setString(src_text.getString())
                for prop in TEXT_CHAR_PROPS:
                    copy_prop(clone, src_shape, prop)
                src_portions: list[Any] = []
                if hasattr(src_text, "createEnumeration"):
                    src_portions = list(iter_elements(src_text.createEnumeration()))
                dst_portions: list[Any] = []
                if hasattr(dst_text, "createEnumeration"):
                    dst_portions = list(iter_elements(dst_text.createEnumeration()))
                fallback = src_portions[-1] if src_portions else None
                for idx, dst_portion in enumerate(dst_portions):
                    src_portion = src_portions[idx] if idx < len(src_portions) else fallback
                    if src_portion is not None:
                        for prop in TEXT_CHAR_PROPS:
                            copy_prop(dst_portion, src_portion, prop)
        else:
            res = copy_prop(clone, src_shape, "String")
            if res == "missing":
                get_str: Any = getattr(src_shape, "getString", None)
                set_str: Any = getattr(clone, "setString", None)
                if callable(get_str) and callable(set_str):
                    set_str(get_str())
    except Exception as exc:
        reraise_if_disposed(exc)

    # Geometry last so layout does not overwrite chrome metrics.
    copy_prop(clone, src_shape, "Position")
    copy_prop(clone, src_shape, "Size")
    log.debug("clone_shape index=%s type=%s warnings=%s", shape_index, shape_type, shape_warnings)
    return ClonedShape(
        shape=clone,
        shape_type=shape_type,
        reimported=reimported,
        graphic_shapes=graphic_shapes,
        graphic_reimports=graphic_reimports,
        empty_group_message=empty_group_message,
    )
