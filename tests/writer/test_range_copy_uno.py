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
