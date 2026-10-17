# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for notebook controls and page style resolution."""

from __future__ import annotations

from plugin.notebook.notebook_controls import _page_style, _text_area_width_units
from plugin.testing_runner import native_test, show_window
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("writer", hidden=not show_window)
def test_page_style_follows_first_paragraph_not_document(ctx, doc):
    """Code-field sizing reads PageDescName on the first paragraph.

    SwXTextDocument has no PageDescName. A distinct page style on the first
    paragraph must win over Standard.
    """
    families = doc.getStyleFamilies().getByName("PageStyles")
    doc_read_failed = False
    try:
        doc.getPropertyValue("PageDescName")
    except Exception:
        doc_read_failed = True
    assert doc_read_failed, "text document model must not expose PageDescName"

    standard_name = None
    for candidate in ("Standard", "Default", "Default Page Style"):
        if families.hasByName(candidate):
            standard_name = candidate
            break
    assert standard_name is not None
    standard = families.getByName(standard_name)
    std_area = (
        int(standard.getPropertyValue("Width"))
        - int(standard.getPropertyValue("LeftMargin"))
        - int(standard.getPropertyValue("RightMargin"))
    )

    style_name = "WA_NB_PageDesc"
    if families.hasByName(style_name):
        families.removeByName(style_name)
    custom = doc.createInstance("com.sun.star.style.PageStyle")
    assert custom is not None
    families.insertByName(style_name, custom)
    custom.setPropertyValue("Width", 30000)
    custom.setPropertyValue("Height", 20000)
    custom.setPropertyValue("LeftMargin", 500)
    custom.setPropertyValue("RightMargin", 500)

    cursor = doc.getText().createTextCursor()
    cursor.gotoStart(False)
    cursor.setPropertyValue("PageDescName", style_name)
    assert str(cursor.getPropertyValue("PageDescName") or "") == style_name

    resolved = _page_style(doc)
    assert resolved is not None
    assert int(resolved.getPropertyValue("Width")) == 30000
    area = _text_area_width_units(doc)
    expected = max(
        6000,
        int(resolved.getPropertyValue("Width"))
        - int(resolved.getPropertyValue("LeftMargin"))
        - int(resolved.getPropertyValue("RightMargin")),
    )
    assert area == expected
    assert area != std_area
