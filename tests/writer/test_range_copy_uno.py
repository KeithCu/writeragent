# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for range_copy view-cursor preservation."""

from typing import Any

import uno  # noqa: F401

from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.page import copy_page_style


@native_test
@with_native_doc("writer")
def test_page_style_clone_with_header_table_keeps_view_cursor_uno(ctx: Any, doc: Any) -> None:
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    text.insertString(cur, "First body paragraph.", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second body paragraph.", False)

    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    standard = page_styles.getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    header_text = standard.getPropertyValue("HeaderText")
    h_cur = header_text.createTextCursor()
    header_text.insertString(h_cur, "Header title", False)
    header_text.insertControlCharacter(h_cur, 0, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 2)
    header_text.insertTextContent(h_cur, tbl, False)

    vc = doc.getCurrentController().getViewCursor()
    vc.gotoStart(False)
    assert text.compareRegionStarts(vc.getStart(), text.getStart()) == 0

    copy_page_style(doc, "Standard", "CloneX")

    assert text.compareRegionStarts(vc.getStart(), text.getStart()) == 0
    assert text.compareRegionStarts(vc.getStart(), text.getEnd()) != 0

    clone = page_styles.getByName("CloneX")
    clone_header = clone.getPropertyValue("HeaderText")
    has_table = False
    enum = clone_header.createEnumeration()
    while enum_has_more(enum):
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.TextTable"):
            has_table = True
            break
    assert has_table


@native_test
@with_native_doc("writer")
def test_copy_flow_into_xtext_clones_para_table_para_structure_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.range_copy import copy_flow_into_xtext

    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    standard = page_styles.getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    standard.setPropertyValue("FooterIsOn", True)
    src = standard.getPropertyValue("HeaderText")
    dest = standard.getPropertyValue("FooterText")

    cur = src.createTextCursor()
    src.insertString(cur, "one", False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    src.insertTextContent(src.getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("cell")
    cur.gotoEnd(False)
    src.insertString(cur, "after", False)

    copy_flow_into_xtext(doc, src, dest)

    structure = []
    enum = dest.createEnumeration()
    while enum_has_more(enum):
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.TextTable"):
            structure.append("TextTable")
        else:
            structure.append(f"Paragraph:{el.getString()}")
    assert structure == ["Paragraph:one", "TextTable", "Paragraph:after"], structure


@native_test
@with_native_doc("writer")
def test_copy_page_style_drops_multi_para_deletion_and_handles_wholly_struck_uno(ctx: Any, doc: Any) -> None:
    from plugin.tests.testing_utils import strike_across_paragraphs

    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    standard = page_styles.getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    header = standard.getPropertyValue("HeaderText")
    strike_across_paragraphs(doc, header)

    copy_page_style(doc, "Standard", "ClonedPartial")
    cloned_header = page_styles.getByName("ClonedPartial").getPropertyValue("HeaderText")
    assert "gone" not in cloned_header.getString()
    assert "Keep" in cloned_header.getString()
    assert "After" in cloned_header.getString()

    # Wholly struck header: clones without raising and is empty.
    standard.setPropertyValue("FooterIsOn", True)
    footer = standard.getPropertyValue("FooterText")
    footer.setString("AllStruck")
    f_cur = footer.createTextCursor()
    f_cur.gotoStart(False)
    f_cur.gotoEnd(True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        f_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    copy_page_style(doc, "Standard", "ClonedEmptyFooter")
    cloned_footer = page_styles.getByName("ClonedEmptyFooter").getPropertyValue("FooterText")
    assert cloned_footer.getString().strip() == ""


@native_test
@with_native_doc("writer")
def test_copy_page_style_with_struck_tables_uno(ctx: Any, doc: Any) -> None:
    import base64
    import os
    import tempfile
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.page import _scan_region_content
    from plugin.writer.range_copy import supports_service

    def _has_table(text_obj: Any) -> bool:
        enum = text_obj.createEnumeration()
        while enum_has_more(enum):
            if supports_service(enum.nextElement(), "com.sun.star.text.TextTable"):
                return True
        return False

    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    standard = page_styles.getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    header = standard.getPropertyValue("HeaderText")

    # 1. Table holding only a Title field, cleared while recording -> clones empty without raising.
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), tbl, False)
    a1 = tbl.getCellByName("A1")
    a1.insertTextContent(a1.createTextCursor(), doc.createInstance("com.sun.star.text.textfield.docinfo.Title"), False)
    doc.setPropertyValue("RecordChanges", True)
    try:
        header.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)
    assert _has_table(header)

    copy_page_style(doc, "Standard", "CloneStruckTitleTable")
    cloned_header = page_styles.getByName("CloneStruckTitleTable").getPropertyValue("HeaderText")
    assert not _has_table(cloned_header)

    # 2. Struck logo table -> clone reports no skipped shapes.
    execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
    header.setString("")
    logo_tbl = doc.createInstance("com.sun.star.text.TextTable")
    logo_tbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), logo_tbl, False)
    logo_cell = logo_tbl.getCellByName("A1")
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    fd, img_path = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png_bytes)
        gfx = doc.createInstance("com.sun.star.text.TextGraphicObject")
        gfx.setPropertyValue("GraphicURL", "file://" + img_path)
        gfx.setPropertyValue("AnchorType", AS_CHARACTER)
        logo_cell.insertTextContent(logo_cell.createTextCursor(), gfx, False)
    finally:
        os.unlink(img_path)

    assert len(_scan_region_content(doc, header)["images"]) == 1
    doc.setPropertyValue("RecordChanges", True)
    try:
        header.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)
    assert _has_table(header)

    res = copy_page_style(doc, "Standard", "CloneStruckLogoTable")
    assert res["skipped_shapes"] == [], res


@native_test
@with_native_doc("writer")
def test_copy_page_style_preserves_input_field_in_cell_uno(ctx: Any, doc: Any) -> None:
    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    standard = page_styles.getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    header = standard.getPropertyValue("HeaderText")

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), tbl, False)
    a1 = tbl.getCellByName("A1")
    inp = doc.createInstance("com.sun.star.text.TextField.Input")
    inp.setPropertyValue("Content", "CellInput")
    inp.setPropertyValue("Hint", "Prompt")
    a1.insertTextContent(a1.createTextCursor(), inp, False)

    copy_page_style(doc, "Standard", "CloneInputCell")
    cloned_header = page_styles.getByName("CloneInputCell").getPropertyValue("HeaderText")
    enum = cloned_header.createEnumeration()
    cloned_tbl = None
    while enum_has_more(enum):
        el = enum.nextElement()
        if el.supportsService("com.sun.star.text.TextTable"):
            cloned_tbl = el
            break
    assert cloned_tbl is not None
    assert cloned_tbl.getCellByName("A1").getString() == "CellInput"
