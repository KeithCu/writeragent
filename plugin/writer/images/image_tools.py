# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Image insertion and gallery management tools for LibreOffice."""

import os
import shutil
import logging
import tempfile
import uno
from pathlib import Path
from typing import Any, cast
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_FRAME
from com.sun.star.awt import Size, Point
from com.sun.star.beans import PropertyValue

logger = logging.getLogger(__name__)

GALLERY_NAME = "writeragent_images"
GALLERY_IMAGE_DIR = GALLERY_NAME
# Shared with images.py download cache — paths under this dir are embedded, not linked.
IMAGE_CACHE_DIR_NAME = "writeragent_images"

_WRITER_GRAPHIC_SERVICE = "com.sun.star.text.TextGraphicObject"
_DRAW_GRAPHIC_SERVICE = "com.sun.star.drawing.GraphicObjectShape"


def get_type_doc(doc):
    TYPE_DOC = {"calc": "com.sun.star.sheet.SpreadsheetDocument", "draw": "com.sun.star.drawing.DrawingDocument", "impress": "com.sun.star.presentation.PresentationDocument", "web": "com.sun.star.text.WebDocument", "writer": "com.sun.star.text.TextDocument"}
    for k, v in TYPE_DOC.items():
        if doc.supportsService(v):
            return k
    return "writer"


def _image_cache_dir() -> str:
    return os.path.join(tempfile.gettempdir(), IMAGE_CACHE_DIR_NAME)


def _path_under_dir(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(directory)]) == os.path.abspath(directory)
    except ValueError:
        return False


def _should_link_image_path(img_path: str) -> bool:
    """User file paths are linked; temp/cache/generated paths stay embedded."""
    if not img_path or not os.path.isfile(img_path):
        return False
    abs_path = os.path.abspath(img_path)
    if _path_under_dir(abs_path, tempfile.gettempdir()):
        return False
    if _path_under_dir(abs_path, _image_cache_dir()):
        return False
    return True


def _file_url_for_path(img_path: str) -> str:
    return uno.systemPathToFileUrl(os.path.abspath(img_path))


def _mm_to_units(width_mm: int | float, height_mm: int | float) -> tuple[int, int]:
    return int(width_mm) * 100, int(height_mm) * 100


def _mm_to_px(width_mm: int | float, height_mm: int | float) -> tuple[int, int]:
    # 1/100 mm -> px at 96 DPI: px = units * 96 / 2540
    w_units, h_units = _mm_to_units(width_mm, height_mm)
    return max(1, int(w_units * 96 / 2540)), max(1, int(h_units * 96 / 2540))


def _safe_try_method(obj: Any, method_name: str, *args: Any) -> bool:
    try:
        method = getattr(obj, method_name, None)
        if callable(method):
            method(*args)
            return True
    except Exception as ex:
        logger.debug("_safe_try_method %s failed: %s", method_name, ex)
    return False


def _apply_graphic_properties(graphic, *, width: int, height: int, title: str, description: str, anchor_type=AS_CHARACTER, inside: str = "writer"):
    # Never use hasattr(graphic, "PropName") — PyUNO raises UnknownPropertyException.
    if inside in ("writer", "web"):
        _safe_set_property(graphic, "AnchorType", anchor_type)
    size = Size(width, height)
    if _has_uno_property(graphic, "Width") and _has_uno_property(graphic, "Height"):
        _safe_set_property(graphic, "Width", width)
        _safe_set_property(graphic, "Height", height)
    elif not _safe_set_property(graphic, "Size", size):
        if not _safe_try_method(graphic, "setSize", size):
            logger.debug("_apply_graphic_properties: could not set size %dx%d", width, height)
    if title:
        _safe_set_property(graphic, "Title", title)
    if description:
        _safe_set_property(graphic, "Description", description)


def _is_graphic_object(obj) -> bool:
    if obj is None:
        return False
    if hasattr(obj, "Graphic") and obj.Graphic is not None:
        return True
    try:
        if hasattr(obj, "getPropertyValue"):
            g = obj.getPropertyValue("Graphic")
            if g is not None:
                return True
    except Exception:
        pass
    try:
        if hasattr(obj, "supportsService"):
            return bool(
                obj.supportsService(_WRITER_GRAPHIC_SERVICE)
                or obj.supportsService("com.sun.star.text.GraphicObject")
                or obj.supportsService(_DRAW_GRAPHIC_SERVICE)
            )
    except Exception:
        pass
    return False


def _selection_graphic_object(model):
    try:
        selection = model.CurrentController.Selection
        if not selection:
            return None
        if hasattr(selection, "getCount"):
            if selection.getCount() != 1:
                return None
            obj = selection.getByIndex(0)
        else:
            obj = selection
        return obj if _is_graphic_object(obj) else None
    except Exception as e:
        logger.debug("_selection_graphic_object error: %s", e)
        return None


def _has_uno_property(obj: Any, name: str) -> bool:
    """True if *name* exists on the UNO PropertySet (never use hasattr on UNO attrs)."""
    try:
        psi = obj.getPropertySetInfo()
        if psi is not None and hasattr(psi, "hasPropertyByName"):
            return bool(psi.hasPropertyByName(name))
    except Exception:
        pass
    return False


def _safe_set_property(obj: Any, name: str, value: Any) -> bool:
    if not _has_uno_property(obj, name):
        return False
    try:
        obj.setPropertyValue(name, value)
        return True
    except Exception as ex:
        logger.debug("_safe_set_property %s failed: %s", name, ex)
        return False


def _graphic_from_provider(ctx: Any, file_url: str) -> Any | None:
    try:
        ctx_any = cast("Any", ctx)
        smgr = ctx_any.ServiceManager
        gp = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx_any)
        if gp is None:
            return None
        props = (PropertyValue(Name="URL", Value=file_url),)
        return gp.queryGraphic(props)
    except Exception as ex:
        logger.debug("_graphic_from_provider failed: %s", ex)
        return None


def _dispatch_insert_linked_graphic(ctx, model, file_url):
    """
    Insert a linked image via .uno:InsertGraphic (LO 6.1+).
    Setting GraphicURL directly embeds bytes; AsLink keeps the ODT small.
    """
    try:
        ctx_any = cast("Any", ctx)
        smgr = ctx_any.ServiceManager
        dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx_any)
        frame = model.getCurrentController().getFrame()
        props = (
            PropertyValue(Name="FileName", Value=file_url),
            PropertyValue(Name="AsLink", Value=True),
        )
        dispatcher.executeDispatch(frame, ".uno:InsertGraphic", "", 0, props)
        return _selection_graphic_object(model)
    except Exception as e:
        logger.debug("_dispatch_insert_linked_graphic failed: %s", e)
        return None


def _create_embedded_graphic(model, inside: str, file_url: str, ctx: Any | None = None):
    if inside in ("writer", "web"):
        graphic = model.createInstance(_WRITER_GRAPHIC_SERVICE)
    else:
        graphic = model.createInstance(_DRAW_GRAPHIC_SERVICE)
    if graphic is None:
        raise RuntimeError(f"Failed to create graphic instance for {inside}")
    if _safe_set_property(graphic, "GraphicURL", file_url):
        return graphic
    xgraphic = _graphic_from_provider(ctx, file_url) if ctx is not None else None
    if xgraphic is not None and _safe_set_property(graphic, "Graphic", xgraphic):
        return graphic
    raise RuntimeError("Could not assign GraphicURL or Graphic to embedded graphic")


def insert_image(ctx, model, img_path, width_px, height_px, title="", description="", add_to_gallery=True, add_frame=False):
    """
    Inserts an image into the document.
    width_px, height_px: Size in pixels.
    """
    inside = get_type_doc(model)

    # 1 inch = 25.4 mm = 2540 units (1/100th mm). At 96 DPI: 1px = 25.4/96 mm ≈ 0.2646 mm = 26.46 units.
    width_units = int(width_px * 26.46)
    height_units = int(height_px * 26.46)

    if inside in ["writer", "web"]:
        _insert_image_to_writer(ctx, model, img_path, width_units, height_units, title, description, add_frame)
    else:
        _insert_image_to_drawpage(ctx, model, inside, img_path, width_units, height_units, title, description)

    if add_to_gallery:
        add_image_to_gallery(ctx, img_path, f"{title}\n\n{description}")


def insert_image_at_locator(ctx, model, img_path, width_mm=80, height_mm=80, title="", description="", text_cursor=None):
    """
    Insert at an optional Writer text cursor, or current view cursor / draw page.
    width_mm, height_mm: display size in millimetres.
    Returns the inserted graphic object, or None on failure.
    """
    inside = get_type_doc(model)
    width_units, height_units = _mm_to_units(width_mm, height_mm)
    width_px, height_px = _mm_to_px(width_mm, height_mm)

    if inside in ("writer", "web"):
        if text_cursor is not None:
            _place_view_cursor_at_text_range(model, text_cursor)
        if _should_link_image_path(img_path):
            file_url = _file_url_for_path(img_path)
            graphic = _dispatch_insert_linked_graphic(ctx, model, file_url)
            if graphic is None:
                graphic = _insert_embedded_at_writer_cursor(model, img_path, width_units, height_units, title, description, text_cursor, ctx=ctx)
            else:
                _apply_graphic_properties(graphic, width=width_units, height=height_units, title=title, description=description, inside=inside)
        else:
            graphic = _insert_embedded_at_writer_cursor(model, img_path, width_units, height_units, title, description, text_cursor, ctx=ctx)
        return graphic

    _insert_image_to_drawpage(ctx, model, inside, img_path, width_units, height_units, title, description)
    return _selection_graphic_object(model)


def _place_view_cursor_at_text_range(model, text_cursor):
    try:
        vc = model.CurrentController.ViewCursor
        vc.gotoRange(text_cursor.getStart(), False)
    except Exception as e:
        logger.debug("_place_view_cursor_at_text_range: %s", e)


def _insert_embedded_at_writer_cursor(model, img_path, width, height, title, description, text_cursor=None, ctx: Any | None = None):
    doc_text = model.getText()
    file_url = _file_url_for_path(img_path)
    image = _create_embedded_graphic(model, "writer", file_url, ctx=ctx)
    _apply_graphic_properties(image, width=width, height=height, title=title, description=description, inside="writer")

    if text_cursor is not None:
        doc_text.insertTextContent(text_cursor, image, False)
        return image

    view_cursor = model.CurrentController.ViewCursor

    def to_text_cursor(vc):
        return doc_text.createTextCursorByRange(vc.getStart())

    try:
        tc = to_text_cursor(view_cursor)
        doc_text.insertTextContent(tc, image, False)
    except Exception as e:
        logger.debug("_insert_embedded_at_writer_cursor fallback: %s", e)
        view_cursor.jumpToStartOfPage()
        tc = to_text_cursor(view_cursor)
        doc_text.insertTextContent(tc, image, False)
    return image


def _insert_image_to_writer(ctx, model, img_path, width, height, title, description, add_frame):
    if add_frame:
        _insert_frame(ctx, model, img_path, width, height, title, description)
        return

    if _should_link_image_path(img_path):
        file_url = _file_url_for_path(img_path)
        graphic = _dispatch_insert_linked_graphic(ctx, model, file_url)
        if graphic is not None:
            _apply_graphic_properties(graphic, width=width, height=height, title=title, description=description, inside="writer")
            return
        logger.debug("_insert_image_to_writer: linked dispatch failed, embedding fallback")

    _insert_embedded_at_writer_cursor(model, img_path, width, height, title, description, ctx=ctx)


def _insert_frame(ctx, model, img_path, width, height, title, description):
    doc_text = model.getText()
    view_cursor = model.CurrentController.ViewCursor
    text_frame = model.createInstance("com.sun.star.text.TextFrame")
    frame_size = Size()
    frame_size.Height = height + 150  # Small padding for title
    frame_size.Width = width + 150
    text_frame.setSize(frame_size)
    text_frame.setPropertyValue("AnchorType", AT_FRAME)

    try:
        text_cursor = doc_text.createTextCursorByRange(view_cursor.getStart())
        doc_text.insertTextContent(text_cursor, text_frame, False)
    except Exception as e:
        logger.debug("_insert_frame insertTextContent fallback: %s", e)
        view_cursor.jumpToStartOfPage()
        text_cursor = doc_text.createTextCursorByRange(view_cursor.getStart())
        doc_text.insertTextContent(text_cursor, text_frame, False)

    frame_text = text_frame.getText()
    frame_cursor = frame_text.createTextCursor()
    _place_view_cursor_at_text_range(model, frame_cursor)

    if _should_link_image_path(img_path):
        file_url = _file_url_for_path(img_path)
        graphic = _dispatch_insert_linked_graphic(ctx, model, file_url)
        if graphic is not None:
            _apply_graphic_properties(graphic, width=width, height=height, title=title, description=description, inside="writer")
            if title:
                frame_text.insertString(frame_cursor, "\n" + title, False)
            return
        logger.debug("_insert_frame: linked dispatch failed, embedding fallback")

    file_url = _file_url_for_path(img_path)
    image = _create_embedded_graphic(model, "writer", file_url, ctx=ctx)
    _apply_graphic_properties(image, width=width, height=height, title=title, description=description, inside="writer")
    text_frame.insertTextContent(frame_cursor, image, False)
    if title:
        frame_text.insertString(frame_cursor, "\n" + title, False)


def _insert_image_to_drawpage(ctx, model, inside, img_path, width, height, title, description):
    ctrllr = model.CurrentController
    if inside == "calc":
        draw_page = ctrllr.ActiveSheet.DrawPage
    else:
        draw_page = ctrllr.CurrentPage

    if _should_link_image_path(img_path):
        file_url = _file_url_for_path(img_path)
        graphic = _dispatch_insert_linked_graphic(ctx, model, file_url)
        if graphic is not None:
            _apply_graphic_properties(graphic, width=width, height=height, title=title, description=description, inside=inside)
            if inside != "calc":
                pos = Point((draw_page.Width - width) // 2, (draw_page.Height - height) // 2)
                if hasattr(graphic, "setPosition"):
                    graphic.setPosition(pos)
            return
        logger.debug("_insert_image_to_drawpage: linked dispatch failed, embedding fallback")

    image = _create_embedded_graphic(model, inside, _file_url_for_path(img_path))
    _apply_graphic_properties(image, width=width, height=height, title=title, description=description, inside=inside)
    draw_page.add(image)
    if inside != "calc":
        pos = Point((draw_page.Width - width) // 2, (draw_page.Height - height) // 2)
        image.setPosition(pos)


def replace_graphic_source(ctx, model, graphic, img_path, width_units=None, height_units=None, title=None, description=None):
    """
    Replace an existing graphic's image source (by name), preserving object when possible.
    User paths are re-linked; temp/cache paths update GraphicURL (embed).
    """
    inside = get_type_doc(model)
    if width_units is None or height_units is None:
        try:
            size = graphic.getSize()
            width_units = size.Width
            height_units = size.Height
        except Exception:
            try:
                size = graphic.getPropertyValue("Size")
                width_units = size.Width
                height_units = size.Height
            except Exception:
                width_units, height_units = 8000, 8000

    if _should_link_image_path(img_path):
        file_url = _file_url_for_path(img_path)
        is_calc = inside == "calc"
        if inside in ("writer", "web"):
            anchor = graphic.getAnchor()
            if anchor is None:
                return False
            _place_view_cursor_at_text_range(model, anchor)
            new_graphic = _dispatch_insert_linked_graphic(ctx, model, file_url)
            if new_graphic is not None:
                model.getText().removeTextContent(graphic)
            if new_graphic is None:
                new_graphic = _create_embedded_graphic(model, "writer", file_url)
                _apply_graphic_properties(
                    new_graphic,
                    width=width_units,
                    height=height_units,
                    title=title or "",
                    description=description or "",
                    inside=inside,
                )
                model.getText().insertTextContent(anchor, new_graphic, False)
            else:
                _apply_graphic_properties(
                    new_graphic,
                    width=width_units,
                    height=height_units,
                    title=title or "",
                    description=description or "",
                    inside=inside,
                )
        else:
            ctrllr = model.CurrentController
            draw_page = ctrllr.ActiveSheet.DrawPage if is_calc else ctrllr.CurrentPage
            pos = graphic.getPosition()
            draw_page.remove(graphic)
            new_graphic = _dispatch_insert_linked_graphic(ctx, model, file_url)
            if new_graphic is None:
                new_graphic = _create_embedded_graphic(model, inside, file_url)
                new_graphic.setPosition(pos)
                _apply_graphic_properties(
                    new_graphic,
                    width=width_units,
                    height=height_units,
                    title=title or "",
                    description=description or "",
                    inside=inside,
                )
                draw_page.add(new_graphic)
            else:
                if hasattr(new_graphic, "setPosition"):
                    new_graphic.setPosition(pos)
                _apply_graphic_properties(
                    new_graphic,
                    width=width_units,
                    height=height_units,
                    title=title or "",
                    description=description or "",
                    inside=inside,
                )
        return True

    file_url = _file_url_for_path(img_path)
    if not _safe_set_property(graphic, "GraphicURL", file_url) and ctx is not None:
        xgraphic = _graphic_from_provider(ctx, file_url)
        if xgraphic is not None:
            _safe_set_property(graphic, "Graphic", xgraphic)
    if title is not None or description is not None:
        _apply_graphic_properties(
            graphic,
            width=width_units,
            height=height_units,
            title=title or "",
            description=description or "",
            inside=inside,
        )
    elif width_units is not None and height_units is not None:
        sz = Size(width_units, height_units)
        if not _safe_set_property(graphic, "Size", sz):
            _safe_try_method(graphic, "setSize", sz)
    return True


def _get_selected_graphic_object(model):
    """
    If the current selection is a single graphic, return (content, doc_type).
    Writer: content is XTextContent (GraphicObject); Calc/Draw: content is XShape (GraphicObjectShape).
    Otherwise return (None, None).
    """
    obj = _selection_graphic_object(model)
    if obj is None:
        return None, None
    return obj, get_type_doc(model)


def replace_image_in_place(ctx, model, img_path, width_px, height_px, title="", description="", add_to_gallery=True, add_frame=False):
    """
    If the current selection is a single graphic, replace it with the new image and return True.
    Otherwise return False (caller should fall back to insert_image).
    """
    obj, inside = _get_selected_graphic_object(model)
    if obj is None:
        return False
    width_units = int(width_px * 26.46)
    height_units = int(height_px * 26.46)
    try:
        if replace_graphic_source(ctx, model, obj, img_path, width_units, height_units, title, description):
            if add_to_gallery:
                add_image_to_gallery(ctx, img_path, f"{title}\n\n{description}")
            return True
        return False
    except Exception as e:
        logger.error("Replace image in place failed: %s", e)
        return False


def get_selected_image_dimensions_px(model):
    """
    Returns (width_px, height_px) of the currently selected graphic, or (None, None) if none.
    Uses 96 DPI: 1/100 mm -> px conversion (size * 96 / 2540).
    """
    obj, _ = _get_selected_graphic_object(model)
    if obj is None:
        return None, None
    try:
        if hasattr(obj, "getSize"):
            size = obj.getSize()
        else:
            size = obj.getPropertyValue("Size")
        # Size is in 1/100 mm. At 96 DPI: 1 mm = 96/25.4 px, so px = size * 96 / 2540
        w_px = int(size.Width * 96 / 2540)
        h_px = int(size.Height * 96 / 2540)
        return max(64, w_px), max(64, h_px)  # clamp to provider minimum
    except Exception:
        return None, None


def get_selected_image_base64(model, ctx=None):
    """
    Returns the base64 encoded data of the currently selected image.
    Works for GraphicObject (Writer) or GraphicObjectShape (Calc/Draw).
    ctx: optional component context (e.g. from chat panel or MainJob). If None, uses uno.getComponentContext().
    Use the panel/MainJob ctx for Calc so context-dependent logic works correctly.
    """
    try:
        selection = model.CurrentController.Selection
        if not selection:
            return None

        # For Writer, Selection is often a TextRange or an IndexAccess of objects
        if hasattr(selection, "getCount") and selection.getCount() > 0:
            obj = selection.getByIndex(0)
        else:
            obj = selection

        # Check if it's a graphic object
        if hasattr(obj, "Graphic"):
            graphic = obj.Graphic
        elif hasattr(obj, "getPropertyValue"):
            try:
                graphic = obj.getPropertyValue("Graphic")
            except Exception as e:
                logger.warning("get_selected_image_base64 missing Graphic property: %s", e)
                return None
        else:
            return None

        # Export graphic to base64
        # We use a GraphicProvider to export as PNG/JPG
        import base64

        if ctx is None:
            ctx = uno.getComponentContext()
        assert ctx is not None
        ctx_any = cast("Any", ctx)
        sm = getattr(ctx_any, "ServiceManager", getattr(ctx_any, "getServiceManager", lambda: None)())
        assert sm is not None
        gp = cast("Any", sm).createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx_any)

        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp_url = uno.systemPathToFileUrl(tmp.name)
            props = (PropertyValue(Name="URL", Value=tmp_url), PropertyValue(Name="MimeType", Value="image/png"))
            gp.storeGraphic(graphic, props)
            with open(tmp.name, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to get selected image: {e}")
        return None


def add_image_to_gallery(ctx, img_path, title):
    try:
        psettings = ctx.getValueByName("/singletons/com.sun.star.util.thePathSettings")
        gallery_dir = Path(uno.fileUrlToSystemPath(psettings.Storage_writable)) / GALLERY_IMAGE_DIR
        os.makedirs(gallery_dir, exist_ok=True)

        filename = os.path.basename(img_path)
        target_path = gallery_dir / filename
        shutil.copy2(img_path, target_path)

        themes_list = ctx.ServiceManager.createInstanceWithContext("com.sun.star.gallery.GalleryThemeProvider", ctx)

        if themes_list.hasByName(GALLERY_NAME):
            theme = themes_list.getByName(GALLERY_NAME)
        else:
            theme = themes_list.insertNewByName(GALLERY_NAME)

        theme.insertURLByIndex(uno.systemPathToFileUrl(str(target_path)), -1)
        theme.update()
    except Exception as e:
        logger.error(f"Failed to add to gallery: {e}")
