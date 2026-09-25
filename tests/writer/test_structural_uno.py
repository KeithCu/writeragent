# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.structural import GetPageObjects


@native_test
@with_native_doc("writer")
def test_structural_tools_execution(ctx, doc):
    mock_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")

    # Test BookmarkList via registry
    from plugin.main import get_tools
    registry = get_tools()
    list_bm_tool = registry.get("bookmark_list")
    assert list_bm_tool is not None, "list_bookmarks tool not found in registry"
    
    bm_res = list_bm_tool.execute(mock_ctx)
    assert bm_res["status"] == "ok", f"BookmarkList failed: {bm_res}"
    assert isinstance(bm_res["bookmarks"], list), "BookmarkList should return a list"

    list_sec_tool = registry.get("section_list")
    assert list_sec_tool is not None, "list_sections (structural domain) should be registered"
    sec_res = list_sec_tool.execute(mock_ctx)
    assert sec_res["status"] == "ok", f"SectionList failed: {sec_res}"
    assert isinstance(sec_res["sections"], list), "SectionList should return a list"


@native_test
@with_native_doc("writer")
def test_get_page_objects_with_table_at_page_end_uno(ctx, doc):
    """Regression: get_page_objects from a table-cell cursor.

    Cloning the view cursor through doc.getText() after jumpToEndOfPage used to raise
    UNO RuntimeException "End of content node doesn't have the proper start node".
    lockControllers() made gotoRange/getPage fail when the cursor started in a cell;
    leave via body getStart() first, unlock before restore. Table-anchor hops while
    locked leave getPage() at 0 — that is stale layout, not an empty page.
    Must list the outer table and a nested table, then restore the cell cursor.
    """
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(6, 3)
    text.insertTextContent(text.getEnd(), tbl, False)
    for name in tbl.getCellNames():
        tbl.getCellByName(name).setString("cell " + name)
    inner = doc.createInstance("com.sun.star.text.TextTable")
    inner.initialize(2, 2)
    host = tbl.getCellByName("B2")
    host.insertTextContent(host.getStart(), inner, False)
    for name in inner.getCellNames():
        inner.getCellByName(name).setString("inner " + name)

    cell = tbl.getCellByName("A1")
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(cell, False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = GetPageObjects().execute(tool_ctx, page=1)
    assert res.get("status") == "ok", res
    names = [t.get("name") for t in res.get("tables") or []]
    assert tbl.getName() in names, res
    assert inner.getName() in names, res
    # Save/restore must leave the view cursor in the nested cell XText.
    restored = vc.getPropertyValue("TextTable")
    assert restored is not None
    assert restored.getName() == tbl.getName()
