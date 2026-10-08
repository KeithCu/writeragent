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

from plugin.testing_runner import native_test, show_window
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
    res_write = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": "A1:B6", "values": data})
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    # 2. Create chart
    res_create = _execute_calc_tool(doc, ctx, "manage_charts", {"action": "create", "data_range": "A1:B6", "chart_type": "bar"})
    assert res_create.get("status") == "ok", f"create_chart failed: {res_create}"

    # 3. List charts
    res_list = _execute_calc_tool(doc, ctx, "manage_charts", {"action": "list"})
    assert res_list.get("status") == "ok", f"manage_charts list failed: {res_list}"
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
    res_info = _execute_calc_tool(doc, ctx, "manage_charts", {"action": "get_info", "name": chart_name})
    assert res_info.get("status") == "ok", f"manage_charts get_info failed: {res_info}"
    assert res_info.get("name") == chart_name, "Chart info name mismatch"

    # 6. Edit chart
    res_edit = _execute_calc_tool(doc, ctx, "manage_charts", {"action": "edit", "name": chart_name, "title": "Monthly Sales"})
    assert res_edit.get("status") == "ok", f"edit_chart failed: {res_edit}"

    # Verify title change
    res_info_after_edit = _execute_calc_tool(doc, ctx, "manage_charts", {"action": "get_info", "name": chart_name})
    assert res_info_after_edit.get("title") == "Monthly Sales", f"Chart title not updated: {res_info_after_edit}"

    # 7. Delete chart
    res_delete = _execute_calc_tool(doc, ctx, "manage_charts", {"action": "delete", "name": chart_name})
    assert res_delete.get("status") == "ok", f"manage_charts delete failed: {res_delete}"

    # Verify deletion
    res_list_after_delete = _execute_calc_tool(doc, ctx, "manage_charts", {"action": "list"})
    assert len(res_list_after_delete.get("charts", [])) == 0, "Chart not deleted"


@unittest.skipIf(not show_window, "Writer/Calc array test requires visible window for event execution")
@native_test
@with_native_doc("calc")
def test_charts_validation_and_writer_arrays(ctx, doc):
    # 1. Calc validation checks
    # Create chart with headers/rows should fail in Calc
    res = _execute_calc_tool(doc, ctx, "manage_charts", {
        "action": "create",
        "chart_type": "bar",
        "headers": ["Month", "Sales"],
        "rows": [["Jan", 100], ["Feb", 150]]
    })
    assert res.get("status") == "error"
    assert "data_range is required for Calc charts" in res.get("message", "")

    # Create chart without data_range should fail in Calc
    res = _execute_calc_tool(doc, ctx, "manage_charts", {
        "action": "create",
        "chart_type": "bar"
    })
    assert res.get("status") == "error"
    assert "data_range is required for Calc charts" in res.get("message", "")

    # 2. Writer chart creation and array mapping validation
    writer_doc = TestingFactory.create_native_doc(ctx, "writer", hidden=not show_window)
    try:
        from plugin.main import get_tools, get_services

        writer_ctx = TestingFactory.create_context(
            doc=writer_doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
        )

        # Create chart with data_range should fail in Writer due to missing headers/rows
        res_fail = get_tools().execute("manage_charts", writer_ctx, action="create", chart_type="bar", data_range="A1:B6")
        assert res_fail.get("status") == "error"
        assert "Both 'headers' and 'rows' are required" in res_fail.get("message", "")

        # Create chart without headers/rows should fail in Writer
        res_fail2 = get_tools().execute("manage_charts", writer_ctx, action="create", chart_type="bar")
        assert res_fail2.get("status") == "error"
        assert "Both 'headers' and 'rows' are required" in res_fail2.get("message", "")

        # Create chart with headers and rows should succeed in Writer
        headers = ["Month", "Sales", "Expenses"]
        rows = [["Jan", 100, 80], ["Feb", 150, 110], ["Mar", 200, 130]]
        res_ok = get_tools().execute(
            "manage_charts", writer_ctx,
            action="create",
            chart_type="bar",
            headers=headers,
            rows=rows,
            title="Writer Chart"
        )
        assert res_ok.get("status") == "ok", f"Writer chart creation failed: {res_ok}"
        chart_name = res_ok.get("name")
        assert chart_name is not None

        # Query OLE2Shape in Writer document and verify XChartDataArray
        objects = writer_doc.getEmbeddedObjects()
        assert objects.hasByName(chart_name), f"Chart '{chart_name}' not found in embedded objects"
        assert res_ok.get("status") == "ok", f"Writer manage_charts create failed: {res_ok}"

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

        # Test edit action in Writer
        new_headers = ["Period", "Revenue"]
        new_rows = [["Q1", 500], ["Q2", 600]]
        res_edit = get_tools().execute(
            "manage_charts", writer_ctx,
            action="edit",
            name=chart_name,
            headers=new_headers,
            rows=new_rows,
            title="Edited Writer Chart"
        )
        assert res_edit.get("status") == "ok", f"Writer manage_charts edit failed: {res_edit}"

    finally:
        TestingFactory.close_doc(writer_doc)


@native_test
def test_charts_schema_filtering():
    from plugin.main import get_tools

    manage_charts_tool = get_tools().get("manage_charts")
    assert manage_charts_tool is not None

    # Test Calc filtering
    calc_params = manage_charts_tool.get_parameters("calc")
    assert calc_params is not None
    assert "data_range" in calc_params["properties"]
    assert "headers" not in calc_params["properties"]
    assert "rows" not in calc_params["properties"]

    # Test Writer filtering
    writer_params = manage_charts_tool.get_parameters("writer")
    assert writer_params is not None
    assert "data_range" not in writer_params["properties"]
    assert "headers" in writer_params["properties"]
    assert "rows" in writer_params["properties"]

    # Test Draw filtering
    draw_params = manage_charts_tool.get_parameters("draw")
    assert draw_params is not None
    assert "data_range" not in draw_params["properties"]
    assert "headers" in draw_params["properties"]
    assert "rows" in draw_params["properties"]
