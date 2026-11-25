# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import unittest

from plugin.testing_runner import current_office, native_test
from plugin.tests.testing_utils import (
    TestingFactory,
    execute_calc_tool as _execute_calc_tool,
    with_native_doc,
)


@native_test
@with_native_doc("calc")
def test_charts_creation_and_listing(ctx, doc):
    active_sheet = doc.getCurrentController().getActiveSheet()

    # 1. Populate sample data
    data = [
        "Month", "Sales",
        "Jan", "100",
        "Feb", "150",
        "Mar", "200",
        "Apr", "250",
        "May", "300"
    ]
    res_write = _execute_calc_tool(doc, ctx, "write_range", {"range": "A1:B6", "values": data})
    assert res_write.get("status") == "ok", f"write_range failed: {res_write}"

    # 2. Create chart
    res_create = _execute_calc_tool(doc, ctx, "chart_upsert", {"data_range": "A1:B6", "chart_type": "bar"})
    assert res_create.get("status") == "ok", f"create_chart failed: {res_create}"

    # 3. List charts
    res_list = _execute_calc_tool(doc, ctx, "chart_list", {})
    assert res_list.get("status") == "ok", f"chart_list failed: {res_list}"
    charts = res_list.get("charts", [])
    assert len(charts) == 1, f"Expected 1 chart, found {len(charts)}"
    chart_name = charts[0].get("name")
    assert chart_name is not None, "Chart name should not be None"

    # 4. Query DrawPage for OLE2Shape
    draw_page = active_sheet.getDrawPage()
    found_chart_shape = False
    for i in range(draw_page.getCount()):
        shape = draw_page.getByIndex(i)
        if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape":
            found_chart_shape = True
            break
    assert found_chart_shape, "com.sun.star.drawing.OLE2Shape not found on DrawPage"

    # 5. Get chart info
    res_info = _execute_calc_tool(doc, ctx, "chart_get_info", {"name": chart_name})
    assert res_info.get("status") == "ok", f"chart_get_info failed: {res_info}"
    assert res_info.get("name") == chart_name, "Chart info name mismatch"

    # 6. Edit chart
    res_edit = _execute_calc_tool(doc, ctx, "chart_upsert", {"name": chart_name, "title": "Monthly Sales"})
    assert res_edit.get("status") == "ok", f"edit_chart failed: {res_edit}"

    # Verify title change
    res_info_after_edit = _execute_calc_tool(doc, ctx, "chart_get_info", {"name": chart_name})
    assert res_info_after_edit.get("title") == "Monthly Sales", f"Chart title not updated: {res_info_after_edit}"

    # 7. Delete chart
    res_delete = _execute_calc_tool(doc, ctx, "chart_delete", {"name": chart_name})
    assert res_delete.get("status") == "ok", f"chart_delete failed: {res_delete}"

    # Verify deletion
    res_list_after_delete = _execute_calc_tool(doc, ctx, "chart_list", {})
    assert len(res_list_after_delete.get("charts", [])) == 0, "Chart not deleted"


@unittest.skipIf(not current_office().options.show_window, "Writer/Calc array test requires visible window for event execution")
@native_test
@with_native_doc("calc")
def test_charts_validation_and_writer_arrays(ctx, doc):
    # 1. Calc validation checks
    # Create chart with headers/rows should fail in Calc
    res = _execute_calc_tool(doc, ctx, "chart_upsert", {
        "chart_type": "bar",
        "headers": ["Month", "Sales"],
        "rows": [["Jan", 100], ["Feb", 150]]
    })
    # Schema strips the other doc type's fields before validation (docs/calc/charts.md).
    assert res.get("status") == "error"
    assert res.get("code") == "VALIDATION_ERROR"
    assert "data_range" in res.get("message", "")
    assert res["details"]["ignored_parameters"] == ["headers", "rows"]

    # Create chart without data_range should fail in Calc
    res = _execute_calc_tool(doc, ctx, "chart_upsert", {
        "chart_type": "bar"
    })
    assert res.get("status") == "error"
    assert "data_range" in res.get("message", "")

    # 2. Writer chart creation and array mapping validation
    writer_doc = TestingFactory.create_native_doc(ctx, "writer", hidden=not current_office().options.show_window)
    try:
        from plugin.app import get_tools, get_services

        writer_ctx = TestingFactory.create_context(
            doc=writer_doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
        )

        # Create chart with data_range should fail in Writer due to missing headers/rows
        res_fail = get_tools().execute("chart_upsert", writer_ctx, chart_type="bar", data_range="A1:B6")
        # Schema strips the other doc type's fields before validation (docs/calc/charts.md).
        assert res_fail.get("status") == "error"
        assert res_fail.get("code") == "VALIDATION_ERROR"
        assert "Both 'headers' and 'rows' are required" in res_fail.get("message", "")
        assert res_fail["details"]["ignored_parameters"] == ["data_range"]

        # Create chart without headers/rows should fail in Writer
        res_fail2 = get_tools().execute("chart_upsert", writer_ctx, chart_type="bar")
        assert res_fail2.get("status") == "error"
        assert "Both 'headers' and 'rows' are required" in res_fail2.get("message", "")

        # Create chart with headers and rows should succeed in Writer
        headers = ["Month", "Sales", "Expenses"]
        rows = [["Jan", 100, 80], ["Feb", 150, 110], ["Mar", 200, 130]]
        res_ok = get_tools().execute(
            "chart_upsert", writer_ctx,
            chart_type="bar",
            headers=headers,
            rows=rows,
            title="Writer Chart",
            colors=["#FF0000"],
        )
        assert res_ok.get("status") == "ok", f"Writer chart creation failed: {res_ok}"
        chart_name = res_ok.get("name")
        assert chart_name is not None

        # Query OLE2Shape in Writer document and verify XChartDataArray
        objects = writer_doc.getEmbeddedObjects()
        assert objects.hasByName(chart_name), f"Chart '{chart_name}' not found in embedded objects"

        # Verify underlying ChartData structure
        draw_page = writer_doc.getDrawPage()
        assert draw_page.getCount() >= 1, "No chart shape created in Writer draw page"

        chart_shape = None
        for i in range(draw_page.getCount()):
            shape = draw_page.getByIndex(i)
            if shape.supportsService("com.sun.star.drawing.OLE2Shape") and shape.CLSID == "12d37028-0b55-463d-863a-211463e2c59f":
                chart_shape = shape
                break

        assert chart_shape is not None, "OLE2 chart shape not found"
        chart_doc = chart_shape.getEmbeddedObject()
        chart_data = chart_doc.getData()

        row_desc = chart_data.getRowDescriptions()
        col_desc = chart_data.getColumnDescriptions()
        data_matrix = chart_data.getData()

        assert row_desc == ("Jan", "Feb", "Mar"), f"Row descriptions mismatch: {row_desc}"
        assert col_desc == ("Sales", "Expenses"), f"Column descriptions mismatch: {col_desc}"
        assert data_matrix == ((100.0, 80.0), (150.0, 110.0), (200.0, 130.0)), f"Data matrix mismatch: {data_matrix}"

        # Read back series color
        diag = chart_doc.getFirstDiagram()
        assert diag is not None, "Expected diagram on Writer chart"
        coords = diag.getCoordinateSystems()
        assert coords, "Expected coordinate systems on Writer chart"
        ctypes = coords[0].getChartTypes()
        assert ctypes, "Expected chart types on Writer chart"
        series_list = ctypes[0].getDataSeries()
        assert series_list, "Expected data series on Writer chart"
        s0 = series_list[0]
        prop_info = s0.getPropertySetInfo()
        color_prop = next((p for p in ("FillColor", "Color") if prop_info.hasPropertyByName(p)), None)
        assert color_prop is not None, "Expected FillColor or Color property on data series"
        assert s0.getPropertyValue(color_prop) == 0xFF0000

        # Test edit in Writer
        new_headers = ["Period", "Revenue"]
        new_rows = [["Q1", 500], ["Q2", 600]]
        res_edit = get_tools().execute(
            "chart_upsert", writer_ctx,
            name=chart_name,
            headers=new_headers,
            rows=new_rows,
            title="Edited Writer Chart"
        )
        assert res_edit.get("status") == "ok", f"Writer chart_upsert edit failed: {res_edit}"

        # Delete by name, assert hasByName is false and draw-page OLE shape is gone
        res_del = get_tools().execute("chart_delete", writer_ctx, name=chart_name)
        assert res_del.get("status") == "ok", f"Writer chart_delete failed: {res_del}"
        assert not objects.hasByName(chart_name)
        found_deleted_shape = False
        for i in range(draw_page.getCount()):
            shape = draw_page.getByIndex(i)
            if shape.supportsService("com.sun.star.drawing.OLE2Shape") and (getattr(shape, "Name", None) == chart_name or shape == chart_shape):
                found_deleted_shape = True
                break
        assert not found_deleted_shape, "Draw-page OLE shape still present after delete"

    finally:
        TestingFactory.close_doc(writer_doc)


@native_test
@with_native_doc("calc")
def test_chart_from_named_data_range(ctx, doc):
    from com.sun.star.table import CellAddress

    active_sheet = doc.getCurrentController().getActiveSheet()
    data = [
        "Month", "Sales",
        "Jan", "100",
        "Feb", "150",
        "Mar", "200",
        "Apr", "250",
        "May", "300",
    ]
    res_write = _execute_calc_tool(doc, ctx, "write_range", {"range": "A1:B6", "values": data})
    assert res_write.get("status") == "ok", f"write_range failed: {res_write}"

    named_ranges = doc.NamedRanges
    if named_ranges.hasByName("ChartData"):
        named_ranges.removeByName("ChartData")
    named_ranges.addNewByName("ChartData", "$Sheet1.$A$1:$B$6", CellAddress(Sheet=0, Column=0, Row=0), 0)
    chart_name = None
    try:
        res_create = _execute_calc_tool(doc, ctx, "chart_upsert", {"data_range": "ChartData", "chart_type": "bar"})
        assert res_create.get("status") == "ok", f"chart_upsert create failed: {res_create}"

        res_list = _execute_calc_tool(doc, ctx, "chart_list", {})
        charts = res_list.get("charts", [])
        assert len(charts) == 1
        chart_name = charts[0]["name"]

        chart_obj = active_sheet.getCharts().getByName(chart_name)
        addr = chart_obj.getRanges()[0]
        assert (addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow) == (0, 0, 1, 5)

        res_edit = _execute_calc_tool(doc, ctx, "chart_upsert", {"name": chart_name, "data_range": "ChartData"})
        assert res_edit.get("status") == "ok", f"chart_upsert edit failed: {res_edit}"

        named_ranges.removeByName("ChartData")
        named_ranges.addNewByName("ChartData", "$Sheet1.$A$1:$B$4", CellAddress(Sheet=0, Column=0, Row=0), 0)
        res_edit2 = _execute_calc_tool(doc, ctx, "chart_upsert", {"name": chart_name, "data_range": "ChartData"})
        assert res_edit2.get("status") == "ok", f"chart_upsert edit2 failed: {res_edit2}"
        addr2 = chart_obj.getRanges()[0]
        assert (addr2.StartColumn, addr2.StartRow, addr2.EndColumn, addr2.EndRow) == (0, 0, 1, 3)
    finally:
        if chart_name is not None:
            _execute_calc_tool(doc, ctx, "chart_delete", {"name": chart_name})
        if named_ranges.hasByName("ChartData"):
            named_ranges.removeByName("ChartData")


@native_test
@with_native_doc("writer")
def test_writer_chart_while_recording_reject_removes_uno(ctx, doc):
    from plugin.calc.charts import ChartUpsert
    from plugin.framework.tool import ToolContext
    from plugin.framework.uno_context import execute_dispatch

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ChartUpsert().execute(
            tctx,
            chart_type="bar",
            headers=["A", "B"],
            rows=[["X", 10], ["Y", 20]],
        )
        assert res["status"] == "ok"
        assert res["tracked"] is True and res["pending_review"] is True
        assert len(doc.getEmbeddedObjects().getElementNames()) == 1

        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert len(doc.getEmbeddedObjects().getElementNames()) == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_chart_delete_writer_anchors_uno(ctx, doc):
    from com.sun.star.awt import Size
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_PAGE

    from plugin.calc.charts import CHART_CLSID_DRAW_OLE, ChartDelete

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Body text", False)

    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cur, table, False)
    cell_text = table.getCellByName("A1")
    cell_cur = cell_text.createTextCursor()

    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.setPropertyValue("Size", Size(6000, 4000))
    text.insertTextContent(cur, frame, False)
    frame_text = frame.getText()
    frame_cur = frame_text.createTextCursor()

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    hosts = [
        ("PageChart", text, cur, AT_PAGE, 1),
        ("CellChart", cell_text, cell_cur, AS_CHARACTER, None),
        ("FrameChart", frame_text, frame_cur, AS_CHARACTER, None),
    ]
    for name, host_text, host_cur, anchor_type, page_no in hosts:
        embed = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
        embed.CLSID = CHART_CLSID_DRAW_OLE.upper()
        embed.setPropertyValue("Size", Size(4000, 3000))
        embed.setPropertyValue("AnchorType", anchor_type)
        if page_no is not None:
            embed.setPropertyValue("AnchorPageNo", page_no)
        host_text.insertTextContent(host_cur, embed, False)
        embed.setName(name)
        assert doc.getEmbeddedObjects().hasByName(name)

        res = ChartDelete().execute_safe(tool_ctx, name=name)
        assert res.get("status") == "ok", res
        assert not doc.getEmbeddedObjects().hasByName(name)

        if anchor_type == AT_PAGE:
            doc.getUndoManager().undo()
            assert doc.getEmbeddedObjects().hasByName(name)
            assert doc.getEmbeddedObjects().getByName(name).getPropertyValue("AnchorType") == AT_PAGE
            res2 = ChartDelete().execute_safe(tool_ctx, name=name)
            assert res2.get("status") == "ok", res2
            assert not doc.getEmbeddedObjects().hasByName(name)


@native_test
@with_native_doc("writer")
def test_writer_chart_position_cursor_uses_pinned_caret_uno(ctx, doc):
    from plugin.calc.charts import ChartUpsert
    from plugin.doc.text_helpers import selection_cursor_snapshot
    from plugin.framework.tool import SelectionPin

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha beta gamma", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second para", False)
    cur.gotoStart(False)
    cur.goRight(6, False)
    pinned_point = cur.getStart()
    controller = doc.getCurrentController()
    controller.select(cur)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    tool_ctx.selection_pin = SelectionPin(selection_cursor_snapshot(doc), doc)
    controller.getViewCursor().gotoEnd(False)

    res = ChartUpsert().execute_safe(
        tool_ctx,
        headers=["A", "B"],
        rows=[["X", 1], ["Y", 2]],
        chart_type="bar",
        position="cursor",
    )
    assert res.get("status") == "ok", res
    embeds = doc.getEmbeddedObjects()
    assert embeds.getCount() == 1
    embed = embeds.getByIndex(0)
    assert text.compareRegionStarts(embed.getAnchor().getStart(), pinned_point) == 0
