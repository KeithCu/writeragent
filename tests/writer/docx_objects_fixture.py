# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared UNO fixture builder for docx rotated pictures and Word text boxes."""

from __future__ import annotations

import os
import tempfile

import uno
from com.sun.star.awt import Point, Size
from com.sun.star.beans import PropertyValue
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_PARAGRAPH


def _pv(name: str, value) -> PropertyValue:
    return PropertyValue(Name=name, Value=value)


def _fixture_image_path() -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(root, "tests", "images", "numpy-dog-photo.png")


def build_docx_objects_doc(ctx, doc):
    """Populate *doc* with a fly image, rotated draw pictures, a text box, and a header fly,
    save as .docx, reload with Hidden=True, and enable RecordChanges.

    Returns ``(reloaded_doc, docx_path)``. Callers must close *reloaded_doc* and unlink *docx_path*.
    """
    img_path = _fixture_image_path()
    assert os.path.isfile(img_path), f"fixture image missing: {img_path}"
    img_url = uno.systemPathToFileUrl(img_path)

    gp = ctx.getServiceManager().createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
    graphic = gp.queryGraphic((_pv("URL", img_url),))

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Body paragraph one.\n", False)

    fly = doc.createInstance("com.sun.star.text.TextGraphicObject")
    fly.setName("Image 1")
    fly.setPropertyValue("Graphic", graphic)
    fly.setPropertyValue("AnchorType", AS_CHARACTER)
    fly.setPropertyValue("Width", 2000)
    fly.setPropertyValue("Height", 2000)
    text.insertTextContent(cur, fly, False)
    text.insertString(cur, "\n", False)

    for shape_name in ("Image 1", "Image 11"):
        shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
        shape.setName(shape_name)
        shape.setPropertyValue("Graphic", graphic)
        shape.setPropertyValue("AnchorType", AS_CHARACTER)
        shape.setSize(Size(2000, 2000))
        shape.setPropertyValue("RotateAngle", 900)
        text.insertTextContent(cur, shape, False)
        text.insertString(cur, "\n", False)

    tbox = doc.createInstance("com.sun.star.drawing.RectangleShape")
    tbox.setName("Text Box 2")
    tbox.setSize(Size(4000, 2000))
    tbox.setPosition(Point(1000, 1000))
    text.insertTextContent(cur, tbox, False)
    tbox.setPropertyValue("TextBox", True)
    tbox.setString("Inside the text box")

    text.insertString(cur, "\nBody paragraph two.\n", False)

    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    std_style = page_styles.getByName("Standard")
    std_style.setPropertyValue("HeaderIsOn", True)
    header_text = std_style.getPropertyValue("HeaderText")
    hcur = header_text.createTextCursor()
    hfly = doc.createInstance("com.sun.star.text.TextGraphicObject")
    hfly.setName("HeaderLogo")
    hfly.setPropertyValue("Graphic", graphic)
    hfly.setPropertyValue("AnchorType", AS_CHARACTER)
    hfly.setPropertyValue("Width", 1000)
    hfly.setPropertyValue("Height", 1000)
    header_text.insertTextContent(hcur, hfly, False)

    fd, docx_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    docx_url = uno.systemPathToFileUrl(docx_path)
    doc.storeToURL(docx_url, (_pv("FilterName", "MS Word 2007 XML"),))

    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    reloaded = desktop.loadComponentFromURL(
        docx_url, "_blank", 0, (_pv("Hidden", True),)
    )
    reloaded.setPropertyValue("RecordChanges", True)
    return reloaded, docx_path


def build_grouped_text_boxes(ctx, doc, anchor_type=None):
    """Build a group of two Word text boxes, save as .docx, and reload with Hidden=True.

    Returns ``(reloaded_doc, docx_path)``. Callers must close *reloaded_doc* and unlink *docx_path*.
    """
    sm = ctx.getServiceManager()
    page = doc.getDrawPage()
    collection = sm.createInstanceWithContext("com.sun.star.drawing.ShapeCollection", ctx)

    for idx, x in enumerate((1000, 5000)):
        shape = doc.createInstance("com.sun.star.drawing.CustomShape")
        shape.setName(f"S{idx}")
        shape.setPropertyValue("AnchorType", AT_PARAGRAPH)
        shape.setSize(Size(3000, 2000))
        shape.setPosition(Point(x, 1000))
        page.add(shape)
        collection.add(shape)

    group = page.group(collection)
    group.setName("Grp")
    for idx in range(group.getCount()):
        child = group.getByIndex(idx)
        child.setPropertyValue("TextBox", True)
        child.setString(f"in box {idx}")

    if anchor_type is not None:
        group.setPropertyValue("AnchorType", anchor_type)

    fd, docx_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    docx_url = uno.systemPathToFileUrl(docx_path)
    doc.storeToURL(docx_url, (_pv("FilterName", "MS Word 2007 XML"),))

    desktop = sm.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    reloaded = desktop.loadComponentFromURL(docx_url, "_blank", 0, (_pv("Hidden", True),))
    return reloaded, docx_path
