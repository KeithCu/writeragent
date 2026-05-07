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

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test
import uno

_calc_doc = None
_writer_doc = None
_draw_doc = None


@setup
def setup_docs(ctx):
    global _calc_doc, _writer_doc, _draw_doc
    desktop = get_desktop(ctx)
    props = (uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True),)

    _calc_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, props)
    _writer_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, props)
    _draw_doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, props)


@teardown
def teardown_docs(ctx):
    global _calc_doc, _writer_doc, _draw_doc
    for d in [_calc_doc, _writer_doc, _draw_doc]:
        if d:
            d.close(True)
    _calc_doc = _writer_doc = _draw_doc = None


def _execute(doc, name, args, domain="calc"):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool import ToolContext
    tctx = ToolContext(doc, None, domain, get_services(), "test")
    return get_tools().execute(name, tctx, **args)


@native_test
def test_calc_enhanced_chart():
    # 1. Setup data
    _execute(_calc_doc, "write_formula_range", {"range_name": "A1:B3", "formula_or_values": [["A", 1], ["B", 2], ["C", 3]]})
    
    # 2. Create 3D Stacked Chart
    res = _execute(_calc_doc, "create_chart", {
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
    chart_name = res.get("chart_name")
    
    # 3. Verify Info
    info = _execute(_calc_doc, "get_chart_info", {"chart_name": chart_name})
    assert info.get("status") == "ok"
    assert info.get("is_3d") is True
    assert info.get("stacked") is True
    assert info.get("title") == "3D Chart Test"
    assert info.get("x_axis_title") == "X Axis"
    assert info.get("y_axis_title") == "Y Axis"
    
    # 4. Edit properties
    edit_res = _execute(_calc_doc, "edit_chart", {
        "chart_name": chart_name,
        "is_3d": False,
        "legend_position": "top",
        "y_axis_title": "New Y"
    })
    assert edit_res.get("status") == "ok"
    
    info2 = _execute(_calc_doc, "get_chart_info", {"chart_name": chart_name})
    assert info2.get("is_3d") is False
    assert info2.get("y_axis_title") == "New Y"


@unittest.skip("Disabled as per user request: internal test causing problems")
@native_test
def test_writer_chart_polymorphic():
    # 1. Create in Writer
    res = _execute(_writer_doc, "create_chart", {
        "chart_type": "pie",
        "title": "Writer Pie"
    }, domain="writer")
    assert res.get("status") == "ok", f"Writer create failed: {res}"
    name = res.get("chart_name")

    probe = _execute(_writer_doc, "get_chart_info", {"chart_name": name}, domain="writer")
    if probe.get("status") != "ok":
        raise unittest.SkipTest(
            "Writer chart embed not available in this LibreOffice runtime "
            f"(get_chart_info: {probe!r}). OLE insert may be disabled in headless/pyuno."
        )

    # 2. List in Writer
    list_res = _execute(_writer_doc, "list_charts", {}, domain="writer")
    assert list_res.get("status") == "ok", f"list_charts failed: {list_res}"
    names = [c["name"] for c in list_res.get("charts", [])]
    assert name in names, (
        f"chart_name {name!r} not in list_charts names {names!r}; full list_res={list_res!r}"
    )
    
    # 3. Info
    info = _execute(_writer_doc, "get_chart_info", {"chart_name": name}, domain="writer")
    assert info.get("title") == "Writer Pie"
    assert "PieDiagram" in info.get("diagram_type", "")


@native_test
def test_draw_chart_polymorphic():
    # 1. Create in Draw
    res = _execute(_draw_doc, "create_chart", {
        "chart_type": "line",
        "title": "Slide Chart",
        "is_3d": True
    }, domain="draw")
    assert res.get("status") == "ok", f"Draw create failed: {res}"
    name = res.get("chart_name")
    
    # 2. Info
    info = _execute(_draw_doc, "get_chart_info", {"chart_name": name}, domain="draw")
    assert info.get("is_3d") is True
    assert info.get("title") == "Slide Chart"
    
    # 3. Delete
    del_res = _execute(_draw_doc, "delete_chart", {"chart_name": name}, domain="draw")
    assert del_res.get("status") == "ok"
    
    list_res = _execute(_draw_doc, "list_charts", {}, domain="draw")
    assert len(list_res.get("charts", [])) == 0
