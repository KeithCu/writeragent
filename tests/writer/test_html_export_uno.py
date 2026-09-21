# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Writer HTML range export (html_export)."""

from typing import Any

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import skip_windows_leftover_hidden_load, with_native_doc


@native_test
@with_native_doc("writer")
def test_range_export_keeps_bold_inside_odd_para_uno(ctx: Any, doc: Any) -> None:
    """Range export must still show emphasis when the source paragraph carries
    unusual Para* values. A refused Para* used to skip Char* paint entirely."""
    # GHA 34690797019: leftover writer reuse then
    # html_export._range_to_content_via_temp_doc Hidden `_default`
    # hung 30s on temp_doc.close(True) after XHTML. SkipTest still
    # runs finally — skip before the factory load.
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "plain ", False)
    cur.setPropertyValue("CharWeight", 150.0)
    text.insertString(cur, "BOLD", False)
    cur.setPropertyValue("CharWeight", 100.0)
    text.insertString(cur, " tail", False)

    para = text.createTextCursor()
    para.gotoStart(False)
    para.gotoEndOfParagraph(True)
    para.setPropertyValue("ParaLeftMargin", 5000)
    para.setPropertyValue("ParaFirstLineIndent", -2000)
    para.setPropertyValue("ParaBackColor", 0xFFE4C4)

    from plugin.writer.html_export import _range_to_content_via_temp_doc

    html = _range_to_content_via_temp_doc(doc, ctx, 0, 16, None, None)
    assert "BOLD" in html, html
    compact = html.lower().replace(" ", "")
    has_emphasis = (
        "<strong>" in compact
        or "<b>" in compact
        or "font-weight:bold" in compact
        or "font-weight:700" in compact
    )
    assert has_emphasis, html


@native_test
@with_native_doc("writer")
def test_copy_xtext_keeps_nested_text_table_uno(ctx: Any, doc: Any) -> None:
    """_copy_cell_xtext used to skip in-cell TextTables; dest must keep the nest."""
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    host = outer.getCellByName("B2")
    host.setString("EXPORT_CAPTION")
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 2)
    host.insertTextContent(host.getEnd(), nested, False)
    nested.setName("ExportNested")
    nested.getCellByName("A1").setString("NEST_A")
    nested.getCellByName("B1").setString("NEST_B")

    from plugin.writer.html_export import _copy_xtext_into_doc, _open_hidden_writer
    from plugin.writer.specialized.tables import TableGetCells, TableList
    from plugin.tests.testing_utils import TestingFactory

    temp_doc = None
    try:
        temp_doc = _open_hidden_writer(ctx)
        _copy_xtext_into_doc(doc, doc.getText(), temp_doc)
        tool_ctx = TestingFactory.create_context(doc=temp_doc, ctx=ctx, env="native")
        listed = TableList().execute(tool_ctx)
        assert listed.get("status") == "ok", listed
        assert listed["count"] >= 2, listed
        hosted = {}
        for entry in listed["tables"]:
            hosted.update(entry.get("nested_in_cells") or {})
        assert hosted, listed
        nested_name = next(iter(hosted.values()))[0]
        cells = TableGetCells().execute(tool_ctx, name=nested_name)
        assert cells["cells"]["A1"] == "NEST_A"
        assert cells["cells"]["B1"] == "NEST_B"
        parent_name = next(
            t["name"] for t in listed["tables"] if t.get("nested_in_cells")
        )
        parent_cells = TableGetCells().execute(tool_ctx, name=parent_name)
        assert "EXPORT_CAPTION" in (parent_cells["cells"].get("B2") or "")
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass


@native_test
@with_native_doc("writer")
def test_copy_table_keeps_merged_banner_d2_uno(ctx: Any, doc: Any) -> None:
    """HTML copy used range(cols); after A1:D1 merge that dropped D2."""
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    text = doc.getText()
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(5, 4)
    text.insertTextContent(text.getEnd(), table, False)
    table.setName("ExportMerged")
    table.getCellByName("A1").setString("Title")
    cursor = table.createCursorByCellName("A1")
    cursor.gotoCellByName("D1", True)
    cursor.mergeRange()
    table.getCellByName("D2").setString("Telèfon responsable")

    from plugin.writer.html_export import _copy_xtext_into_doc, _open_hidden_writer
    from plugin.writer.specialized.tables import TableGetCells, TableList
    from plugin.tests.testing_utils import TestingFactory

    temp_doc = None
    try:
        temp_doc = _open_hidden_writer(ctx)
        _copy_xtext_into_doc(doc, doc.getText(), temp_doc)
        tool_ctx = TestingFactory.create_context(doc=temp_doc, ctx=ctx, env="native")
        listed = TableList().execute(tool_ctx)
        assert listed.get("status") == "ok", listed
        assert listed["count"] >= 1, listed
        dest_name = listed["tables"][0]["name"]
        cells = TableGetCells().execute(tool_ctx, name=dest_name)
        assert cells.get("status") == "ok", cells
        assert cells["cells"].get("D2") == "Telèfon responsable", cells
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass
