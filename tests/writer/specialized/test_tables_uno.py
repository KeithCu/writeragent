# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO coverage for nested Writer text-table relationships."""
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.tables import (
    ManageTableStructure,
    TableDelete,
    TableGetCells,
    TableInsert,
    TableList,
    TableSetCell,
)


@native_test
@with_native_doc("writer")
def test_table_get_cells_reports_nested_parent_relation_uno(ctx, doc):
    """A nested table reports its direct parent table/cell; a top-level table does not."""
    text = doc.getText()

    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("FixtureOuter")
    outer.getCellByName("A1").setString("OUTER_ALPHA")

    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(2, 2)
    host = outer.getCellByName("B2")
    host.insertTextContent(host.getStart(), nested, False)
    nested.setName("FixtureNested")
    nested.getCellByName("A1").setString("NESTED_ALPHA")
    nested.getCellByName("B2").setString("NESTED_OMEGA")

    standalone = doc.createInstance("com.sun.star.text.TextTable")
    standalone.initialize(1, 2)
    text.insertTextContent(text.getEnd(), standalone, False)
    standalone.setName("FixtureStandalone")
    standalone.getCellByName("A1").setString("STANDALONE_ALPHA")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    nested_res = TableGetCells().execute(tool_ctx, name="FixtureNested")
    assert nested_res.get("status") == "ok", nested_res
    assert "matrix" not in nested_res
    assert nested_res["cells"]["A1"] == "NESTED_ALPHA"
    assert nested_res["cells"]["B2"] == "NESTED_OMEGA"
    assert nested_res["nesting"] == {
        "is_nested": True,
        "parent_table": "FixtureOuter",
        "parent_cell": "B2",
    }
    assert nested_res["nested_in_cells"] == {}

    standalone_res = TableGetCells().execute(tool_ctx, name="FixtureStandalone")
    assert standalone_res.get("status") == "ok", standalone_res
    assert standalone_res["cells"]["A1"] == "STANDALONE_ALPHA"
    assert "matrix" not in standalone_res
    assert standalone_res["nesting"] == {
        "is_nested": False,
        "parent_table": None,
        "parent_cell": None,
    }

    outer_res = TableGetCells().execute(tool_ctx, name="FixtureOuter")
    assert outer_res.get("status") == "ok", outer_res
    assert outer_res["nesting"] == {
        "is_nested": False,
        "parent_table": None,
        "parent_cell": None,
    }
    assert outer_res["nested_in_cells"] == {"B2": ["FixtureNested"]}
    # Host slot is host paragraphs only — not NESTED_ALPHA concatenated in.
    assert "NESTED_ALPHA" not in (outer_res["cells"].get("B2") or "")
    assert "matrix" not in outer_res

    listed = TableList().execute(tool_ctx)
    assert listed.get("status") == "ok", listed
    by = {t["name"]: t for t in listed["tables"]}
    assert by["FixtureNested"]["nesting"] == nested_res["nesting"]
    assert by["FixtureOuter"]["nested_in_cells"] == outer_res["nested_in_cells"]
    assert by["FixtureStandalone"]["nesting"]["is_nested"] is False

    wipe = TableSetCell().execute(tool_ctx, name="FixtureOuter", cell="B2", text="HOST_CAPTION")
    assert wipe.get("status") == "ok", wipe
    assert nested.getName() == "FixtureNested"
    assert nested.getCellByName("A1").getString() == "NESTED_ALPHA"

    del_row = ManageTableStructure().execute(
        tool_ctx, action="delete", axis="row", name="FixtureOuter", index=1
    )
    assert del_row.get("status") == "error" and "FixtureNested" in del_row.get("message", ""), del_row
    assert outer.getRows().getCount() == 2


@native_test
@with_native_doc("writer")
def test_table_insert_parent_cell_nests_and_table_delete_removes_uno(ctx, doc):
    """table_insert(parent, cell) nests; table_delete removes nested and top-level tables."""
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("InsertOuter")
    outer.getCellByName("A1").setString("KEEP_HOST")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    nested = TableInsert().execute(
        tool_ctx, rows=2, columns=2, parent="InsertOuter", cell="B2",
        data=[["N1", "N2"], ["N3", "N4"]],
    )
    assert nested.get("status") == "ok", nested
    nested_name = nested["table_name"]
    assert nested_name
    assert nested["nesting"] == {
        "is_nested": True,
        "parent_table": "InsertOuter",
        "parent_cell": "B2",
    }
    assert doc.getTextTables().hasByName(nested_name)
    listed = TableList().execute(tool_ctx)
    by = {t["name"]: t for t in listed["tables"]}
    assert by[nested_name]["nesting"] == nested["nesting"]
    assert by["InsertOuter"]["nested_in_cells"] == {"B2": [nested_name]}
    cells = TableGetCells().execute(tool_ctx, name=nested_name)
    assert cells["cells"]["A1"] == "N1" and cells["cells"]["B2"] == "N4"
    assert "matrix" not in cells

    deleted = TableDelete().execute(tool_ctx, name=nested_name)
    assert deleted.get("status") == "ok", deleted
    assert not doc.getTextTables().hasByName(nested_name)
    assert doc.getTextTables().hasByName("InsertOuter")
    after = TableList().execute(tool_ctx)
    after_by = {t["name"]: t for t in after["tables"]}
    assert after_by["InsertOuter"]["nested_in_cells"] == {}
    # Host cell is writable again once the nested table is gone.
    set_host = TableSetCell().execute(tool_ctx, name="InsertOuter", cell="B2", text="host-again")
    assert set_host.get("status") == "ok", set_host

    top = TableInsert().execute(tool_ctx, rows=1, columns=2)
    assert top.get("status") == "ok", top
    top_name = top["table_name"]
    assert top["nesting"]["is_nested"] is False
    assert doc.getTextTables().hasByName(top_name)
    gone = TableDelete().execute(tool_ctx, name=top_name)
    assert gone.get("status") == "ok", gone
    assert not doc.getTextTables().hasByName(top_name)
    assert doc.getTextTables().hasByName("InsertOuter")


@native_test
@with_native_doc("writer")
def test_table_set_cell_host_keeps_nested_table_uno(ctx, doc):
    """table_set_cell on a host cell rewrites the caption and leaves the nested table."""
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("HostOuter")
    host = outer.getCellByName("B2")
    host.setString("OLD_CAPTION")
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    host.insertTextContent(host.getEnd(), nested, False)
    nested.setName("HostNested")
    nested.getCellByName("A1").setString("INNER")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    set_host = TableSetCell().execute(
        tool_ctx, name="HostOuter", cell="B2", text="NEW_CAPTION"
    )
    assert set_host.get("status") == "ok", set_host
    assert doc.getTextTables().hasByName("HostNested")
    assert nested.getCellByName("A1").getString() == "INNER"
    cells = TableGetCells().execute(tool_ctx, name="HostOuter")
    assert cells["cells"]["B2"] == "NEW_CAPTION"
    assert "matrix" not in cells
    assert cells["nested_in_cells"] == {"B2": ["HostNested"]}


@native_test
@with_native_doc("writer")
def test_table_in_frame_not_in_cell_xtext_is_not_hosted_uno(ctx, doc):
    """A TextFrame inserted into a cell is not in that cell's XText (probed).

    The frame is anchored to the cell, but the cell enum is Paragraph only
    and portions have TextFrame=None. table_list therefore does not nest it.
    table_set_cell uses setString, which destroys the anchored frame and the
    table inside it. In-cell frames that *do* appear as as-character portions
    are covered by the unit walk.
    """
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("FrameOuter")
    host = outer.getCellByName("B2")
    frame = doc.createInstance("com.sun.star.text.TextFrame")
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

    frame.setPropertyValue("AnchorType", AS_CHARACTER)
    frame.setPropertyValue("Width", 5000)
    frame.setPropertyValue("Height", 3000)
    host.insertTextContent(host.getEnd(), frame, False)
    inner = doc.createInstance("com.sun.star.text.TextTable")
    inner.initialize(1, 1)
    frame.getText().insertTextContent(frame.getText().getEnd(), inner, False)
    inner.setName("FrameNested")
    inner.getCellByName("A1").setString("FRAMED")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    listed = TableList().execute(tool_ctx)
    assert listed.get("status") == "ok", listed
    by = {t["name"]: t for t in listed["tables"]}
    assert by["FrameNested"]["nesting"]["is_nested"] is False
    assert by["FrameOuter"]["nested_in_cells"] == {}
    wipe = TableSetCell().execute(tool_ctx, name="FrameOuter", cell="B2", text="wipe")
    assert wipe.get("status") == "ok", wipe
    assert not doc.getTextTables().hasByName("FrameNested")


@native_test
@with_native_doc("writer")
def test_nested_table_anchor_text_properties_uno(ctx, doc):
    """Anchor text exposes CellName and cursor TextTable for nested tables only."""
    text = doc.getText()
    # 1. Body table: anchor text has no CellName
    body_table = doc.createInstance("com.sun.star.text.TextTable")
    body_table.initialize(2, 2)
    text.insertTextContent(text.getEnd(), body_table, False)
    body_table.setName("BodyTable")
    body_anchor_text = body_table.getAnchor().getText()
    assert not hasattr(body_anchor_text, "CellName") or not body_anchor_text.CellName

    # 2. Host and nested table in B2
    host_table = doc.createInstance("com.sun.star.text.TextTable")
    host_table.initialize(2, 2)
    text.insertTextContent(text.getEnd(), host_table, False)
    host_table.setName("HostTable")
    b2_cell = host_table.getCellByName("B2")

    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    b2_cell.insertTextContent(b2_cell.getEnd(), nested, False)
    nested.setName("NestedTable")

    nested_anchor_text = nested.getAnchor().getText()
    assert getattr(nested_anchor_text, "CellName", None) == "B2"
    cur = nested_anchor_text.createTextCursor()
    assert getattr(cur, "TextTable", None) is not None
    assert cur.TextTable.getName() == "HostTable"

    # 3. Frame-nested table: anchor text has no CellName
    frame = doc.createInstance("com.sun.star.text.TextFrame")
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

    frame.setPropertyValue("AnchorType", AS_CHARACTER)
    frame.setPropertyValue("Width", 5000)
    frame.setPropertyValue("Height", 3000)
    b2_cell.insertTextContent(b2_cell.getEnd(), frame, False)
    frame_table = doc.createInstance("com.sun.star.text.TextTable")
    frame_table.initialize(1, 1)
    frame.getText().insertTextContent(frame.getText().getEnd(), frame_table, False)
    frame_table.setName("FrameTable")
    frame_anchor_text = frame_table.getAnchor().getText()
    assert not hasattr(frame_anchor_text, "CellName") or not frame_anchor_text.CellName

    # 4. Nested table in D2 after A1:D1 merge: parent cell is still D2
    merged_table = doc.createInstance("com.sun.star.text.TextTable")
    merged_table.initialize(5, 4)
    text.insertTextContent(text.getEnd(), merged_table, False)
    merged_table.setName("MergedTable")
    _merge_a1_d1(merged_table)
    d2_cell = merged_table.getCellByName("D2")
    d2_nested = doc.createInstance("com.sun.star.text.TextTable")
    d2_nested.initialize(1, 1)
    d2_cell.insertTextContent(d2_cell.getEnd(), d2_nested, False)
    d2_nested.setName("D2Nested")
    d2_anchor_text = d2_nested.getAnchor().getText()
    assert getattr(d2_anchor_text, "CellName", None) == "D2"
    d2_cur = d2_anchor_text.createTextCursor()
    assert getattr(d2_cur, "TextTable", None) is not None
    assert d2_cur.TextTable.getName() == "MergedTable"


def _merge_a1_d1(table):
    """Merge the first row into one banner cell (discussion #821 fixture)."""
    cursor = table.createCursorByCellName("A1")
    cursor.gotoCellByName("D1", True)
    cursor.mergeRange()


@native_test
@with_native_doc("writer")
def test_table_get_cells_merged_banner_reports_d2_uno(ctx, doc):
    """5×4 + merge A1:D1: getColumns() is 1, but D2 is a real cell (discussion #821)."""
    text = doc.getText()
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(5, 4)
    text.insertTextContent(text.getEnd(), table, False)
    table.setName("MergedBanner")
    table.getCellByName("A1").setString("Title")
    _merge_a1_d1(table)
    table.getCellByName("D2").setString("Telèfon responsable")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    listed = TableList().execute(tool_ctx)
    assert listed.get("status") == "ok", listed
    by = {t["name"]: t for t in listed["tables"]}
    entry = by["MergedBanner"]
    # Hypothesis (verified): getColumns() is SwXTableColumns::getCount — first-row
    # box count — so a merged banner reports cols=1 while getRows() stays 5.
    assert entry["rows"] == 5
    assert entry["cols"] == 1
    assert entry["cell_count"] == 17

    cells = TableGetCells().execute(tool_ctx, name="MergedBanner")
    assert cells.get("status") == "ok", cells
    assert cells["rows"] == 5 and cells["cols"] == 1
    assert "matrix" not in cells
    assert cells["cells"]["D2"] == "Telèfon responsable"
    assert "B1" not in cells["cells"]
    assert "B1" not in cells["cell_names"]
    assert "D2" in cells["cell_names"]

    one = TableGetCells().execute(tool_ctx, name="MergedBanner", cell="D2")
    assert one.get("status") == "ok", one
    assert one["cell"] == "D2"
    assert one["cells"]["D2"] == "Telèfon responsable"
    assert one["cell_names"] == ["D2"]
    assert "matrix" not in one


@native_test
@with_native_doc("writer")
def test_delete_row_refuses_nested_table_in_merged_d2_uno(ctx, doc):
    """Nest in D2 after A1:D1 merge; delete that row must name the child."""
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(5, 4)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("MergedHost")
    _merge_a1_d1(outer)
    host = outer.getCellByName("D2")
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    host.insertTextContent(host.getEnd(), nested, False)
    nested.setName("MergedChild")
    nested.getCellByName("A1").setString("INNER")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    del_row = ManageTableStructure().execute(
        tool_ctx, action="delete", axis="row", name="MergedHost", index=1
    )
    assert del_row.get("status") == "error" and "MergedChild" in del_row.get("message", ""), del_row
    assert outer.getRows().getCount() == 5
    assert doc.getTextTables().hasByName("MergedChild")
