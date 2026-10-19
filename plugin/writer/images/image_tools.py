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

import hashlib
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
import uno
from com.sun.star.awt import Point, Size
from com.sun.star.beans import PropertyValue
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_FRAME
from plugin.doc import visual_helpers
from plugin.doc.text_helpers import clone_text_range
from plugin.framework.errors import best_effort

# WriterCompoundUndo is intentionally imported function-locally in insert_image,
# insert_image_at_locator, insert_image_into_header_footer, and
# replace_graphic_source because LibrePy bundles image_tools.py without edit_review.py.
# A module-level import would break LibrePy when calc or vision only needs export helpers.

log = logging.getLogger(__name__)

GALLERY_NAME = "writeragent_images"
GALLERY_IMAGE_DIR = GALLERY_NAME
# Shared with images.py download cache — paths under this dir are embedded, not linked.
IMAGE_CACHE_DIR_NAME = "writeragent_images"

_WRITER_GRAPHIC_SERVICE = visual_helpers.WRITER_GRAPHIC_SERVICE
_DRAW_GRAPHIC_SERVICE = visual_helpers.DRAW_GRAPHIC_SERVICE


def get_type_doc(doc: Any) -> str:
    return visual_helpers.get_visual_doc_type(doc)


def _image_cache_dir() -> str:
    return os.path.join(tempfile.gettempdir(), IMAGE_CACHE_DIR_NAME)


def _path_under_dir(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(directory)]) == os.path.realpath(directory)
    except (ValueError, OSError):
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
    return visual_helpers.mm_to_units(width_mm, height_mm)


def _safe_try_method(obj: Any, method_name: str, *args: Any) -> bool:
    return visual_helpers.safe_try_method(obj, method_name, *args)


def _apply_graphic_properties(graphic: Any, *, width: int, height: int, title: str, description: str, anchor_type: Any = AS_CHARACTER, inside: str = "writer") -> None:
    # Never use hasattr(graphic, "PropName") — PyUNO raises UnknownPropertyException.
    if inside in ("writer", "web"):
        _safe_set_property(graphic, "AnchorType", anchor_type)
    size = Size(width, height)
    if _has_uno_property(graphic, "Width") and _has_uno_property(graphic, "Height"):
        _safe_set_property(graphic, "Width", width)
        _safe_set_property(graphic, "Height", height)
    elif not _safe_set_property(graphic, "Size", size):
        if not _safe_try_method(graphic, "setSize", size):
            log.debug("_apply_graphic_properties: could not set size %dx%d", width, height)
    if title:
        _safe_set_property(graphic, "Title", title)
    if description:
        _safe_set_property(graphic, "Description", description)


def _selection_graphic_object(model: Any) -> Any | None:
    return visual_helpers.selected_graphic_object(model)


def _has_uno_property(obj: Any, name: str) -> bool:
    """True if *name* exists on the UNO PropertySet (never use hasattr on UNO attrs)."""
    return visual_helpers.has_uno_property(obj, name)


def _safe_set_property(obj: Any, name: str, value: Any) -> bool:
    return visual_helpers.safe_set_property(obj, name, value)


def _graphic_from_provider(ctx: Any, file_url: str) -> Any | None:
    try:
        smgr = ctx.ServiceManager
        gp = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        if gp is None:
            return None
        props = (PropertyValue(Name="URL", Value=file_url),)
        return gp.queryGraphic(props)
    except Exception as ex:
        log.debug("_graphic_from_provider failed: %s", ex)
        return None


def _is_same_graphic(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if a is b or a == b:
        return True
    a_name = a.getName()
    b_name = b.getName()
    if isinstance(a_name, str) and isinstance(b_name, str) and a_name and a_name == b_name:
        return True
    return False


# Bugfix: executeDispatch could fail silently or keep the existing selection unchanged,
# causing subsequent property calls to resize or modify the previously selected graphic.
# Verifying that the selection changed to a new graphic object prevents modifying
# unintended objects, allowing caller to fall back cleanly to embedding.
def _dispatch_insert_linked_graphic(ctx: Any, model: Any, file_url: str) -> Any | None:
    """Insert a linked image via .uno:InsertGraphic (LO 6.1+).

    Setting GraphicURL directly embeds bytes; AsLink keeps the ODT small.
    """
    try:
        before = _selection_graphic_object(model)
        smgr = ctx.ServiceManager
        dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = model.getCurrentController().getFrame()
        props = (
            PropertyValue(Name="FileName", Value=file_url),
            PropertyValue(Name="AsLink", Value=True),
        )
        dispatcher.executeDispatch(frame, ".uno:InsertGraphic", "", 0, props)
        after = _selection_graphic_object(model)
        if after is not None and not _is_same_graphic(before, after):
            return after
        log.debug("_dispatch_insert_linked_graphic: selection unchanged")
        return None
    except Exception:
        log.debug("_dispatch_insert_linked_graphic failed", exc_info=True)
        return None


def _create_embedded_graphic(model: Any, inside: str, file_url: str, ctx: Any | None = None) -> Any:
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


def insert_image(
    ctx: Any,
    model: Any,
    img_path: str,
    width_px: int,
    height_px: int,
    title: str = "",
    description: str = "",
    add_to_gallery: bool = True,
    add_frame: bool = False,
    page_index: int | None = None,
    x_mm: int | float | None = None,
    y_mm: int | float | None = None,
    *,
    size_units: tuple[int, int] | None = None,
) -> None:
    """Inserts an image into the document.

    width_px, height_px: Source/generate pixels, not on-page millimetres.
    Display size is capped (``px_to_display_units``) so a 1024px generate
    stays a ~135mm inset instead of filling the Writer page.
    size_units overrides the pixel conversion when the frame size is already known.
    page_index is a 0-based Draw/Impress page or a Calc sheet; x_mm/y_mm are
    ignored on Calc; omitted x/y centers a Draw/Impress page. add_frame is
    Writer/Web only.
    """
    from plugin.writer.edit_review import WriterCompoundUndo

    inside = get_type_doc(model)

    if size_units is not None:
        width_units, height_units = size_units
    else:
        width_units, height_units = visual_helpers.px_to_display_units(width_px, height_px)

    # Gallery is filesystem-only; keep it outside the document undo group.
    with WriterCompoundUndo(model, "WriterAgent: Insert image"):
        if inside in ["writer", "web"]:
            _insert_image_to_writer(ctx, model, img_path, width_units, height_units, title, description, add_frame)
        else:
            if add_frame:
                log.debug("insert_image: add_frame is Writer/Web only; ignoring on %s", inside)
            _insert_image_to_drawpage(
                ctx, model, inside, img_path, width_units, height_units, title, description,
                page_index=page_index, x_mm=x_mm, y_mm=y_mm,
            )

    if add_to_gallery:
        add_image_to_gallery(ctx, img_path)


def insert_image_at_locator(ctx: Any, model: Any, img_path: str, width_mm: int | float = 80, height_mm: int | float = 80, title: str = "", description: str = "", text_cursor: Any = None, page_index: int | None = None, x_mm: int | float | None = None, y_mm: int | float | None = None) -> Any:
    """Insert at an optional Writer text cursor, or current view cursor / draw page.

    width_mm, height_mm: display size in millimetres.
    For Draw/Impress, optional page_index (0-based) and x_mm/y_mm; omitted x/y centers on the page.
    Returns the inserted graphic object, or None on failure.
    """
    from plugin.writer.edit_review import WriterCompoundUndo

    inside = get_type_doc(model)
    width_units, height_units = _mm_to_units(width_mm, height_mm)

    with WriterCompoundUndo(model, "WriterAgent: Insert image"):
        if inside in ("writer", "web"):
            return _insert_writer_graphic(
                ctx,
                model,
                img_path,
                width_units,
                height_units,
                title,
                description,
                text_cursor=text_cursor,
            )

        return _insert_image_to_drawpage(
            ctx, model, inside, img_path, width_units, height_units, title, description,
            page_index=page_index, x_mm=x_mm, y_mm=y_mm,
        )


def insert_image_into_header_footer(
    model: Any,
    img_path: str,
    region: str,
    *,
    width_mm: int | float = 80,
    height_mm: int | float = 80,
    style_name: str = "Standard",
    auto_height: bool = True,
    title: str = "",
    description: str = "",
    ctx: Any | None = None,
) -> dict[str, Any]:
    """Insert an image into a Writer page header or footer.

    Anchors ``AS_CHARACTER`` so the image contributes to line height. A
    floating ``AT_CHARACTER`` image does not grow the region, so a logo
    taller than the fixed header height overlaps the body text.

    ``region`` is a ``_REGION_PROPS`` key (``header``, ``footer``,
    ``header_first``, ``footer_first``, and the left variants). A
    different-first-page letterhead logo belongs in ``header_first`` /
    ``footer_first`` after ``FirstIsShared=False`` — those are separate
    ``XText`` objects; writing the shared ``header`` never reaches them.
    """
    from plugin.writer.edit_review import WriterCompoundUndo
    from plugin.writer.page import (
        _REGION_PROPS,
        resolve_page_style,
        set_header_footer_auto_height,
    )

    if region not in _REGION_PROPS:
        raise ValueError("region must be one of: %s" % ", ".join(_REGION_PROPS))

    # Enable region + auto-height + embed are separate UNO steps; group them.
    with WriterCompoundUndo(model, "WriterAgent: Insert image in header/footer"):
        style, resolved = resolve_page_style(model, style_name)
        is_on_prop, text_prop = _REGION_PROPS[region]
        if not style.getPropertyValue(is_on_prop):
            style.setPropertyValue(is_on_prop, True)
        if auto_height:
            set_header_footer_auto_height(style, region, True)

        region_text = style.getPropertyValue(text_prop)
        cursor = region_text.createTextCursorByRange(region_text.getEnd())
        width_units, height_units = _mm_to_units(width_mm, height_mm)
        graphic = _insert_embedded_at_writer_cursor(
            model,
            img_path,
            width_units,
            height_units,
            title,
            description,
            text_cursor=cursor,
            text_container=region_text,
            ctx=ctx,
        )
        return {
            "graphic": graphic,
            "style_name": resolved,
            "region": region,
            "auto_height": bool(auto_height),
        }


def _place_view_cursor_at_text_range(model: Any, text_cursor: Any) -> bool:
    def _place() -> bool:
        vc = model.CurrentController.ViewCursor
        vc.gotoRange(text_cursor.getStart(), False)
        return True

    return best_effort(_place, "_place_view_cursor_at_text_range failed", default=False)


def _cursor_for_writer_view(vc: Any) -> tuple[Any, Any]:
    """Collapsed model cursor at the view cursor, on the host XText.

    Why getStart() first: ``clone_text_range(ViewCursor)`` calls
    ``host.createTextCursorByRange(vc)`` with the full SwXTextViewCursor.
    That raises a bare ``RuntimeException`` (empty message) on a plain
    Writer body — PR #796 / image insert. The pre-#796 path
    ``createTextCursorByRange(vc.getStart())`` works.

    Why host XText, not ``model.getText()``: a table-cell or frame range
    cannot be cloned through the body ("End of content node doesn't have
    the proper start node"). ``clone_text_range`` stays as the nested
    fallback when getStart() is rejected.
    """
    host = vc.getText()
    try:
        return host, host.createTextCursorByRange(vc.getStart())
    except Exception as e:
        log.debug("_cursor_for_writer_view nested clone: %s", e)
        return host, clone_text_range(vc)


def _insert_embedded_at_writer_cursor(
    model: Any,
    img_path: str,
    width: int,
    height: int,
    title: str,
    description: str,
    text_cursor: Any = None,
    text_container: Any = None,
    ctx: Any | None = None,
) -> Any:
    doc_text = text_container if text_container is not None else model.getText()
    file_url = _file_url_for_path(img_path)
    image = _create_embedded_graphic(model, "writer", file_url, ctx=ctx)
    # Headers/footers need AS_CHARACTER so the image contributes to line height
    # and HeaderIsDynamicHeight can grow the region (AT_CHARACTER floats and spills).
    _apply_graphic_properties(
        image,
        width=width,
        height=height,
        title=title,
        description=description,
        anchor_type=AS_CHARACTER,
        inside="writer",
    )

    if text_cursor is not None:
        doc_text.insertTextContent(text_cursor, image, False)
        return image

    view_cursor = model.CurrentController.ViewCursor

    def insert_at_view(vc: Any) -> None:
        host, tc = _cursor_for_writer_view(vc)
        host.insertTextContent(tc, image, False)

    try:
        insert_at_view(view_cursor)
    except Exception as e:
        log.debug("_insert_embedded_at_writer_cursor fallback: %s", e)
        view_cursor.jumpToStartOfPage()
        insert_at_view(view_cursor)
    return image


# Bugfix: When placing the view cursor at text_cursor failed, linked insert
# proceeded anyway with executeDispatch, dropping the image at the user's
# active caret instead of the targeted locator position. Skipping dispatch when
# view cursor placement fails ensures the graphic is cleanly embedded at text_cursor.
def _insert_writer_graphic(
    ctx: Any,
    model: Any,
    img_path: str,
    width: int,
    height: int,
    title: str,
    description: str,
    *,
    text_cursor: Any = None,
    text_container: Any = None,
) -> Any:
    linking = _should_link_image_path(img_path)
    if linking and text_cursor is not None:
        if not _place_view_cursor_at_text_range(model, text_cursor):
            linking = False

    if linking:
        file_url = _file_url_for_path(img_path)
        graphic = _dispatch_insert_linked_graphic(ctx, model, file_url)
        if graphic is not None:
            _apply_graphic_properties(
                graphic,
                width=width,
                height=height,
                title=title,
                description=description,
                inside="writer",
            )
            return graphic
        log.debug("_insert_writer_graphic: linked dispatch failed, embedding")

    return _insert_embedded_at_writer_cursor(
        model,
        img_path,
        width,
        height,
        title,
        description,
        text_cursor=text_cursor,
        text_container=text_container,
        ctx=ctx,
    )


def _insert_image_to_writer(ctx: Any, model: Any, img_path: str, width: int, height: int, title: str, description: str, add_frame: bool) -> None:
    if add_frame:
        _insert_frame(ctx, model, img_path, width, height, title, description)
        return
    _insert_writer_graphic(ctx, model, img_path, width, height, title, description)


_FRAME_TITLE_HEIGHT_UNITS = 500


# Bugfix: Inserting caption using the pre-image cursor could place caption text
# in front of the image. Sizing added arbitrary 150-unit padding to width/height,
# and if image insertion failed inside the frame, the empty frame remained in the
# document because WriterCompoundUndo does not roll back on exception.
# We now size the frame to exact width and height + 500 (if title), remove the
# empty frame if image insert raises, and place the caption at frame_text.getEnd().
def _insert_frame(ctx: Any, model: Any, img_path: str, width: int, height: int, title: str, description: str) -> None:
    view_cursor = model.CurrentController.ViewCursor
    text_frame = model.createInstance("com.sun.star.text.TextFrame")
    frame_size = Size()
    frame_size.Width = width
    frame_size.Height = height + (_FRAME_TITLE_HEIGHT_UNITS if title else 0)
    text_frame.setSize(frame_size)
    text_frame.setPropertyValue("AnchorType", AT_FRAME)

    try:
        host, text_cursor = _cursor_for_writer_view(view_cursor)
        host.insertTextContent(text_cursor, text_frame, False)
    except Exception as e:
        log.debug("_insert_frame insertTextContent fallback: %s", e)
        view_cursor.jumpToStartOfPage()
        host, text_cursor = _cursor_for_writer_view(view_cursor)
        host.insertTextContent(text_cursor, text_frame, False)

    frame_text = text_frame.getText()
    frame_cursor = frame_text.createTextCursor()
    try:
        _insert_writer_graphic(
            ctx,
            model,
            img_path,
            width,
            height,
            title,
            description,
            text_cursor=frame_cursor,
            text_container=frame_text,
        )
    except Exception:
        log.exception("_insert_frame: image insert failed; removing empty frame")
        try:
            host.removeTextContent(text_frame)
        except Exception:
            log.exception("_insert_frame: could not remove empty frame")
        raise

    if title:
        end = frame_text.createTextCursorByRange(frame_text.getEnd())
        frame_text.insertString(end, "\n" + title, False)


# Bugfix: Calc ignored page_index entirely and always returned the active sheet's
# draw page, because getDrawPages() is Draw/Impress-specific and Calc sheets each
# have their own draw page accessed via sheet.getDrawPage().
def _draw_page_for_insert(model: Any, inside: str, page_index: int | None = None) -> Any:
    if inside in ("draw", "impress") and page_index is not None:
        pages = model.getDrawPages()
        count = pages.getCount()
        if page_index < 0 or page_index >= count:
            raise IndexError("Page index %s out of range (0..%s)." % (page_index, count - 1))
        return pages.getByIndex(page_index)
    if inside == "calc" and page_index is not None:
        sheets = model.getSheets()
        count = sheets.getCount()
        if page_index < 0 or page_index >= count:
            raise IndexError("Sheet index %s out of range (0..%s)." % (page_index, count - 1))
        return sheets.getByIndex(page_index).getDrawPage()
    draw_page = visual_helpers.get_active_draw_page(model, inside)
    if draw_page is None:
        raise RuntimeError(f"Could not resolve draw page for {inside}")
    return draw_page


def _position_on_draw_page(draw_page: Any, width: int, height: int, x_mm: int | float | None, y_mm: int | float | None) -> Any:
    if x_mm is not None or y_mm is not None:
        x = visual_helpers.mm_to_unit(x_mm) if x_mm is not None else 0
        y = visual_helpers.mm_to_unit(y_mm) if y_mm is not None else 0
        return Point(x, y)
    return Point((draw_page.Width - width) // 2, (draw_page.Height - height) // 2)


def _insert_image_to_drawpage(
    ctx: Any,
    model: Any,
    inside: str,
    img_path: str,
    width: int,
    height: int,
    title: str,
    description: str,
    page_index: int | None = None,
    x_mm: int | float | None = None,
    y_mm: int | float | None = None,
) -> Any:
    draw_page = _draw_page_for_insert(model, inside, page_index)

    # When page_index is specified, do not dispatch .uno:InsertGraphic because
    # dispatch inserts on the currently active view page, then setPosition would
    # run against a different page's coordinates. Embed directly onto draw_page instead.
    if page_index is None and _should_link_image_path(img_path):
        file_url = _file_url_for_path(img_path)
        graphic = _dispatch_insert_linked_graphic(ctx, model, file_url)
        if graphic is not None:
            _apply_graphic_properties(graphic, width=width, height=height, title=title, description=description, inside=inside)
            if inside != "calc":
                pos = _position_on_draw_page(draw_page, width, height, x_mm, y_mm)
                if not _safe_try_method(graphic, "setPosition", pos):
                    log.debug("_insert_image_to_drawpage: setPosition failed on linked graphic")
            return graphic
        log.debug("_insert_image_to_drawpage: linked dispatch failed, embedding fallback")

    image = _create_embedded_graphic(model, inside, _file_url_for_path(img_path), ctx=ctx)
    _apply_graphic_properties(image, width=width, height=height, title=title, description=description, inside=inside)
    draw_page.add(image)
    if inside != "calc":
        pos = _position_on_draw_page(draw_page, width, height, x_mm, y_mm)
        if not _safe_try_method(image, "setPosition", pos):
            log.debug("_insert_image_to_drawpage: setPosition failed on embedded graphic")
    return image


# Bugfix: The linked replace branch dispatched .uno:InsertGraphic at the view cursor,
# which could drop the image at the wrong location or duplicate it, and removed the
# original with model.getText() (body only, breaking tables/frames) or get_active_draw_page
# (not the shape's real page). Furthermore, WriterCompoundUndo does not roll back on exception.
# LibreOffice TextGraphicObject.idl specifies that setting GraphicURL directly loads
# the image and sets Graphic on the existing object in place, preserving its name,
# anchor, and frame. Linking is insert-only.
def replace_graphic_source(
    ctx: Any,
    model: Any,
    graphic: Any,
    img_path: str,
    width_units: Any = None,
    height_units: Any = None,
    title: str | None = None,
    description: str | None = None,
) -> bool:
    """Replace an existing graphic's image source in place.

    Image linking is insert-only; replacement always embeds the new image onto
    the existing object, preserving its name, anchor, and frame.
    Passing None or "" for title leaves the current title unchanged. Same for
    description.
    """
    from plugin.writer.edit_review import WriterCompoundUndo

    inside = get_type_doc(model)
    if width_units is None or height_units is None:
        size = graphic.getSize()
        width_units = size.Width
        height_units = size.Height

    with WriterCompoundUndo(model, "WriterAgent: Replace image"):
        file_url = _file_url_for_path(img_path)
        placed = _safe_set_property(graphic, "GraphicURL", file_url)
        if not placed and ctx is not None:
            xgraphic = _graphic_from_provider(ctx, file_url)
            if xgraphic is not None:
                placed = _safe_set_property(graphic, "Graphic", xgraphic)
        if not placed:
            return False

        if title is not None or description is not None:
            _apply_graphic_properties(
                graphic,
                width=width_units,
                height=height_units,
                title=title or "",
                description=description or "",
                inside=inside,
            )
        else:
            sz = Size(width_units, height_units)
            if not _safe_set_property(graphic, "Size", sz):
                _safe_try_method(graphic, "setSize", sz)
        return True

def _get_selected_graphic_object(model: Any) -> tuple[Any | None, str | None]:
    """
    If the current selection is a single graphic, return (content, doc_type).
    Writer: content is XTextContent (GraphicObject); Calc/Draw: content is XShape (GraphicObjectShape).
    Otherwise return (None, None).
    """
    obj = _selection_graphic_object(model)
    if obj is None:
        return None, None
    return obj, get_type_doc(model)


def replace_image_in_place(ctx: Any, model: Any, img_path: str, width_px: int, height_px: int, title: str = "", description: str = "", add_to_gallery: bool = True) -> bool:
    """If the current selection is a single graphic, replace it with the new image and return True.

    Otherwise return False (caller should fall back to insert_image).
    """
    obj, _inside = _get_selected_graphic_object(model)
    if obj is None:
        return False
    # Same cap as insert_image: generate pixels must not become page mm.
    width_units, height_units = visual_helpers.px_to_display_units(width_px, height_px)
    try:
        if replace_graphic_source(ctx, model, obj, img_path, width_units, height_units, title, description):
            if add_to_gallery:
                add_image_to_gallery(ctx, img_path)
            return True
        return False
    except Exception:
        log.exception("Replace image in place failed")
        return False


def get_selected_image_pixel_size(model: Any) -> tuple[int | None, int | None]:
    """Returns (width_px, height_px) of the currently selected graphic's native image data,
    or (None, None) if none or if it's a vector graphic with no pixels.
    """
    obj, _unused = _get_selected_graphic_object(model)
    if obj is None:
        return None, None
    graphic = _graphic_from_object(obj)
    if graphic is None:
        log.debug("get_selected_image_pixel_size: no SizePixel")
        return None, None
    px = graphic.getPropertyValue("SizePixel")
    if px is None or px.Width <= 0 or px.Height <= 0:
        log.debug("get_selected_image_pixel_size: no SizePixel")
        return None, None
    return int(px.Width), int(px.Height)


def get_selected_image_dimensions_px(model: Any) -> tuple[int | None, int | None]:
    """Returns (width_px, height_px) of the currently selected graphic, or (None, None) if none.

    Uses 96 DPI: 1/100 mm -> px conversion (size * 96 / 2540).
    The 64 px floor is a pixel estimate and must not be passed back through px_to_display_units.
    """
    obj, _unused = _get_selected_graphic_object(model)
    if obj is None:
        return None, None
    size = visual_helpers.safe_get_property(obj, "Size")
    if size is None:
        size = obj.getSize()
    return visual_helpers.units_to_px(size.Width, size.Height, minimum=64)


def _graphic_from_object(obj: Any) -> Any | None:
    """Return the UNO Graphic for a text graphic or draw GraphicObjectShape."""
    return visual_helpers.graphic_from_object(obj)


# Bugfix: getattr(ctx, "getServiceManager", lambda: None)() evaluated the default
# callable even when ServiceManager existed, and assert statements could be stripped
# under Python -O optimizations. Context and ServiceManager availability are now
# validated with explicit RuntimeError checks.
def export_graphic_to_bytes(ctx: Any, graphic: Any) -> bytes:
    """Export a UNO Graphic to raw PNG bytes via GraphicProvider."""
    if ctx is None:
        # New infection start if we call uno.getComponentContext() here.
        # get_ctx() wraps the component context (Layer A) and is main-thread-only.
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
    if ctx is None:
        raise RuntimeError("export_graphic_to_bytes: no component context")
    sm = ctx.ServiceManager
    if sm is None:
        raise RuntimeError("export_graphic_to_bytes: ServiceManager is missing")
    gp = sm.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
    if gp is None:
        raise RuntimeError("export_graphic_to_bytes: GraphicProvider is missing")
    # Windows cannot reopen a still-open NamedTemporaryFile (sharing /
    # DeleteOnClose). mkstemp + unlink matches image_utils / payload_codec.
    fd, tmp_name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        tmp_url = uno.systemPathToFileUrl(tmp_name)
        props = (PropertyValue(Name="URL", Value=tmp_url), PropertyValue(Name="MimeType", Value="image/png"))
        gp.storeGraphic(graphic, props)
        with open(tmp_name, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def export_graphic_object_to_bytes(ctx: Any, obj: Any) -> bytes | None:
    """Export a graphic-bearing UNO object (GraphicObject or GraphicObjectShape) to PNG bytes."""
    graphic = _graphic_from_object(obj)
    if graphic is None:
        return None
    return export_graphic_to_bytes(ctx, graphic)


def get_selected_image_base64(model: Any, ctx: Any = None) -> str | None:
    """Returns the base64 encoded data of the currently selected image.

    Works for GraphicObject (Writer) or GraphicObjectShape (Calc/Draw).
    ctx: optional component context (e.g. from chat panel or MainJob). If None, uses get_ctx().
    Use the panel/MainJob ctx for Calc so context-dependent logic works correctly.
    """
    import base64

    # Use _selection_graphic_object which handles pyuno TextGraphicObject
    # correctly via supportsService / GraphicURL checks.
    obj = _selection_graphic_object(model)
    if obj is None:
        log.debug("get_selected_image_base64: no graphic in selection")
        return None
    png_bytes = export_graphic_object_to_bytes(ctx, obj)
    if png_bytes is None:
        return None
    return base64.b64encode(png_bytes).decode("utf-8")


# Bugfix: Adding files with the same basename from different directories to the
# gallery overwrote existing files in the gallery directory. We now compute a unique
# destination filename using a hash digest when a different file with the same basename
# already exists in the gallery.
def _gallery_dest(gallery_dir: Path, img_path: str) -> Path:
    filename = os.path.basename(img_path)
    if not filename or filename in (".", ".."):
        raise ValueError("image path has no filename")
    src_real = os.path.realpath(img_path)
    primary = gallery_dir / filename
    if not primary.exists() or os.path.realpath(str(primary)) == src_real:
        return primary
    stem, ext = os.path.splitext(filename)
    digest = hashlib.sha256(src_real.encode("utf-8")).hexdigest()[:8]
    return gallery_dir / f"{stem}-{digest}{ext}"


def add_image_to_gallery(ctx: Any, img_path: str) -> None:
    try:
        psettings = ctx.getValueByName("/singletons/com.sun.star.util.thePathSettings")
        gallery_dir = Path(uno.fileUrlToSystemPath(psettings.Storage_writable)) / GALLERY_IMAGE_DIR
        os.makedirs(gallery_dir, exist_ok=True)

        target_path = _gallery_dest(gallery_dir, img_path)
        if os.path.realpath(img_path) != os.path.realpath(str(target_path)):
            try:
                shutil.copy2(img_path, target_path)
            except shutil.SameFileError:
                pass

        themes_list = ctx.ServiceManager.createInstanceWithContext("com.sun.star.gallery.GalleryThemeProvider", ctx)

        if themes_list.hasByName(GALLERY_NAME):
            theme = themes_list.getByName(GALLERY_NAME)
        else:
            theme = themes_list.insertNewByName(GALLERY_NAME)

        theme.insertURLByIndex(uno.systemPathToFileUrl(str(target_path)), -1)
        theme.update()
    except Exception:
        log.exception("Failed to add to gallery")
