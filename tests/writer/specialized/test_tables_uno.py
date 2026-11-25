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
    TableMergeCells,
    TableSetCell,
    TableSplitCell,
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


@native_test
@with_native_doc("writer")
def test_table_merge_cells_joins_text_renumbers_and_undoes_uno(ctx, doc):
    """Merge B2:A1 (lower case, reversed) joins text, renumbers C1 -> B1, and undoes in one step."""
    text = doc.getText()
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(3, 3)
    text.insertTextContent(text.getEnd(), table, False)
    table.setName("MergeGrid")
    for cell_name in table.getCellNames():
        table.getCellByName(cell_name).setString("v" + cell_name)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    merged = TableMergeCells().execute(tool_ctx, name="MergeGrid", cell_range="b2:a1")
    assert merged.get("status") == "ok", merged
    assert merged["merged_cell"] == "A1"

    cells = TableGetCells().execute(tool_ctx, name="MergeGrid")
    assert cells.get("status") == "ok", cells
    assert cells["cells"]["A1"] == "vA1\nvB1\nvA2\nvB2"
    assert cells["cells"]["B1"] == "vC1"
    expected_names = ["A1", "B1", "B2", "A3", "B3", "C3"]
    assert cells["cell_names"] == expected_names
    assert merged["cell_names"] == expected_names

    doc.getUndoManager().undo()
    restored = TableGetCells().execute(tool_ctx, name="MergeGrid")
    assert restored.get("status") == "ok", restored
    assert len(restored["cell_names"]) == 9


@native_test
@with_native_doc("writer")
def test_table_merge_cells_keeps_nested_table_uno(ctx, doc):
    """Merging across a host cell preserves the nested table; splitting keeps it in the first cell."""
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("Outer")
    host = outer.getCellByName("B2")
    host.setString("host")
    inner = doc.createInstance("com.sun.star.text.TextTable")
    inner.initialize(1, 1)
    host.insertTextContent(host.getEnd(), inner, False)
    inner.setName("Inner")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    merged = TableMergeCells().execute(tool_ctx, name="Outer", cell_range="A1:B2")
    assert merged.get("status") == "ok", merged
    listed = TableList().execute(tool_ctx)
    assert any(t["name"] == "Inner" for t in listed["tables"])
    after_merge = TableGetCells().execute(tool_ctx, name="Outer")
    assert after_merge["nested_in_cells"] == {"A1": ["Inner"]}

    split_res = TableSplitCell().execute(tool_ctx, name="Outer", cell="A1", direction="columns", parts=2)
    assert split_res.get("status") == "ok", split_res
    after_split = TableGetCells().execute(tool_ctx, name="Outer")
    assert after_split["nested_in_cells"] == {"A1": ["Inner"]}
    assert after_split["cells"]["B1"] == ""


@native_test
@with_native_doc("writer")
def test_table_split_cell_rows_and_unmerge_uno(ctx, doc):
    """Split into rows stacks empty cells; splitting a merged cell restores the split names."""
    text = doc.getText()
    t1 = doc.createInstance("com.sun.star.text.TextTable")
    t1.initialize(2, 2)
    text.insertTextContent(text.getEnd(), t1, False)
    t1.setName("SplitRows")
    for cell_name in t1.getCellNames():
        t1.getCellByName(cell_name).setString("v" + cell_name)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    split_res = TableSplitCell().execute(tool_ctx, name="SplitRows", cell="A1", direction="rows", parts=3)
    assert split_res.get("status") == "ok", split_res
    assert split_res["rows"] == 4
    assert split_res["cell_names"] == ["A1", "B1", "A2", "A3", "A4", "B4"]
    cells = TableGetCells().execute(tool_ctx, name="SplitRows")
    assert cells["cells"]["A1"] == "vA1"
    assert cells["cells"]["A2"] == ""
    assert cells["cells"]["A4"] == "vA2"

    t2 = doc.createInstance("com.sun.star.text.TextTable")
    t2.initialize(3, 2)
    text.insertTextContent(text.getEnd(), t2, False)
    t2.setName("Unmerge")
    merged = TableMergeCells().execute(tool_ctx, name="Unmerge", cell_range="A2:A3")
    assert merged.get("status") == "ok", merged
    assert merged["merged_cell"] == "A2"
    unmerged = TableSplitCell().execute(tool_ctx, name="Unmerge", cell="A2", direction="rows", parts=2)
    assert unmerged.get("status") == "ok", unmerged
    assert len(unmerged["cell_names"]) == 6
    cells2 = TableGetCells().execute(tool_ctx, name="Unmerge")
    assert cells2["cells"]["A3"] == ""


def _accept_all_tables(doc, ctx):
    from plugin.framework.uno_context import execute_dispatch

    execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")


def _reject_all_tables(doc, ctx):
    from plugin.framework.uno_context import execute_dispatch

    execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")


@native_test
@with_native_doc("writer", reuse=False)
def test_manage_table_structure_delete_tracked_variants_uno(ctx, doc):
    """Row and column deletes while recording are tracked across full, empty, mixed, and colspan tables."""
    from plugin.framework.config import get_config, save

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()

    for case_idx, (axis, cells_init, idx) in enumerate([
        ("row", {"A1": "r0a", "B1": "r0b", "A2": "r1a", "B2": "r1b"}, 0),
        ("row", {"A1": "r0a", "B1": "r0b"}, 1),  # empty row
        ("row", {"A1": "r0a", "A2": "r1a"}, 0),  # mixed (col B empty)
        ("column", {"A1": "c0r0", "B1": "c1r0", "A2": "c0r1", "B2": "c1r1"}, 1),
        ("column", {"A1": "c0r0", "A2": "c0r1"}, 1),  # empty col
    ]):
        doc.getText().setString("")
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(2, 2)
        text.insertTextContent(text.getEnd(), tbl, False)
        tbl.setName(f"Band{case_idx}")
        for k, v in cells_init.items():
            tbl.getCellByName(k).setString(v)

        doc.setPropertyValue("RecordChanges", True)
        try:
            res = ManageTableStructure().execute(
                tool_ctx, action="delete", axis=axis, name=f"Band{case_idx}", index=idx
            )
            assert res["status"] == "ok", (axis, idx, res)
            assert res["tracked"] is True and res["pending_review"] is True
            assert doc.getRedlines().getCount() > 0

            _reject_all_tables(doc, ctx)
            assert doc.getRedlines().getCount() == 0
            assert tbl.getRows().getCount() == 2 and tbl.getColumns().getCount() == 2
            for k, v in cells_init.items():
                assert tbl.getCellByName(k).getString() == v

            res2 = ManageTableStructure().execute(
                tool_ctx, action="delete", axis=axis, name=f"Band{case_idx}", index=idx
            )
            assert res2["status"] == "ok", res2
            _accept_all_tables(doc, ctx)
            assert doc.getRedlines().getCount() == 0
            if axis == "row":
                assert tbl.getRows().getCount() == 1
            else:
                assert tbl.getColumns().getCount() == 1
        finally:
            doc.setPropertyValue("RecordChanges", False)

    # Colspan column delete
    doc.getText().setString("")
    colspan_tbl = doc.createInstance("com.sun.star.text.TextTable")
    colspan_tbl.initialize(2, 3)
    text.insertTextContent(text.getEnd(), colspan_tbl, False)
    colspan_tbl.setName("ColspanTbl")
    for n in colspan_tbl.getCellNames():
        colspan_tbl.getCellByName(n).setString(n)
    TableMergeCells().execute(tool_ctx, name="ColspanTbl", cell_range="A1:B1")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res_cs = ManageTableStructure().execute(
            tool_ctx, action="delete", axis="column", name="ColspanTbl", index=0
        )
        assert res_cs["status"] == "error", res_cs
        assert res_cs["code"] == "COLUMN_HAS_SPANNING_CELL", res_cs
        assert colspan_tbl.getColumns().getCount() == 2
        assert len(colspan_tbl.getCellNames()) == 5
        assert colspan_tbl.getCellByName("A1").getString() == "A1\nB1"
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # Re-deleting an already marked row with a two-paragraph cell must succeed
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

    doc.getText().setString("")
    two_para_tbl = doc.createInstance("com.sun.star.text.TextTable")
    two_para_tbl.initialize(2, 2)
    text.insertTextContent(text.getEnd(), two_para_tbl, False)
    two_para_tbl.setName("TwoParaTbl")
    a1_cell = two_para_tbl.getCellByName("A1")
    a1_cur = a1_cell.createTextCursor()
    a1_cell.insertString(a1_cur, "first para", False)
    a1_cell.insertControlCharacter(a1_cur, PARAGRAPH_BREAK, False)
    a1_cell.insertString(a1_cur, "second para", False)
    para_count = 0
    para_enum = a1_cell.createEnumeration()
    while para_enum.hasMoreElements():
        para_enum.nextElement()
        para_count += 1
    assert para_count == 2
    two_para_tbl.getCellByName("A2").setString("keep")
    doc.setPropertyValue("RecordChanges", True)
    try:
        first_del = ManageTableStructure().execute(
            tool_ctx, action="delete", axis="row", name="TwoParaTbl", index=0
        )
        assert first_del["status"] == "ok" and first_del["tracked"] is True
        re_del = ManageTableStructure().execute(
            tool_ctx, action="delete", axis="row", name="TwoParaTbl", index=0
        )
        assert re_del["status"] == "ok" and re_del["tracked"] is True
        _accept_all_tables(doc, ctx)
        assert two_para_tbl.getRows().getCount() == 1
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # Record mode restores RecordChanges=False
    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "record"})
    try:
        doc.getText().setString("")
        rec_tbl = doc.createInstance("com.sun.star.text.TextTable")
        rec_tbl.initialize(2, 2)
        text.insertTextContent(text.getEnd(), rec_tbl, False)
        rec_tbl.setName("RecTbl")
        rec_tbl.getCellByName("A1").setString("keep")
        rec_tbl.getCellByName("A2").setString("drop")
        assert doc.getPropertyValue("RecordChanges") is False
        res_rec = ManageTableStructure().execute(
            tool_ctx, action="delete", axis="row", name="RecTbl", index=1
        )
        assert res_rec["status"] == "ok", res_rec
        assert res_rec["tracked"] is True
        assert doc.getPropertyValue("RecordChanges") is False
        _accept_all_tables(doc, ctx)
        assert rec_tbl.getRows().getCount() == 1
    finally:
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_tracked_table_and_band_delete_failures_and_nested_uno(ctx, doc):
    """Pending-insertion refusal, hidden-section refusal, and A1-nested table_delete."""
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()

    # 1. Row whose text is a pending insertion -> error, text and redlines unchanged
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(text.getEnd(), tbl, False)
    tbl.setName("PendingText")
    tbl.getCellByName("A1").setString("stay")
    doc.setPropertyValue("RecordChanges", True)
    try:
        tbl.getCellByName("A2").setString("user_ins")
        before_rl = doc.getRedlines().getCount()
        err = ManageTableStructure().execute_safe(
            tool_ctx, action="delete", axis="row", name="PendingText", index=1
        )
        assert err["status"] == "error" and err["code"] == "TABLE_DELETE_NOT_TRACKABLE"
        assert tbl.getCellByName("A2").getString() == "user_ins"
        assert doc.getRedlines().getCount() == before_rl
        _accept_all_tables(doc, ctx)
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # 2. Pending inserted table -> both band delete and table_delete fail and keep text
    doc.getText().setString("")
    doc.setPropertyValue("RecordChanges", True)
    try:
        ptbl = doc.createInstance("com.sun.star.text.TextTable")
        ptbl.initialize(2, 2)
        text.insertTextContent(text.getEnd(), ptbl, False)
        ptbl.setName("PendingTbl")
        ptbl.getCellByName("A1").setString("p1")
        ptbl.getCellByName("A2").setString("p2")
        before_rl = doc.getRedlines().getCount()
        err_band = ManageTableStructure().execute_safe(
            tool_ctx, action="delete", axis="row", name="PendingTbl", index=0
        )
        assert err_band["status"] == "error" and err_band["code"] == "TABLE_DELETE_NOT_TRACKABLE"
        assert ptbl.getCellByName("A1").getString() == "p1"
        assert all(
            doc.getRedlines().getByIndex(i).getPropertyValue("RedlineType") == "Insert"
            for i in range(doc.getRedlines().getCount())
        )
        err_tbl = TableDelete().execute_safe(tool_ctx, name="PendingTbl")
        assert err_tbl["status"] == "error" and err_tbl["code"] == "TABLE_DELETE_NOT_TRACKABLE"
        assert ptbl.getCellByName("A1").getString() == "p1"
        assert ptbl.getCellByName("A2").getString() == "p2"
        assert all(
            doc.getRedlines().getByIndex(i).getPropertyValue("RedlineType") == "Insert"
            for i in range(doc.getRedlines().getCount())
        )
        _reject_all_tables(doc, ctx)
        assert doc.getRedlines().getCount() == 0
        assert ptbl.getCellByName("A1").getString() == ""
        assert ptbl.getCellByName("A2").getString() == ""
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # 3. Hidden section table + visible other table -> error, other table untouched, 0 redlines
    doc.getText().setString("")
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setPropertyValue("IsVisible", False)
    text.insertTextContent(text.getEnd(), sec, False)
    hidden_tbl = doc.createInstance("com.sun.star.text.TextTable")
    hidden_tbl.initialize(1, 1)
    sec_cur = sec.getAnchor().getText().createTextCursorByRange(sec.getAnchor().getStart())
    sec.getAnchor().getText().insertTextContent(sec_cur, hidden_tbl, False)
    hidden_tbl.setName("HiddenTbl")
    hidden_tbl.getCellByName("A1").setString("hidden")

    vis_tbl = doc.createInstance("com.sun.star.text.TextTable")
    vis_tbl.initialize(1, 1)
    text.insertTextContent(text.getEnd(), vis_tbl, False)
    vis_tbl.setName("VisibleTbl")
    vis_tbl.getCellByName("A1").setString("keep me")

    doc.setPropertyValue("RecordChanges", True)
    try:
        err_hid = TableDelete().execute_safe(tool_ctx, name="HiddenTbl")
        assert err_hid["status"] == "error" and err_hid["code"] == "TABLE_DELETE_NOT_TRACKABLE"
        assert doc.getRedlines().getCount() == 0
        assert vis_tbl.getCellByName("A1").getString() == "keep me"
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # 4. Outer table whose A1 starts with a nested table -> outer table goes on Accept
    doc.getText().setString("")
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(1, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("OuterA1")
    a1 = outer.getCellByName("A1")
    inner = doc.createInstance("com.sun.star.text.TextTable")
    inner.initialize(1, 1)
    a1.insertTextContent(a1.getStart(), inner, False)
    inner.setName("InnerA1")
    outer.getCellByName("B1").setString("outer_b1")
    doc.setPropertyValue("RecordChanges", True)
    try:
        del_outer = TableDelete().execute(tool_ctx, name="OuterA1")
        assert del_outer["status"] == "ok" and del_outer["pending_review"] is True
        _reject_all_tables(doc, ctx)
        assert doc.getTextTables().hasByName("OuterA1")
        assert doc.getTextTables().hasByName("InnerA1")
        assert outer.getCellByName("B1").getString() == "outer_b1"

        del_outer2 = TableDelete().execute(tool_ctx, name="OuterA1")
        assert del_outer2["status"] == "ok" and del_outer2["pending_review"] is True
        _accept_all_tables(doc, ctx)
        assert not doc.getTextTables().hasByName("OuterA1")
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_record_mode_table_row_col_delete_tagged_as_one_change_uno(ctx, doc):
    from plugin.framework.config import get_config, save
    from plugin.writer.review_scan import is_agent_token

    prev = get_config("doc.agent_edit_review_mode")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    t_name = TableInsert().execute(tool_ctx, rows=3, columns=3, data=[["a", "b", "c"], ["d", "e", "f"], ["g", "h", "i"]])["table_name"]
    save({"doc.agent_edit_review_mode": "record"})
    try:
        res_row = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t_name, index=1)
        assert res_row["status"] == "ok" and res_row.get("pending_review") is True, res_row
        tokens_row = {
            doc.getRedlines().getByIndex(i).getPropertyValue("RedlineComment")
            for i in range(doc.getRedlines().getCount())
        }
        assert len(tokens_row) == 1 and all(is_agent_token(t) for t in tokens_row)
        _reject_all_tables(doc, ctx)

        res_col = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t_name, index=1)
        assert res_col["status"] == "ok" and res_col["pending_review"] is True
        tokens_col = {
            doc.getRedlines().getByIndex(i).getPropertyValue("RedlineComment")
            for i in range(doc.getRedlines().getCount())
        }
        assert len(tokens_col) == 1 and all(is_agent_token(t) for t in tokens_col)
        _reject_all_tables(doc, ctx)

        res_tbl = TableDelete().execute_safe(tool_ctx, name=t_name)
        assert res_tbl["status"] == "ok" and res_tbl["pending_review"] is True
        tokens_tbl = {
            doc.getRedlines().getByIndex(i).getPropertyValue("RedlineComment")
            for i in range(doc.getRedlines().getCount())
        }
        assert len(tokens_tbl) == 1 and all(is_agent_token(t) for t in tokens_tbl)
        _accept_all_tables(doc, ctx)
        assert not doc.getTextTables().hasByName(t_name)
    finally:
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_colspan_column_delete_preserves_nested_table_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    outer_name = TableInsert().execute(tool_ctx, rows=2, columns=3, data=[["a1", "b1", "c1"], ["a2", "b2", "c2"]])["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=outer_name, cell_range="A1:B1")
    outer = doc.getTextTables().getByName(outer_name)
    b2_text = outer.getCellByName("B2").getText()
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    nested.setName("Inner")
    b2_text.insertTextContent(b2_text.createTextCursor(), nested, False)

    # Column 0 spans A1:B1 -> includes B2's nested table (refused both untracked and tracked).
    res_untracked = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=outer_name, index=0)
    assert res_untracked["status"] == "error" and "Inner" in res_untracked["message"]
    assert doc.getTextTables().hasByName("Inner")

    doc.setPropertyValue("RecordChanges", True)
    try:
        res_tracked = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=outer_name, index=0)
        assert res_tracked["status"] == "error" and "Inner" in res_tracked["message"]
        assert doc.getTextTables().hasByName("Inner")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # Column 1 (C2) must succeed when the nested table is in B2.
    ok_col1 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=outer_name, index=1)
    assert ok_col1["status"] == "ok", ok_col1
    assert doc.getTextTables().hasByName("Inner")

    # C2 host with column 1 (index=1 of A1:B1 merged 2x3) must refuse (tracked and untracked).
    outer2_name = TableInsert().execute(tool_ctx, rows=2, columns=3, data=[["a1", "b1", "c1"], ["a2", "b2", "c2"]])["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=outer2_name, cell_range="A1:B1")
    outer2 = doc.getTextTables().getByName(outer2_name)
    c2_text = outer2.getCellByName("C2").getText()
    nested2 = doc.createInstance("com.sun.star.text.TextTable")
    nested2.initialize(1, 1)
    nested2.setName("InnerC2")
    c2_text.insertTextContent(c2_text.createTextCursor(), nested2, False)

    ref_c2 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=outer2_name, index=1)
    assert ref_c2["status"] == "error" and "InnerC2" in ref_c2["message"]
    assert doc.getTextTables().hasByName("InnerC2")

    doc.setPropertyValue("RecordChanges", True)
    try:
        ref_c2_tr = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=outer2_name, index=1)
        assert ref_c2_tr["status"] == "error" and "InnerC2" in ref_c2_tr["message"]
        assert doc.getTextTables().hasByName("InnerC2")
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_column_band_cells_matches_libreoffice_delete_uno(ctx, doc):
    from plugin.writer.specialized.tables import _column_band_cells

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    t_name = TableInsert().execute(tool_ctx, rows=2, columns=3, data=[["a1", "b1", "c1"], ["a2", "b2", "c2"]])["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=t_name, cell_range="A1:B1")
    table = doc.getTextTables().getByName(t_name)
    predicted_texts = {table.getCellByName(n).getString() for n in _column_band_cells(table, 0)}
    assert predicted_texts == {"a1\nb1", "a2", "b2"}

    before_texts = {table.getCellByName(n).getString() for n in table.getCellNames()}
    table.getColumns().removeByIndex(0, 1)
    after_texts = {table.getCellByName(n).getString() for n in table.getCellNames()}
    assert before_texts - after_texts == predicted_texts


@native_test
@with_native_doc("writer")
def test_column_delete_refuses_wider_cell_and_split_recovers_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    # 1. Header merge A1:B1 in 3-col table -> col 0 refused, cells unchanged
    t1 = TableInsert().execute(tool_ctx, rows=2, columns=3, data=[["a1", "b1", "c1"], ["a2", "b2", "c2"]])["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=t1, cell_range="A1:B1")
    tbl1 = doc.getTextTables().getByName(t1)
    before1 = {n: tbl1.getCellByName(n).getString() for n in tbl1.getCellNames()}
    res1 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t1, index=0)
    assert res1["status"] == "error"
    assert res1["code"] == "COLUMN_HAS_SPANNING_CELL"
    assert {n: tbl1.getCellByName(n).getString() for n in tbl1.getCellNames()} == before1

    # 2. Full-width row A3:C3 in 3-col table, col 1 -> refused tracked and untracked; table survives
    t2 = TableInsert().execute(
        tool_ctx, rows=3, columns=3,
        data=[["a1", "b1", "c1"], ["a2", "b2", "c2"], ["a3", "b3", "c3"]],
    )["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=t2, cell_range="A3:C3")
    for rec in (False, True):
        doc.setPropertyValue("RecordChanges", rec)
        try:
            res2 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t2, index=1)
            assert res2["status"] == "error"
            assert res2["code"] == "COLUMN_HAS_SPANNING_CELL"
            assert doc.getTextTables().hasByName(t2)
        finally:
            doc.setPropertyValue("RecordChanges", False)

    # 3. Split A1:B1 -> 2 columns, then delete col 0 keeps b and c
    t3 = TableInsert().execute(tool_ctx, rows=2, columns=3, data=[["a", "b", "c"], ["a2", "b2", "c2"]])["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=t3, cell_range="A1:B1")
    from plugin.writer.specialized.tables import TableSplitCell
    split_res = TableSplitCell().execute_safe(tool_ctx, name=t3, cell="A1", direction="columns", parts=2)
    assert split_res["status"] == "ok", split_res
    del_res = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t3, index=0)
    assert del_res["status"] == "ok", del_res
    tbl3 = doc.getTextTables().getByName(t3)
    assert tbl3.getColumns().getCount() == 2
    texts3 = {n: tbl3.getCellByName(n).getString() for n in tbl3.getCellNames()}
    assert texts3 == {"A1": "", "B1": "c", "A2": "b2", "B2": "c2"}, texts3

    # 4. Staggered merges B1:C1 and C2:D2 in 3x5 -> col 1 names B1 (spans 2) affecting 3 columns; split 2 recovers
    t4 = TableInsert().execute(
        tool_ctx, rows=3, columns=5,
        data=[
            ["a1", "b1", "c1", "d1", "e1"],
            ["a2", "b2", "c2", "d2", "e2"],
            ["a3", "b3", "c3", "d3", "e3"],
        ],
    )["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=t4, cell_range="B1:C1")
    TableMergeCells().execute_safe(tool_ctx, name=t4, cell_range="C2:D2")
    res4 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t4, index=1)
    assert res4["status"] == "error"
    assert res4["code"] == "COLUMN_HAS_SPANNING_CELL"
    assert "'B1' spans 2 columns, so 3 columns" in res4["message"], res4["message"]
    split4 = TableSplitCell().execute_safe(tool_ctx, name=t4, cell="B1", direction="columns", parts=2)
    assert split4["status"] == "ok", split4
    del4 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t4, index=1)
    assert del4["status"] == "ok", del4
    tbl4 = doc.getTextTables().getByName(t4)
    row1 = [tbl4.getCellByName(c).getString() for c in ("A1", "B1", "C1", "D1")]
    assert row1 == ["a1", "", "d1", "e1"], row1


@native_test
@with_native_doc("writer")
def test_tall_first_cell_row_delete_refused_and_covered_row_deletes_own_cells_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    tall_name = TableInsert().execute(tool_ctx, rows=3, columns=3, data=[["a1", "b1", "c1"], ["a2", "b2", "c2"], ["a3", "b3", "c3"]])["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=tall_name, cell_range="A1:A2")
    doc.setPropertyValue("RecordChanges", True)
    try:
        refused = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=tall_name, index=0)
        assert refused["status"] == "error"
        assert refused["code"] == "ROW_HAS_SPANNING_CELL"
        assert doc.getRedlines().getCount() == 0

        ok = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=tall_name, index=1)
        assert ok["status"] == "ok"
        assert doc.getRedlines().getCount() == 2
        _accept_all_tables(doc, ctx)
        assert doc.getTextTables().getByName(tall_name).getRows().getCount() == 2
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_tall_cell_row_delete_untracked_and_middle_tracked_and_after_split_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    t1 = TableInsert().execute(
        tool_ctx, rows=3, columns=3, data=[["a1", "b1", "c1"], ["a2", "b2", "c2"], ["a3", "b3", "c3"]]
    )["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=t1, cell_range="A1:A2")
    tbl1 = doc.getTextTables().getByName(t1)

    refused_untracked = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t1, index=0)
    assert refused_untracked["status"] == "error"
    assert refused_untracked["code"] == "ROW_HAS_SPANNING_CELL"
    assert tbl1.getRows().getCount() == 3
    assert tbl1.getCellByName("A1").getString() == "a1\na2"

    # Deleting covered row 1 keeps A1's merged content.
    ok_cov = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t1, index=1)
    assert ok_cov["status"] == "ok"
    assert tbl1.getCellByName("A1").getString() == "a1\na2"

    # Middle tall cell (B1:B2) while recording is also refused; after split, row 0 deletes cleanly.
    t2 = TableInsert().execute(
        tool_ctx, rows=3, columns=3, data=[["a1", "b1", "c1"], ["a2", "b2", "c2"], ["a3", "b3", "c3"]]
    )["table_name"]
    TableMergeCells().execute_safe(tool_ctx, name=t2, cell_range="B1:B2")
    doc.setPropertyValue("RecordChanges", True)
    try:
        refused_mid = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t2, index=0)
        assert refused_mid["status"] == "error"
        assert refused_mid["code"] == "ROW_HAS_SPANNING_CELL"
    finally:
        doc.setPropertyValue("RecordChanges", False)

    split_res = TableSplitCell().execute_safe(tool_ctx, name=t2, cell="B1", direction="rows", parts=2)
    assert split_res["status"] == "ok"
    ok_after_split = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t2, index=0)
    assert ok_after_split["status"] == "ok"
    assert doc.getTextTables().getByName(t2).getRows().getCount() == 2


@native_test
@with_native_doc("writer")
def test_table_cell_reads_hide_multi_para_tracked_deletion_uno(ctx, doc):
    from plugin.doc.text_helpers import DeletionState, visible_range_text
    from plugin.tests.testing_utils import strike_across_paragraphs
    from plugin.writer.table_nesting import cell_plain_siblings

    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 2)
    text.insertTextContent(text.getEnd(), tbl, False)
    tbl.setName("T1")

    cell_a1 = tbl.getCellByName("A1")
    strike_across_paragraphs(doc, cell_a1)
    assert "gone" not in cell_plain_siblings(cell_a1, DeletionState())
    assert "Keep" in cell_plain_siblings(cell_a1, DeletionState())

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    res = TableGetCells().execute(tctx, name="T1")
    assert res["status"] == "ok", res
    assert "gone" not in res["cells"]["A1"], res["cells"]
    assert "Tail" in res["cells"]["A1"], res["cells"]

    # visible_range_text is empty for a cell whose three paragraphs are one Delete.
    cell_b1 = tbl.getCellByName("B1")
    b_cur = cell_b1.createTextCursor()
    for idx, line in enumerate(["d1", "d2", "d3"]):
        if idx > 0:
            cell_b1.insertControlCharacter(b_cur, 0, False)
        cell_b1.insertString(b_cur, line, False)
    b_cur.gotoStart(False)
    b_cur.gotoEnd(True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        b_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)
    assert doc.getRedlines().getCount() == 2  # 1 in A1 + 1 in B1
    b_enum = cell_b1.createEnumeration()
    b_paras = []
    while b_enum.hasMoreElements():
        b_paras.append(b_enum.nextElement())
    mid_enum = b_paras[1].createEnumeration()
    while mid_enum.hasMoreElements():
        assert mid_enum.nextElement().getPropertyValue("TextPortionType") != "Redline"
    assert visible_range_text(doc, cell_b1).strip("\n") == ""


@native_test
@with_native_doc("writer", reuse=False)
def test_table_get_cells_hides_struck_table_uno(ctx, doc):
    from plugin.tests.testing_utils import strike_all_after_leading_table

    tbl = strike_all_after_leading_table(doc, ctx)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    res = TableGetCells().execute(tctx, name=tbl.getName())
    assert res["status"] == "ok", res
    assert res["cells"]["A1"] == "", res["cells"]


@native_test
@with_native_doc("writer", reuse=False)
def test_nested_live_table_keeps_deletion_recorded_false_uno(ctx, doc):
    from plugin.writer.specialized.tables import _delete_redline_count, _deletion_recorded

    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(1, 1)
    doc.getText().insertTextContent(doc.getText().getEnd(), outer, False)
    host_cell = outer.getCellByName("A1")
    host_cell.setString("host")
    h_cur = host_cell.createTextCursor()
    h_cur.gotoStart(False)
    h_cur.gotoEnd(True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        h_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)
    inner = doc.createInstance("com.sun.star.text.TextTable")
    inner.initialize(1, 1)
    host_cell.insertTextContent(host_cell.getEnd(), inner, False)
    inner.getCellByName("A1").setString("live inner")

    before = _delete_redline_count(doc)
    assert before is not None
    assert _deletion_recorded(doc, before, [host_cell]) is False


def _make_3x3_lettered_table(doc, name):
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(3, 3)
    text = doc.getText()
    text.insertTextContent(text.getEnd(), tbl, False)
    tbl.setName(name)
    for row in range(1, 4):
        for col in "abc":
            cell_name = f"{col.upper()}{row}"
            tbl.getCellByName(cell_name).setString(f"{col}{row}")
    return tbl


@native_test
@with_native_doc("writer", reuse=False)
def test_empty_tall_cell_row_delete_untracked_keeps_rows_below_uno(ctx, doc):
    """Untracked row delete succeeds when a vertically merged cell is empty, unless it holds a frame."""
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    tbl = _make_3x3_lettered_table(doc, "UntrackedSpan")
    tbl.getCellByName("A1").setString("")
    tbl.getCellByName("A2").setString("")
    merge_res = TableMergeCells().execute(tool_ctx, name="UntrackedSpan", cell_range="A1:A2")
    assert merge_res["status"] == "ok", merge_res

    del_res = ManageTableStructure().execute(
        tool_ctx, action="delete", axis="row", name="UntrackedSpan", index=0
    )
    assert del_res["status"] == "ok", del_res
    cells = TableGetCells().execute(tool_ctx, name="UntrackedSpan")
    assert cells["cells"] == {"A1": "", "B1": "b2", "C1": "c2", "A2": "a3", "B2": "b3", "C2": "c3"}

    # An at-paragraph TextFrame inside an otherwise empty spanning cell must keep the refusal.
    tbl2 = _make_3x3_lettered_table(doc, "UntrackedFrameSpan")
    tbl2.getCellByName("A1").setString("")
    tbl2.getCellByName("A2").setString("")
    merge2 = TableMergeCells().execute(tool_ctx, name="UntrackedFrameSpan", cell_range="A1:A2")
    assert merge2["status"] == "ok", merge2
    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.setPropertyValue("AnchorType", uno.Enum("com.sun.star.text.TextContentAnchorType", "AT_PARAGRAPH"))
    a1 = tbl2.getCellByName("A1")
    a1.insertTextContent(a1.getStart(), frame, False)
    assert doc.getTextFrames().getCount() == 1

    refused = ManageTableStructure().execute_safe(
        tool_ctx, action="delete", axis="row", name="UntrackedFrameSpan", index=0
    )
    assert refused["status"] == "error"
    assert refused["code"] == "ROW_HAS_SPANNING_CELL"
    assert doc.getTextFrames().getCount() == 1


@native_test
@with_native_doc("writer", reuse=False)
def test_empty_tall_cell_row_delete_tracked_inner_allowed_edge_refused_uno(ctx, doc):
    """Tracked row delete allows an empty interior span, refuses empty edge spans until split."""
    from plugin.framework.config import get_config, save

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    prev_mode = get_config("doc.agent_edit_review_mode")
    try:
        # Interior empty span B1:B2: allowed while recording; reject restores, accept drops only row 0.
        inner = _make_3x3_lettered_table(doc, "InnerSpan")
        inner.getCellByName("B1").setString("")
        inner.getCellByName("B2").setString("")
        assert TableMergeCells().execute(tool_ctx, name="InnerSpan", cell_range="B1:B2")["status"] == "ok"
        before_cells = TableGetCells().execute(tool_ctx, name="InnerSpan")["cells"]

        doc.setPropertyValue("RecordChanges", True)
        res_inner = ManageTableStructure().execute(
            tool_ctx, action="delete", axis="row", name="InnerSpan", index=0
        )
        assert res_inner["status"] == "ok", res_inner
        assert res_inner["tracked"] is True
        doc.setPropertyValue("RecordChanges", False)

        _reject_all_tables(doc, ctx)
        assert TableGetCells().execute(tool_ctx, name="InnerSpan")["cells"] == before_cells

        doc.setPropertyValue("RecordChanges", True)
        res_inner2 = ManageTableStructure().execute(
            tool_ctx, action="delete", axis="row", name="InnerSpan", index=0
        )
        assert res_inner2["status"] == "ok", res_inner2
        doc.setPropertyValue("RecordChanges", False)
        _accept_all_tables(doc, ctx)
        assert TableGetCells().execute(tool_ctx, name="InnerSpan")["cells"] == {
            "A1": "a2",
            "B1": "",
            "C1": "c2",
            "A2": "a3",
            "B2": "b3",
            "C2": "c3",
        }

        # Empty edge spans A1:A2 and C1:C2: refused while recording, leaving 0 redlines.
        for edge_name, edge_col in (("EdgeASpan", "A"), ("EdgeCSpan", "C")):
            edge_tbl = _make_3x3_lettered_table(doc, edge_name)
            edge_tbl.getCellByName(f"{edge_col}1").setString("")
            edge_tbl.getCellByName(f"{edge_col}2").setString("")
            assert (
                TableMergeCells().execute(
                    tool_ctx, name=edge_name, cell_range=f"{edge_col}1:{edge_col}2"
                )["status"]
                == "ok"
            )
            snapshot = TableGetCells().execute(tool_ctx, name=edge_name)["cells"]
            doc.setPropertyValue("RecordChanges", True)
            refused = ManageTableStructure().execute_safe(
                tool_ctx, action="delete", axis="row", name=edge_name, index=0
            )
            doc.setPropertyValue("RecordChanges", False)
            assert refused["status"] == "error"
            assert refused["code"] == "ROW_HAS_SPANNING_CELL"
            assert TableGetCells().execute(tool_ctx, name=edge_name)["cells"] == snapshot
            assert doc.getRedlines().getCount() == 0

        # Splitting EdgeASpan A1 into 2 rows lets tracked delete + Accept keep row 2.
        assert TableSplitCell().execute(tool_ctx, name="EdgeASpan", cell="A1", direction="rows", parts=2)["status"] == "ok"
        doc.setPropertyValue("RecordChanges", True)
        ok_after_split = ManageTableStructure().execute(
            tool_ctx, action="delete", axis="row", name="EdgeASpan", index=0
        )
        doc.setPropertyValue("RecordChanges", False)
        assert ok_after_split["status"] == "ok", ok_after_split
        _accept_all_tables(doc, ctx)
        assert TableGetCells().execute(tool_ctx, name="EdgeASpan")["cells"] == {
            "A1": "",
            "B1": "b2",
            "C1": "c2",
            "A2": "a3",
            "B2": "b3",
            "C2": "c3",
        }

        # Review mode "record" with RecordChanges off must still refuse edge span inside _delete_band_tracked.
        rec_edge = _make_3x3_lettered_table(doc, "ReviewEdgeSpan")
        rec_edge.getCellByName("A1").setString("")
        rec_edge.getCellByName("A2").setString("")
        assert TableMergeCells().execute(tool_ctx, name="ReviewEdgeSpan", cell_range="A1:A2")["status"] == "ok"
        rec_snapshot = TableGetCells().execute(tool_ctx, name="ReviewEdgeSpan")["cells"]
        assert doc.getPropertyValue("RecordChanges") is False
        save({"doc.agent_edit_review_mode": "record"})
        refused_rec = ManageTableStructure().execute_safe(
            tool_ctx, action="delete", axis="row", name="ReviewEdgeSpan", index=0
        )
        assert refused_rec["status"] == "error"
        assert refused_rec["code"] == "ROW_HAS_SPANNING_CELL"
        assert TableGetCells().execute(tool_ctx, name="ReviewEdgeSpan")["cells"] == rec_snapshot
        assert doc.getRedlines().getCount() == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({"doc.agent_edit_review_mode": prev_mode})


@native_test
@with_native_doc("writer")
def test_table_insert_uses_pinned_caret_uno(ctx, doc):
    from plugin.doc.text_helpers import selection_cursor_snapshot
    from plugin.framework.tool import SelectionPin
    from plugin.framework.uno_context import enum_has_more
    from plugin.tests.testing_utils import native_writer_tool_context

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha beta gamma", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second para", False)
    cur.gotoStart(False)
    cur.goRight(6, False)
    controller = doc.getCurrentController()
    controller.select(cur)

    tctx = native_writer_tool_context(doc, ctx)
    tctx.selection_pin = SelectionPin(selection_cursor_snapshot(doc), doc)
    controller.getViewCursor().gotoEnd(False)

    res = TableInsert().execute(tctx, rows=1, columns=1)
    assert res["status"] == "ok", res

    items = []
    enum = text.createEnumeration()
    while enum_has_more(enum):
        el = enum.nextElement()
        items.append("<TABLE>" if el.supportsService("com.sun.star.text.TextTable") else el.getString())
    assert items == ["Alpha ", "<TABLE>", "beta gamma", "Second para"], items


@native_test
@with_native_doc("writer", reuse=False)
def test_protected_cell_band_delete_refused_and_covered_band_not_deleted_uno(ctx, doc):
    from plugin.doc.text_helpers import iter_elements

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    tbl = _make_3x3_lettered_table(doc, "ProtTbl")
    tbl.getCellByName("B1").setPropertyValue("IsProtected", True)
    before_cells = TableGetCells().execute(tool_ctx, name="ProtTbl")["cells"]

    for rec in (False, True):
        doc.setPropertyValue("RecordChanges", rec)
        try:
            for axis, idx in (("row", 0), ("column", 1)):
                res = ManageTableStructure().execute_safe(
                    tool_ctx, action="delete", axis=axis, name="ProtTbl", index=idx
                )
                assert res["status"] == "error", res
                assert res["code"] == "CELLS_PROTECTED", res
                assert "B1" in res["message"]
                assert tbl.getRows().getCount() == 3
                assert tbl.getColumns().getCount() == 3
                assert TableGetCells().execute(tool_ctx, name="ProtTbl")["cells"] == before_cells
                assert doc.getRedlines().getCount() == 0
        finally:
            doc.setPropertyValue("RecordChanges", False)

    tbl.getCellByName("B1").setPropertyValue("IsProtected", False)
    ok_res = ManageTableStructure().execute_safe(
        tool_ctx, action="delete", axis="row", name="ProtTbl", index=0
    )
    assert ok_res["status"] == "ok", ok_res
    assert tbl.getRows().getCount() == 2

    # Protect A1, then merge A1:A2: row 1 holds only B2, C2, so precheck passes and LO skips.
    cov_tbl = _make_3x3_lettered_table(doc, "CoveredProt")
    cov_tbl.getCellByName("A1").setPropertyValue("IsProtected", True)
    assert TableMergeCells().execute(tool_ctx, name="CoveredProt", cell_range="A1:A2")["status"] == "ok"
    cov_res = ManageTableStructure().execute_safe(
        tool_ctx, action="delete", axis="row", name="CoveredProt", index=1
    )
    assert cov_res["status"] == "error", cov_res
    assert cov_res["code"] == "TABLE_BAND_NOT_DELETED", cov_res
    assert cov_tbl.getRows().getCount() == 3

    # Empty spanning cell holding only a comment: refusal names a comment and keeps it.
    ann_tbl = _make_3x3_lettered_table(doc, "CommentSpan")
    ann_tbl.getCellByName("A1").setString("")
    ann_tbl.getCellByName("A2").setString("")
    assert TableMergeCells().execute(tool_ctx, name="CommentSpan", cell_range="A1:A2")["status"] == "ok"
    ann = doc.createInstance("com.sun.star.text.textfield.Annotation")
    ann.Content = "keep me"
    a1 = ann_tbl.getCellByName("A1")
    a1.insertTextContent(a1.getStart(), ann, False)
    ann_res = ManageTableStructure().execute_safe(
        tool_ctx, action="delete", axis="row", name="CommentSpan", index=0
    )
    assert ann_res["status"] == "error", ann_res
    assert ann_res["code"] == "ROW_HAS_SPANNING_CELL", ann_res
    assert "comment" in ann_res["message"]
    portions = list(iter_elements(a1.createEnumeration().nextElement().createEnumeration()))
    assert any(p.getPropertyValue("TextPortionType") == "Annotation" for p in portions)


@native_test
@with_native_doc("writer", reuse=False)
def test_column_refusal_splits_work_when_followed_uno(ctx, doc):
    import re

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    # 1. Equal header merge A1:B1 on 2x3, delete col 0
    t1 = TableInsert().execute(tool_ctx, rows=2, columns=3, data=[["a", "b", "c"], ["a2", "b2", "c2"]])["table_name"]
    assert TableMergeCells().execute_safe(tool_ctx, name=t1, cell_range="A1:B1")["status"] == "ok"
    ref1 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t1, index=0)
    assert ref1["status"] == "error" and ref1["code"] == "COLUMN_HAS_SPANNING_CELL"
    steps1 = [(c, int(p)) for c, p in re.findall(r"'([A-Za-z]+\d+)' \(parts=(\d+)\)", ref1["message"])]
    assert steps1, ref1["message"]
    for cell_name, parts in steps1:
        assert TableSplitCell().execute_safe(tool_ctx, name=t1, cell=cell_name, direction="columns", parts=parts)["status"] == "ok"
    ok1 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t1, index=0)
    assert ok1["status"] == "ok", ok1
    assert TableGetCells().execute(tool_ctx, name=t1)["cells"] == {"A1": "", "B1": "c", "A2": "b2", "B2": "c2"}

    # 2. 4x4 banner A1:D1, delete idx 0 and idx 2
    for idx, expected in (
        (
            0,
            {
                "A1": "", "B1": "", "C1": "",
                "A2": "b2", "B2": "c2", "C2": "d2",
                "A3": "b3", "B3": "c3", "C3": "d3",
                "A4": "b4", "B4": "c4", "C4": "d4",
            },
        ),
        (
            2,
            {
                "A1": "h1\nh2\nh3\nh4", "B1": "", "C1": "",
                "A2": "a2", "B2": "b2", "C2": "d2",
                "A3": "a3", "B3": "b3", "C3": "d3",
                "A4": "a4", "B4": "b4", "C4": "d4",
            },
        ),
    ):
        tb = TableInsert().execute(
            tool_ctx,
            rows=4,
            columns=4,
            data=[
                ["h1", "h2", "h3", "h4"],
                ["a2", "b2", "c2", "d2"],
                ["a3", "b3", "c3", "d3"],
                ["a4", "b4", "c4", "d4"],
            ],
        )["table_name"]
        assert TableMergeCells().execute_safe(tool_ctx, name=tb, cell_range="A1:D1")["status"] == "ok"
        ref = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=tb, index=idx)
        assert ref["status"] == "error" and ref["code"] == "COLUMN_HAS_SPANNING_CELL", ref
        steps = [(c, int(p)) for c, p in re.findall(r"'([A-Za-z]+\d+)' \(parts=(\d+)\)", ref["message"])]
        assert steps, ref["message"]
        for cell_name, parts in steps:
            assert TableSplitCell().execute_safe(tool_ctx, name=tb, cell=cell_name, direction="columns", parts=parts)["status"] == "ok"
        ok = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=tb, index=idx)
        assert ok["status"] == "ok", ok
        assert TableGetCells().execute(tool_ctx, name=tb)["cells"] == expected

    # 3. 3x3 with A1:B2 merged (both directions), delete col 0
    t3 = TableInsert().execute(
        tool_ctx,
        rows=3,
        columns=3,
        data=[["a1", "b1", "c1"], ["a2", "b2", "c2"], ["a3", "b3", "c3"]],
    )["table_name"]
    assert TableMergeCells().execute_safe(tool_ctx, name=t3, cell_range="A1:B2")["status"] == "ok"
    ref3 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t3, index=0)
    assert ref3["status"] == "error" and ref3["code"] == "COLUMN_HAS_SPANNING_CELL", ref3
    steps3 = [(c, int(p)) for c, p in re.findall(r"'([A-Za-z]+\d+)' \(parts=(\d+)\)", ref3["message"])]
    assert steps3, ref3["message"]
    for cell_name, parts in steps3:
        assert TableSplitCell().execute_safe(tool_ctx, name=t3, cell=cell_name, direction="columns", parts=parts)["status"] == "ok"
    ok3 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t3, index=0)
    assert ok3["status"] == "ok", ok3
    cells3 = TableGetCells().execute(tool_ctx, name=t3)["cells"]
    assert cells3 == {
        "A1": "", "B1": "c1",
        "B2": "c2",
        "A3": "b3", "B3": "c3",
    }, cells3


def _set_table_separators(table, positions):
    seps = []
    for pos in positions:
        sep = uno.createUnoStruct("com.sun.star.text.TableColumnSeparator")
        sep.Position = pos
        sep.IsVisible = True
        seps.append(sep)
    uno.invoke(
        table,
        "setPropertyValue",
        ("TableColumnSeparators", uno.Any("[]com.sun.star.text.TableColumnSeparator", tuple(seps))),
    )


@native_test
@with_native_doc("writer", reuse=False)
def test_column_refusal_unequal_widths_says_do_not_split_uno(ctx, doc):
    import os
    import tempfile
    from plugin.framework.uno_context import create_property_value

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    t_name = TableInsert().execute(
        tool_ctx, rows=2, columns=3, data=[["a1", "b1", "c1"], ["a2", "b2", "c2"]]
    )["table_name"]
    table = doc.getTextTables().getByName(t_name)
    _set_table_separators(table, (1500, 6000))
    got_seps = [int(s.Position) for s in table.getRows().getByIndex(1).getPropertyValue("TableColumnSeparators")]
    assert abs(got_seps[0] - 1500) <= 1 and abs(got_seps[1] - 6000) <= 1, got_seps

    assert TableMergeCells().execute_safe(tool_ctx, name=t_name, cell_range="A1:B1")["status"] == "ok"
    before_cells = TableGetCells().execute(tool_ctx, name=t_name)["cells"]
    before_row1_seps = [int(s.Position) for s in table.getRows().getByIndex(1).getPropertyValue("TableColumnSeparators")]

    ref = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="column", name=t_name, index=0)
    assert ref["status"] == "error" and ref["code"] == "COLUMN_HAS_SPANNING_CELL", ref
    assert "parts=" not in ref["message"], ref["message"]
    assert "table_set_cell" in ref["message"], ref["message"]
    assert TableGetCells().execute(tool_ctx, name=t_name)["cells"] == before_cells
    after_row1_seps = [int(s.Position) for s in table.getRows().getByIndex(1).getPropertyValue("TableColumnSeparators")]
    assert after_row1_seps == before_row1_seps

    # DOCX round trip: widths 1000/4000/9000 with A1:D1 and B2:C2 merged
    doc.getText().setString("")
    docx_tbl_name = TableInsert().execute(
        tool_ctx,
        rows=3,
        columns=4,
        data=[["h1", "h2", "h3", "h4"], ["a2", "b2", "c2", "d2"], ["a3", "b3", "c3", "d3"]],
    )["table_name"]
    docx_tbl = doc.getTextTables().getByName(docx_tbl_name)
    _set_table_separators(docx_tbl, (1000, 4000, 9000))
    assert TableMergeCells().execute_safe(tool_ctx, name=docx_tbl_name, cell_range="A1:D1")["status"] == "ok"
    assert TableMergeCells().execute_safe(tool_ctx, name=docx_tbl_name, cell_range="B2:C2")["status"] == "ok"

    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    fd, docx_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    reloaded = None
    try:
        docx_url = uno.systemPathToFileUrl(docx_path)
        doc.storeToURL(docx_url, (create_property_value("FilterName", "MS Word 2007 XML"),))
        reloaded = desktop.loadComponentFromURL(
            docx_url, "_blank", 0, (create_property_value("Hidden", True),)
        )
        r_ctx = TestingFactory.create_context(doc=reloaded, ctx=ctx, env="native")
        r_names = list(reloaded.getTextTables().getElementNames())
        assert len(r_names) == 1, r_names
        r_name = r_names[0]
        assert reloaded.getTextTables().getByName(r_name).getCellByName("A1").getString() == "h1\nh2\nh3\nh4"
        r_ref = ManageTableStructure().execute_safe(r_ctx, action="delete", axis="column", name=r_name, index=0)
        assert r_ref["status"] == "error" and r_ref["code"] == "COLUMN_HAS_SPANNING_CELL", r_ref
        assert "parts=" not in r_ref["message"], r_ref["message"]
        assert "table_set_cell" in r_ref["message"], r_ref["message"]
    finally:
        if reloaded is not None:
            reloaded.close(True)
        if os.path.exists(docx_path):
            os.remove(docx_path)


@native_test
@with_native_doc("writer", reuse=False)
def test_nested_refusal_followed_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    outer = TableInsert().execute(tool_ctx, rows=2, columns=2, data=[["a1", "b1"], ["a2", "b2"]])["table_name"]
    inner = TableInsert().execute(tool_ctx, rows=1, columns=1, parent=outer, cell="B2", data=[["in"]])["table_name"]

    # Untracked: delete nested table, then row delete succeeds
    ref1 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=outer, index=1)
    assert ref1["status"] == "error" and "table_delete" in ref1["message"], ref1
    assert TableDelete().execute_safe(tool_ctx, name=inner)["status"] == "ok"
    ok1 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=outer, index=1)
    assert ok1["status"] == "ok", ok1

    # Recording: tracked delete of nested table keeps refusal until user accepts
    outer2 = TableInsert().execute(tool_ctx, rows=2, columns=2, data=[["a1", "b1"], ["a2", "b2"]])["table_name"]
    inner2 = TableInsert().execute(tool_ctx, rows=1, columns=1, parent=outer2, cell="B2", data=[["in"]])["table_name"]
    doc.setPropertyValue("RecordChanges", True)
    try:
        ref2 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=outer2, index=1)
        assert ref2["status"] == "error" and "table_delete" in ref2["message"], ref2
        del_inner = TableDelete().execute_safe(tool_ctx, name=inner2)
        assert del_inner["status"] == "ok" and del_inner["pending_review"] is True, del_inner
        ref2_after = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=outer2, index=1)
        assert ref2_after["status"] == "error" and inner2 in ref2_after["message"], ref2_after
        _accept_all_tables(doc, ctx)
        ok2 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=outer2, index=1)
        assert ok2["status"] == "ok" and ok2["pending_review"] is True, ok2
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_tall_cell_refusal_followed_while_recording_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    t_name = TableInsert().execute(
        tool_ctx, rows=3, columns=2, data=[["tall", "b1"], ["", "b2"], ["a3", "b3"]]
    )["table_name"]
    assert TableMergeCells().execute_safe(tool_ctx, name=t_name, cell_range="A1:A2")["status"] == "ok"
    assert TableSetCell().execute_safe(tool_ctx, name=t_name, cell="A1", text="keep")["status"] == "ok"

    doc.setPropertyValue("RecordChanges", True)
    try:
        ref = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t_name, index=0)
        assert ref["status"] == "error" and ref["code"] == "ROW_HAS_SPANNING_CELL", ref
        assert "table_split_cell" in ref["message"] and "table_set_cell" in ref["message"]

        split = TableSplitCell().execute_safe(tool_ctx, name=t_name, cell="A1", direction="rows", parts=2)
        assert split["status"] == "ok", split
        assert TableSetCell().execute_safe(tool_ctx, name=t_name, cell="A2", text="keep")["status"] == "ok"
        del_res = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t_name, index=0)
        assert del_res["status"] == "ok" and del_res["pending_review"] is True, del_res
        _accept_all_tables(doc, ctx)
        cells = TableGetCells().execute(tool_ctx, name=t_name)["cells"]
        assert cells == {"A1": "keep", "B1": "b2", "A2": "a3", "B2": "b3"}, cells
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_tracked_row_deletes_keep_indexes_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    t_name = TableInsert().execute(
        tool_ctx,
        rows=4,
        columns=2,
        data=[["r1", "b1"], ["r2", "b2"], ["r3", "b3"], ["r4", "b4"]],
    )["table_name"]

    doc.setPropertyValue("RecordChanges", True)
    try:
        d1 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t_name, index=1)
        assert d1["status"] == "ok" and d1["rows"] == 4 and d1["tracked"] is True and d1["pending_review"] is True, d1
        assert "later indexes do not shift" in d1["message"], d1

        d2 = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name=t_name, index=2)
        assert d2["status"] == "ok" and d2["rows"] == 4 and d2["tracked"] is True and d2["pending_review"] is True, d2
        assert "later indexes do not shift" in d2["message"], d2

        cells = TableGetCells().execute(tool_ctx, name=t_name)["cells"]
        assert cells == {"A1": "r1", "B1": "b1", "A2": "", "B2": "", "A3": "", "B3": "", "A4": "r4", "B4": "b4"}, cells

        _accept_all_tables(doc, ctx)
        after_accept = TableGetCells().execute(tool_ctx, name=t_name)["cells"]
        assert after_accept == {"A1": "r1", "B1": "b1", "A2": "r4", "B2": "b4"}, after_accept

        # Row insert while recording shifts indexes at once
        ins = ManageTableStructure().execute_safe(tool_ctx, action="insert", axis="row", name=t_name, index=1)
        assert ins["status"] == "ok" and ins["rows"] == 3, ins
        after_ins = TableGetCells().execute(tool_ctx, name=t_name)["cells"]
        assert after_ins["A3"] == "r4", after_ins
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_protected_section_refusal_while_recording_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    text.insertString(text.getEnd(), "Before section\n", False)
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setName("ProtSec")
    text.insertTextContent(text.getEnd(), sec, False)

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    tbl.setName("SecTbl")
    sec_anchor = sec.getAnchor()
    sec_anchor.getText().insertTextContent(sec_anchor.getStart(), tbl, False)
    tbl.getCellByName("A1").setString("s1")
    tbl.getCellByName("A2").setString("s2")
    sec.setPropertyValue("IsProtected", True)

    doc.setPropertyValue("RecordChanges", True)
    try:
        err_row = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name="SecTbl", index=0)
        assert err_row["status"] == "error" and err_row["code"] == "TABLE_DELETE_NOT_TRACKABLE", err_row
        assert "protected section" in err_row["message"] and "did not record" in err_row["message"], err_row
        assert tbl.getRows().getCount() == 2
        assert tbl.getCellByName("A2").getString() == "s2"

        err_tbl = TableDelete().execute_safe(tool_ctx, name="SecTbl")
        assert err_tbl["status"] == "error" and err_tbl["code"] == "TABLE_DELETE_NOT_TRACKABLE", err_tbl
        assert "protected section" in err_tbl["message"] and "did not record" in err_tbl["message"], err_tbl
        assert doc.getTextTables().hasByName("SecTbl")
        assert tbl.getCellByName("A1").getString() == "s1"
        assert tbl.getCellByName("A2").getString() == "s2"

        sec.setPropertyValue("IsProtected", False)
        ok_row = ManageTableStructure().execute_safe(tool_ctx, action="delete", axis="row", name="SecTbl", index=0)
        assert ok_row["status"] == "ok" and ok_row["pending_review"] is True, ok_row
    finally:
        sec.setPropertyValue("IsProtected", False)
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_table_delete_protected_cell_refused_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()

    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("ProtOuter")
    outer.getCellByName("A1").setString("o1")
    outer.getCellByName("B1").setString("o2")
    outer.getCellByName("A2").setString("o3")
    outer.getCellByName("B2").setString("o4")

    undo_mgr = doc.getUndoManager()
    try:
        for recording in (False, True):
            doc.setPropertyValue("RecordChanges", recording)
            outer.getCellByName("B2").setPropertyValue("IsProtected", True)
            cells_before = {c: outer.getCellByName(c).getString() for c in ("A1", "B1", "A2", "B2")}
            undo_before = list(undo_mgr.getAllUndoActionTitles())
            redlines_before = doc.getRedlines().getCount()

            res = TableDelete().execute_safe(tool_ctx, name="ProtOuter")
            assert res["status"] == "error" and res["code"] == "CELLS_PROTECTED", res
            assert "B2" in res["message"]
            assert doc.getTextTables().hasByName("ProtOuter")
            assert {
                c: outer.getCellByName(c).getString() for c in ("A1", "B1", "A2", "B2")
            } == cells_before
            assert doc.getRedlines().getCount() == redlines_before
            assert list(undo_mgr.getAllUndoActionTitles()) == undo_before

            outer.getCellByName("B2").setPropertyValue("IsProtected", False)
            inner = doc.createInstance("com.sun.star.text.TextTable")
            inner.initialize(1, 1)
            host = outer.getCellByName("B2")
            host.insertTextContent(host.getEnd(), inner, False)
            inner.setName("ProtInner")
            inner.getCellByName("A1").setString("in1")
            host.setPropertyValue("IsProtected", True)
            redlines_before_in = doc.getRedlines().getCount()

            res_in = TableDelete().execute_safe(tool_ctx, name="ProtInner")
            assert res_in["status"] == "error" and res_in["code"] == "CELLS_PROTECTED", res_in
            assert "ProtOuter" in res_in["message"]
            assert doc.getTextTables().hasByName("ProtInner")
            assert doc.getRedlines().getCount() == redlines_before_in

            if not recording:
                host.setPropertyValue("IsProtected", False)
                TableDelete().execute_safe(tool_ctx, name="ProtInner")
                assert not doc.getTextTables().hasByName("ProtInner")

        # Protected child cell does not block deleting the outer table.
        host.setPropertyValue("IsProtected", False)
        inner.getCellByName("A1").setPropertyValue("IsProtected", True)
        res_outer = TableDelete().execute_safe(tool_ctx, name="ProtOuter")
        assert res_outer["status"] == "ok" and res_outer["pending_review"] is True, res_outer
    finally:
        doc.setPropertyValue("RecordChanges", False)
