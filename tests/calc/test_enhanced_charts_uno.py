# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu 
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
"""Tests for enhanced chart tools in Calc, Writer, and Impress."""

import unittest

from plugin.testing_runner import native_test, show_window
from tests.testing_utils import TestingFactory, with_native_doc


def _execute(doc, ctx, name, args, domain="calc"):
    return TestingFactory.execute_tool(doc, ctx, name, args, doc_type=domain)


@native_test
@with_native_doc("calc", hidden=not show_window)
def test_calc_enhanced_chart(ctx, doc):
    # 1. Setup data
    _execute(doc, ctx, "write_formula_range", {"range": "A1:B3", "values": [["A", 1], ["B", 2], ["C", 3]]})
    
    # 2. Create 3D Stacked Chart
    res = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "data_range": "A1:B3",
        "chart_type": "column",
        "is_3d": True,
        "stacked": True,
        "title": "3D Chart Test",
        "x_axis_title": "X Axis",
        "y_axis_title": "Y Axis",
        "legend_position": "bottom"
    })
    assert res.get("status") == "ok", f"Create failed: {res}"
    chart_name = res.get("name")
    
    # 3. Verify Info
    info = _execute(doc, ctx, "manage_charts", {"action": "get_info", "name": chart_name})
    assert info.get("status") == "ok"
    assert info.get("is_3d") is True
    assert info.get("stacked") is True
    assert info.get("title") == "3D Chart Test"
    assert info.get("x_axis_title") == "X Axis"
    assert info.get("y_axis_title") == "Y Axis"
    
    # 4. Edit properties
    edit_res = _execute(doc, ctx, "manage_charts", {
        "action": "edit",
        "name": chart_name,
        "is_3d": False,
        "legend_position": "top",
        "y_axis_title": "New Y"
    })
    assert edit_res.get("status") == "ok"
    
    info2 = _execute(doc, ctx, "manage_charts", {"action": "get_info", "name": chart_name})
    assert info2.get("is_3d") is False
    assert info2.get("y_axis_title") == "New Y"


@native_test
@with_native_doc("calc", hidden=not show_window)
def test_calc_chart_colors(ctx, doc):
    # 1. Setup data
    _execute(doc, ctx, "write_formula_range", {"range": "A1:B3", "values": [["A", 1], ["B", 2], ["C", 3]]})

    # 2. Create Chart with custom/arbitrary colors (RGB and hex)
    res = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "data_range": "A1:B3",
        "chart_type": "column",
        "bg_color": "rgba(255, 0, 0, 0.5)",  # Red background via functional rgb
        "colors": ["#00FF00", "blue"]  # green and blue series
    })
    assert res.get("status") == "ok", f"Create with colors failed: {res}"
    chart_name = res.get("name")

    # Read back first series color
    sheet = doc.getSheets().getByIndex(0)
    chart_obj = sheet.getCharts().getByName(chart_name)
    chart_doc = chart_obj.getEmbeddedObject()
    diag = chart_doc.getFirstDiagram()
    if diag:
        coords = diag.getCoordinateSystems()
        if coords:
            ctypes = coords[0].getChartTypes()
            if ctypes:
                series_list = ctypes[0].getDataSeries()
                if series_list:
                    s0 = series_list[0]
                    for prop in ["FillColor", "Color"]:
                        if s0.getPropertySetInfo().hasPropertyByName(prop):
                            assert s0.getPropertyValue(prop) == 0x00FF00
                            break

    # 3. Edit chart with another color (e.g. shorthand hex and CSS name)
    edit_res = _execute(doc, ctx, "manage_charts", {
        "action": "edit",
        "name": chart_name,
        "bg_color": "yellow",
        "colors": ["#0f0"]
    })
    assert edit_res.get("status") == "ok", f"Edit with colors failed: {edit_res}"


@unittest.skip("Disabled as per user request: internal test causing problems")
@native_test
@with_native_doc("writer", hidden=not show_window)
def test_writer_chart_polymorphic(ctx, doc):
    # 1. Create in Writer
    res = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "chart_type": "pie",
        "title": "Writer Pie"
    }, domain="writer")
    assert res.get("status") == "ok", f"Writer create failed: {res}"
    name = res.get("name")

    probe = _execute(doc, ctx, "manage_charts", {"action": "get_info", "name": name}, domain="writer")
    if probe.get("status") != "ok":
        raise unittest.SkipTest(
            "Writer chart embed not available in this LibreOffice runtime "
            f"(manage_charts get_info: {probe!r}). OLE insert may be disabled in headless/pyuno."
        )

    # 2. List in Writer
    list_res = _execute(doc, ctx, "manage_charts", {"action": "list"}, domain="writer")
    assert list_res.get("status") == "ok", f"manage_charts list failed: {list_res}"
    names = [c["name"] for c in list_res.get("charts", [])]
    assert name in names, (
        f"chart name {name!r} not in manage_charts list names {names!r}; full list_res={list_res!r}"
    )
    
    # 3. Info
    info = _execute(doc, ctx, "manage_charts", {"action": "get_info", "name": name}, domain="writer")
    assert info.get("title") == "Writer Pie"
    assert "PieDiagram" in info.get("diagram_type", "")


@unittest.skipIf(not show_window, "Draw/Impress chart create_chart hangs in headless testing_runner (processEventsToIdle in charts.py)")
@native_test
@with_native_doc("impress", hidden=not show_window)
def test_draw_chart_polymorphic(ctx, doc):
    # 1. Create in Draw
    res = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "chart_type": "line",
        "title": "Slide Chart",
        "is_3d": True
    }, domain="draw")
    assert res.get("status") == "ok", f"Draw create failed: {res}"
    name = res.get("name")
    
    # 2. Info
    info = _execute(doc, ctx, "manage_charts", {"action": "get_info", "name": name}, domain="draw")
    assert info.get("is_3d") is True
    assert info.get("title") == "Slide Chart"
    
    # 3. Delete
    del_res = _execute(doc, ctx, "manage_charts", {"action": "delete", "name": name}, domain="draw")
    assert del_res.get("status") == "ok"
    
    list_res = _execute(doc, ctx, "manage_charts", {"action": "list"}, domain="draw")
    assert len(list_res.get("charts", [])) == 0


@native_test
@with_native_doc("calc", hidden=not show_window)
def test_calc_multisheet_charts(ctx, doc):
    # 1. Setup data on active sheet (Sheet1)
    _execute(doc, ctx, "write_formula_range", {"range": "A1:B3", "values": [["A", 10], ["B", 20], ["C", 30]]})

    # 2. Insert a second sheet "Dashboard"
    if not doc.getSheets().hasByName("Dashboard"):
        doc.getSheets().insertNewByName("Dashboard", 1)

    # 3. Create Chart 0 on active sheet (Sheet1)
    res1 = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "data_range": "A1:B3",
        "chart_type": "column",
        "title": "Sheet1 Chart"
    })
    assert res1.get("status") == "ok", f"Create 1 failed: {res1}"
    c0 = res1.get("name")
    assert c0 == "Chart_0"
    assert res1.get("sheet") == "Sheet1"

    # 4. Create Chart 1 placed on "Dashboard" with data from Sheet1
    res2 = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "sheet": "Dashboard",
        "data_range": "'Sheet1'.A1:B3",
        "chart_type": "line",
        "title": "Dashboard Chart"
    })
    assert res2.get("status") == "ok", f"Create 2 failed: {res2}"
    c1 = res2.get("name")
    assert c1 == "Chart_1", f"Expected Chart_1, got {c1!r}"
    assert res2.get("sheet") == "Dashboard"

    # 5. List charts across entire document
    list_res = _execute(doc, ctx, "manage_charts", {"action": "list"})
    assert list_res.get("status") == "ok"
    charts_by_name = {c["name"]: c for c in list_res.get("charts", [])}
    assert "Chart_0" in charts_by_name
    assert "Chart_1" in charts_by_name
    assert charts_by_name["Chart_0"].get("sheet_name") == "Sheet1"
    assert charts_by_name["Chart_1"].get("sheet_name") == "Dashboard"

    # 6. Edit chart on non-active sheet (Dashboard) WITHOUT passing sheet_name (auto-resolution fallback test)
    edit_res = _execute(doc, ctx, "manage_charts", {
        "action": "edit",
        "name": "Chart_1",
        "title": "Updated Dashboard Chart"
    })
    assert edit_res.get("status") == "ok"

    # Edit data_range and verify get_info data_ranges matches _range_to_str
    edit_data_res = _execute(doc, ctx, "manage_charts", {
        "action": "edit",
        "name": "Chart_1",
        "data_range": "'Sheet1'.A1:B2",
    })
    assert edit_data_res.get("status") == "ok"
    from plugin.calc.bridge import CalcBridge
    bridge = CalcBridge(doc)
    sheet1 = doc.getSheets().getByName("Sheet1")
    expected_range_str = bridge._range_to_str(sheet1.getCellRangeByName("A1:B2").getRangeAddress())
    info_c1 = _execute(doc, ctx, "manage_charts", {"action": "get_info", "name": "Chart_1"})
    assert info_c1.get("data_ranges") == [expected_range_str]

    # 7. Create Chart 2 with has_header=False
    res3 = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "sheet": "Dashboard",
        "data_range": "'Sheet1'.A1:B3",
        "chart_type": "pie",
        "has_header": False,
        "title": "No Header Chart"
    })
    assert res3.get("status") == "ok"
    assert res3.get("name") == "Chart_2"

    dashboard_sheet = doc.getSheets().getByName("Dashboard")
    c2_chart = dashboard_sheet.getCharts().getByName("Chart_2")
    assert c2_chart.getHasColumnHeaders() is False
    assert c2_chart.getHasRowHeaders() is False

    # Edit Chart_0 with has_header=False and read back flags
    edit_hdr_res = _execute(doc, ctx, "manage_charts", {
        "action": "edit",
        "name": "Chart_0",
        "has_header": False,
    })
    assert edit_hdr_res.get("status") == "ok"
    sheet1_chart = sheet1.getCharts().getByName("Chart_0")
    assert sheet1_chart.getHasColumnHeaders() is False
    assert sheet1_chart.getHasRowHeaders() is False

    del_res = _execute(doc, ctx, "manage_charts", {
        "action": "delete",
        "name": "Chart_1"
    })
    assert del_res.get("status") == "ok"
    assert del_res.get("sheet") == "Dashboard"

    # Verify remaining charts
    list_res2 = _execute(doc, ctx, "manage_charts", {"action": "list"})
    remaining_names = [c["name"] for c in list_res2.get("charts", [])]
    assert "Chart_0" in remaining_names
    assert "Chart_2" in remaining_names
    assert "Chart_1" not in remaining_names


@unittest.skipIf(not show_window, "Draw/Impress chart create_chart hangs in headless testing_runner (processEventsToIdle in charts.py)")
@native_test
@with_native_doc("impress", hidden=not show_window)
def test_impress_grouped_and_multipage_chart(ctx, doc):
    # 1. Create chart on first slide (Chart_0)
    res0 = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "chart_type": "column",
        "title": "Slide 1 Chart",
    }, domain="draw")
    assert res0.get("status") == "ok", f"Create 0 failed: {res0}"
    assert res0.get("name") == "Chart_0"

    # 2. Group Chart_0 with a rectangle shape
    page0 = doc.getDrawPages().getByIndex(0)
    rect = doc.createInstance("com.sun.star.drawing.RectangleShape")
    page0.add(rect)
    from plugin.draw.shapes import _create_shape_collection

    col = _create_shape_collection(ctx)
    chart_shape = None
    for i in range(page0.getCount()):
        s = page0.getByIndex(i)
        if getattr(s, "Name", None) == "Chart_0":
            chart_shape = s
            break
    assert chart_shape is not None
    col.add(chart_shape)
    col.add(rect)
    group = page0.group(col)
    assert group is not None

    # 3. Delete Chart_0 by name; verify Chart_0 is gone and group remains
    del_res = _execute(doc, ctx, "manage_charts", {"action": "delete", "name": "Chart_0"}, domain="draw")
    assert del_res.get("status") == "ok", f"Delete grouped chart failed: {del_res}"
    page_shapes = [page0.getByIndex(i) for i in range(page0.getCount())]
    assert group in page_shapes
    assert group.getCount() == 1

    # 4. Create another chart on slide 0 (will be Chart_0 again since previous was deleted)
    res_s0 = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "chart_type": "bar",
        "title": "Page 0 Bar",
    }, domain="draw")
    assert res_s0.get("status") == "ok"
    assert res_s0.get("name") == "Chart_0"

    # 5. Insert slide 1 and set current page, then create chart on slide 1 (also Chart_0)
    doc.getDrawPages().insertNewByIndex(1)
    page1 = doc.getDrawPages().getByIndex(1)
    controller = doc.getCurrentController()
    if controller is not None and hasattr(controller, "setCurrentPage"):
        controller.setCurrentPage(page1)

    res_s1 = _execute(doc, ctx, "manage_charts", {
        "action": "create",
        "chart_type": "pie",
        "title": "Page 1 Pie",
    }, domain="draw")
    assert res_s1.get("status") == "ok"
    assert res_s1.get("name") == "Chart_0"

    # 6. List charts: should show Chart_0 on both page 0 and page 1
    list_res = _execute(doc, ctx, "manage_charts", {"action": "list"}, domain="draw")
    assert list_res.get("status") == "ok"
    charts = list_res.get("charts", [])
    assert len(charts) == 2
    pages_with_chart_0 = [c["page"] for c in charts if c["name"] == "Chart_0"]
    assert 0 in pages_with_chart_0
    assert 1 in pages_with_chart_0

    # 7. Delete without page should error with CHART_AMBIGUOUS
    del_ambig = _execute(doc, ctx, "manage_charts", {"action": "delete", "name": "Chart_0"}, domain="draw")
    assert del_ambig.get("status") == "error"
    assert del_ambig.get("code") == "CHART_AMBIGUOUS"

    # 8. Delete with page=1 removes only slide 1 chart
    del_p1 = _execute(doc, ctx, "manage_charts", {"action": "delete", "name": "Chart_0", "page": 1}, domain="draw")
    assert del_p1.get("status") == "ok"

    list_after = _execute(doc, ctx, "manage_charts", {"action": "list"}, domain="draw")
    assert list_after.get("status") == "ok"
    remaining = list_after.get("charts", [])
    assert len(remaining) == 1
    assert remaining[0]["page"] == 0
    assert remaining[0]["name"] == "Chart_0"

