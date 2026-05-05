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
import uno
from pathlib import Path
from typing import Any, cast
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_FRAME
from com.sun.star.awt import Size, Point
from com.sun.star.beans import PropertyValue

logger = logging.getLogger(__name__)

GALLERY_NAME = "writeragent_images"
GALLERY_IMAGE_DIR = GALLERY_NAME


def get_type_doc(doc):
    TYPE_DOC = {
        "calc": "com.sun.star.sheet.SpreadsheetDocument",
        "draw": "com.sun.star.drawing.DrawingDocument",
        "impress": "com.sun.star.presentation.PresentationDocument",
        "web": "com.sun.star.text.WebDocument",
        "writer": "com.sun.star.text.TextDocument",
    }
    for k, v in TYPE_DOC.items():
        if doc.supportsService(v):
            return k
    return "writer"


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
        _insert_image_to_writer(model, img_path, width_units, height_units, title, description, add_frame)
    else:
        _insert_image_to_drawpage(model, inside, img_path, width_units, height_units, title, description)

    if add_to_gallery:
        add_image_to_gallery(ctx, img_path, f"{title}\n\n{description}")


def _insert_image_to_writer(model, img_path, width, height, title, description, add_frame):
    doc_text = model.getText()
    image = model.createInstance("com.sun.star.text.GraphicObject")
    image.GraphicURL = uno.systemPathToFileUrl(img_path)
    image.AnchorType = AS_CHARACTER
    image.Width = width
    image.Height = height
    image.Title = title
    image.Description = description

    view_cursor = model.CurrentController.ViewCursor

    def to_text_cursor(vc):
        # LibreOffice's Text.insertTextContent expects a TextCursor tied to the
        # document's text (not a ViewCursor from the controller).
        return doc_text.createTextCursorByRange(vc.getStart())

    if add_frame:
        _insert_frame(model, view_cursor, image, width, height, title)
    else:
        try:
            text_cursor = to_text_cursor(view_cursor)
            doc_text.insertTextContent(text_cursor, image, False)
        except Exception as e:
            # Fallback if cursor position is invalid (e.g. inside a field)
            logger.debug("_insert_inline_image insertTextContent fallback: %s", e)
            view_cursor.jumpToStartOfPage()
            text_cursor = to_text_cursor(view_cursor)
            doc_text.insertTextContent(text_cursor, image, False)


def _insert_frame(model, cursor, image, width, height, title):
    doc_text = model.getText()
    text_frame = model.createInstance("com.sun.star.text.TextFrame")
    frame_size = Size()
    frame_size.Height = height + 150  # Small padding for title
    frame_size.Width = width + 150
    text_frame.setSize(frame_size)
    text_frame.setPropertyValue("AnchorType", AT_FRAME)

    try:
        # `cursor` comes from the view layer; convert to a TextCursor.
        text_cursor = doc_text.createTextCursorByRange(cursor.getStart())
        doc_text.insertTextContent(text_cursor, text_frame, False)
    except Exception as e:
        logger.debug("_insert_frame insertTextContent fallback: %s", e)
        cursor.jumpToStartOfPage()
        text_cursor = doc_text.createTextCursorByRange(cursor.getStart())
        doc_text.insertTextContent(text_cursor, text_frame, False)

    frame_text = text_frame.getText()
    frame_cursor = frame_text.createTextCursor()
    text_frame.insertTextContent(frame_cursor, image, False)
    if title:
        frame_text.insertString(frame_cursor, "\n" + title, False)


def _insert_image_to_drawpage(model, inside, img_path, width, height, title, description):
    image = model.createInstance("com.sun.star.drawing.GraphicObjectShape")
    image.GraphicURL = uno.systemPathToFileUrl(img_path)

    ctrllr = model.CurrentController
    if inside == "calc":
        draw_page = ctrllr.ActiveSheet.DrawPage
    else:
        draw_page = ctrllr.CurrentPage

    draw_page.add(image)  # LOSHD uses addTop, but add is standard
    image.setSize(Size(width, height))
    image.Title = title
    image.Description = description

    # Center it roughly
    if inside != "calc":
        pos = Point((draw_page.Width - width) // 2, (draw_page.Height - height) // 2)
        image.setPosition(pos)


def _get_selected_graphic_object(model):
    """
    If the current selection is a single graphic, return (content, doc_type).
    Writer: content is XTextContent (GraphicObject); Calc/Draw: content is XShape (GraphicObjectShape).
    Otherwise return (None, None).
    """
    try:
        selection = model.CurrentController.Selection
        if not selection:
            return None, None
        if hasattr(selection, "getCount") and selection.getCount() != 1:
            return None, None
        obj = selection.getByIndex(0) if hasattr(selection, "getByIndex") else selection
        if not (hasattr(obj, "Graphic") or (hasattr(obj, "getPropertyValue") and obj.getPropertyValue("Graphic"))):
            return None, None
        inside = get_type_doc(model)
        return obj, inside
    except Exception as e:
        logger.debug("_get_selected_graphic_object error: %s", e)
        return None, None


def replace_image_in_place(ctx, model, img_path, width_px, height_px, title="", description="", add_to_gallery=True, add_frame=False):
    """
    If the current selection is a single graphic, replace it with the new image and return True.
    Otherwise return False (caller should fall back to insert_image).
    """
    obj, inside = _get_selected_graphic_object(model)
    if obj is None:
        return False
    # Match insert_image: 1 px at 96 DPI ≈ 26.46 units (1/100 mm)
    width_units = int(width_px * 26.46)
    height_units = int(height_px * 26.46)
    try:
        if inside in ["writer", "web"]:
            # Writer: insert new image at anchor of old, then remove old
            anchor = obj.getAnchor()
            if anchor is None:
                return False
            new_image = model.createInstance("com.sun.star.text.GraphicObject")
            new_image.GraphicURL = uno.systemPathToFileUrl(img_path)
            new_image.AnchorType = AS_CHARACTER
            new_image.Width = width_units
            new_image.Height = height_units
            new_image.Title = title
            new_image.Description = description
            model.getText().insertTextContent(anchor, new_image, False)
            model.getText().removeTextContent(obj)
        else:
            # Calc/Draw: add new shape at same position/size, then remove old
            ctrllr = model.CurrentController
            draw_page = ctrllr.ActiveSheet.DrawPage if inside == "calc" else ctrllr.CurrentPage
            pos = obj.getPosition()
            obj.getSize()
            new_image = model.createInstance("com.sun.star.drawing.GraphicObjectShape")
            new_image.GraphicURL = uno.systemPathToFileUrl(img_path)
            new_image.setPosition(pos)
            new_image.setSize(Size(width_units, height_units))
            new_image.Title = title
            new_image.Description = description
            draw_page.add(new_image)
            draw_page.remove(obj)
        if add_to_gallery:
            add_image_to_gallery(ctx, img_path, f"{title}\n\n{description}")
        return True
    except Exception as e:
        logger.error(f"Replace image in place failed: {e}")
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
        import tempfile

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
        # Update metadata of the last inserted item
        # insertURLByIndex returns a boolean in some versions, or index.
        # LO API says it's boolean for success.
        theme.update()
        # Find the item we just added (usually at the end or start depending on sort)
        # For simplicity, we'll just name it here if we can find a way to get the index.
        # theme.getByIndex(theme.Count - 1).Title = title
    except Exception as e:
        logger.error(f"Failed to add to gallery: {e}")
