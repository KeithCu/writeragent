# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for the pooled Writer document wipe in tests/testing_utils.py."""

from __future__ import annotations

from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, execute_calc_tool

_USER_STYLE = "WipeCheckUserStyle"


def _dirty_writer_doc(doc) -> None:
    """Leave the body, styles, page header and change tracking in a state tests commonly leave."""
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "first paragraph", False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(2, 2)
    text.insertTextContent(cur, table, False)
    table.getCellByName("A1").setString("cell")
    cur.gotoEnd(False)
    text.insertString(cur, "after table", False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    doc.setPropertyValue("RecordChanges", True)
    text.insertString(cur, "tracked insertion", False)
    cur.gotoStartOfParagraph(True)
    cur.setPropertyValue("ParaStyleName", "Heading 1")
    cur.setPropertyValue("NumberingStyleName", "Numbering 123")
    cur.setPropertyValue("CharStyleName", "Emphasis")
    cur.setPropertyValue("CharWeight", 150.0)
    cur.setPropertyValue("CharStrikeout", 1)
    # An untracked table of contents built from the outline: its entries sit in sections that
    # deleting the body empties and the wipe then removes.
    doc.setPropertyValue("RecordChanges", False)
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    toc = doc.createInstance("com.sun.star.text.ContentIndex")
    toc.setPropertyValue("CreateFromOutline", True)
    text.insertTextContent(cur, toc, False)
    toc.update()
    doc.setPropertyValue("RecordChanges", True)

    families = doc.getStyleFamilies()
    page = families.getByName("PageStyles").getByName("Standard")
    page.setPropertyValue("HeaderIsOn", True)
    page.setPropertyValue("FirstIsShared", False)
    page.getPropertyValue("HeaderText").setString("leftover header")
    page.getPropertyValue("HeaderTextFirst").setString("leftover first-page header")

    para_styles = families.getByName("ParagraphStyles")
    para_styles.insertByName(_USER_STYLE, doc.createInstance("com.sun.star.style.ParagraphStyle"))
    para_styles.getByName("Standard").setPropertyValue("CharWeight", 150.0)


@native_test
def test_pooled_writer_wipe_leaves_next_document_clean(ctx):
    with TestingFactory.native_doc(ctx, doc_type="writer") as doc:
        _dirty_writer_doc(doc)

    with TestingFactory.native_doc(ctx, doc_type="writer") as reused:
        # A wipe that leaves the pool dirty makes native_doc close it and open a new document.
        assert reused is doc
        assert reused.getText().getString() == ""
        assert not reused.getDocumentIndexes().hasElements()
        assert not reused.getTextSections().hasElements()
        assert not reused.getTextTables().hasElements()
        assert not reused.getRedlines().createEnumeration().hasMoreElements()
        assert reused.getPropertyValue("RecordChanges") is False

        cur = reused.getText().createTextCursor()
        cur.gotoStart(False)
        assert cur.getPropertyValue("ParaStyleName") == "Standard"
        assert cur.getPropertyValue("NumberingStyleName") == ""
        assert cur.getPropertyValue("CharStyleName") == ""
        assert cur.getPropertyValue("CharWeight") == 100.0
        assert cur.getPropertyValue("CharStrikeout") == 0

        families = reused.getStyleFamilies()
        page = families.getByName("PageStyles").getByName("Standard")
        assert page.getPropertyValue("FirstIsShared") is True
        assert page.getPropertyValue("HeaderText").getString() == ""
        para_styles = families.getByName("ParagraphStyles")
        assert not para_styles.hasByName(_USER_STYLE)
        assert para_styles.getByName("Standard").getPropertyValue("CharWeight") == 100.0


@native_test
def test_pooled_calc_wipe_drops_pivot_tables(ctx):
    with TestingFactory.native_doc(ctx, doc_type="calc") as doc:
        sn = doc.getSheets().getByIndex(0).getName()
        res_write = execute_calc_tool(
            doc,
            ctx,
            "write_range",
            {
                "range": "A1:B4",
                "values": [
                    ["Month", "Sales"],
                    ["Jan", "100"],
                    ["Feb", "150"],
                    ["Mar", "200"],
                ],
            },
        )
        assert res_write.get("status") == "ok", f"write_range failed: {res_write}"
        res_pivot = execute_calc_tool(
            doc,
            ctx,
            "pivot_table_create",
            {
                "name": "WA_WipePivot",
                "source_range": "A1:B4",
                "source_sheet": sn,
                "destination_sheet": sn,
                "destination_cell": "D1",
                "row_fields": ["Month"],
                "column_fields": [],
                "data_fields": ["Sales"],
                "page_fields": [],
            },
        )
        assert res_pivot.get("status") == "ok", f"pivot_table_create failed: {res_pivot}"

    with TestingFactory.native_doc(ctx, doc_type="calc") as reused:
        assert reused is doc
        sheets = reused.getSheets()
        for i in range(sheets.getCount()):
            assert sheets.getByIndex(i).getDataPilotTables().getElementNames() == ()
        assert sheets.getByIndex(0).getCellRangeByName("D1").getString() == ""
