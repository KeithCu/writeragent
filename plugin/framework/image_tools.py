"""Image insertion and gallery management tools for LibreOffice."""
import os
import shutil
import logging
import uno
import unohelper
from pathlib import Path
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_FRAME
from com.sun.star.awt import Size, Point
from com.sun.star.beans import PropertyValue
from com.sun.star.beans.PropertyAttribute import TRANSIENT

logger = logging.getLogger(__name__)

GALLERY_NAME = "localwriter_images"
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
    image = model.createInstance("com.sun.star.text.GraphicObject")
    image.GraphicURL = uno.systemPathToFileUrl(img_path)
    image.AnchorType = AS_CHARACTER
    image.Width = width
    image.Height = height
    image.Title = title
    image.Description = description
    
    view_cursor = model.CurrentController.ViewCursor

    if add_frame:
        _insert_frame(model, view_cursor, image, width, height, title)
    else:
        try:
            model.Text.insertTextContent(view_cursor, image, False)
        except Exception:
            # Fallback if cursor position is invalid (e.g. inside a field)
            view_cursor.jumpToStartOfPage()
            model.Text.insertTextContent(view_cursor, image, False)

def _insert_frame(model, cursor, image, width, height, title):
    text_frame = model.createInstance("com.sun.star.text.TextFrame")
    frame_size = Size()
    frame_size.Height = height + 150 # Small padding for title
    frame_size.Width = width + 150
    text_frame.setSize(frame_size)
    text_frame.setPropertyValue("AnchorType", AT_FRAME)
    
    try:
        model.getText().insertTextContent(cursor, text_frame, False)
    except Exception:
        cursor.jumpToStartOfPage()
        model.getText().insertTextContent(cursor, text_frame, False)

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

    draw_page.add(image) # LOSHD uses addTop, but add is standard
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
    except Exception:
        return None, None


def replace_image_in_place(ctx, model, img_path, width_px, height_px, title="", description="", add_to_gallery=True, add_frame=False):
    """
    If the current selection is a single graphic, replace it with the new image and return True.
    Otherwise return False (caller should fall back to insert_image).
    """
    obj, inside = _get_selected_graphic_object(model)
    if obj is None:
        return False
    width_units = int(width_px * 25.4)
    height_units = int(height_px * 25.4)
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
            size = obj.getSize()
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
            except:
                return None
        else:
            return None

        # Export graphic to base64
        # We use a GraphicProvider to export as PNG/JPG
        import base64
        import tempfile
        
        if ctx is None:
            ctx = uno.getComponentContext()
        gp = ctx.ServiceManager.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp_url = uno.systemPathToFileUrl(tmp.name)
            props = (
                PropertyValue(Name="URL", Value=tmp_url),
                PropertyValue(Name="MimeType", Value="image/png")
            )
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
        
        themes_list = ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.gallery.GalleryThemeProvider", ctx
        )
        
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
