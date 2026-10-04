# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
"""Unit tests for Calc multi-sheet chart helpers and non-empty exception formatting (no UNO required)."""

from unittest.mock import MagicMock, patch
from plugin.calc.charts import (
    _format_chart_exception_msg,
    _get_all_calc_chart_names,
    _find_calc_chart_and_sheet,
)


def test_format_chart_exception_msg():
    # 1. Standard python exception with text
    e1 = ValueError("invalid value")
    assert _format_chart_exception_msg(e1) == "ValueError: invalid value"

    # 2. Exception with empty str() but Message attribute (UNO Exception style)
    class DummyUnoException(Exception):
        def __init__(self, message):
            self.Message = message
        def __str__(self):
            return ""

    e2 = DummyUnoException("ElementExistException: Name exists")
    assert _format_chart_exception_msg(e2) == "DummyUnoException: ElementExistException: Name exists"

    # 3. Exception with completely empty message
    class BlankException(Exception):
        def __str__(self):
            return ""

    e3 = BlankException()
    assert _format_chart_exception_msg(e3) == "BlankException"


def test_is_chart_name_used_and_count():
    doc = MagicMock()

    sheet1 = MagicMock()
    sheet1.getName.return_value = "Sheet1"
    charts1 = MagicMock()
    charts1.hasByName.side_effect = lambda name: name == "Chart_0"
    charts1.getElementNames.return_value = ["Chart_0"]
    sheet1.getCharts.return_value = charts1

    sheet2 = MagicMock()
    sheet2.getName.return_value = "Sheet2"
    charts2 = MagicMock()
    charts2.hasByName.side_effect = lambda name: name == "Chart_1"
    charts2.getElementNames.return_value = ["Chart_1"]
    sheet2.getCharts.return_value = charts2

    sheets = MagicMock()
    sheets.getElementNames.return_value = ["Sheet1", "Dashboard"]
    sheets.getByName.side_effect = lambda name: sheet1 if name == "Sheet1" else sheet2
    doc.getSheets.return_value = sheets

    # Chart_0 is on Sheet1, Chart_1 is on Sheet2
    all_names = _get_all_calc_chart_names(doc)
    assert "Chart_0" in all_names
    assert "Chart_1" in all_names
    assert "Chart_2" not in all_names

    # Find chart and sheet
    chart_obj, found_sheet = _find_calc_chart_and_sheet(doc, "Chart_1")
    assert found_sheet is sheet2
    assert chart_obj is not None

    chart_obj_none, sheet_none = _find_calc_chart_and_sheet(doc, "Chart_999")
    assert chart_obj_none is None
    assert sheet_none is None


def test_create_calc_chart_has_header_false():
    from plugin.calc.charts import UpsertChart

    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    charts = MagicMock()
    charts.getElementNames.return_value = []
    sheet.getCharts.return_value = charts

    cell_range = MagicMock()
    addr = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=5)
    cell_range.getRangeAddress.return_value = addr

    bridge = MagicMock()
    bridge.get_active_sheet.return_value = sheet
    bridge.get_cell_range.return_value = cell_range

    tool = UpsertChart()
    rect = MagicMock()
    service = "com.sun.star.chart.BarDiagram"

    with MagicMock() as mock_bridge_cls:
        mock_bridge_cls.return_value = bridge
        from unittest.mock import patch
        with patch("plugin.calc.charts.CalcBridge", return_value=bridge), patch("plugin.calc.charts._chart_document_from_host", return_value=MagicMock()):
            tool._create_calc_chart(ctx, rect, service, data_range="A1:B6", has_header=False)

    charts.addNewByName.assert_called_once()
    # Check that False was passed for HasCategories and HasSingleCellHeader
    args = charts.addNewByName.call_args[0]
    assert args[3] is False
    assert args[4] is False


def test_manage_charts_error_handling():
    from plugin.calc.charts import ManageCharts, UpsertChart

    ctx = MagicMock()
    ctx.doc = MagicMock()

    # 1. ManageCharts missing action
    mc = ManageCharts()
    res_no_action = mc.execute(ctx)
    assert res_no_action["status"] == "error"
    assert res_no_action["code"] == "MISSING_PARAMETER"

    # 2. ManageCharts unsupported action
    res_bad_action = mc.execute(ctx, action="invalid_action")
    assert res_bad_action["status"] == "error"

    # 3. UpsertChart edit not found
    uc = UpsertChart()
    with patch("plugin.calc.charts.supportsService", return_value=True), patch("plugin.calc.charts._resolve_chart", return_value=None):
        res_edit_nf = uc.execute(ctx, action="edit", name="NonExistentChart")
        assert res_edit_nf["status"] == "error"
        assert "not found" in res_edit_nf["message"]


def test_manage_charts_schema_impress_strips_calc_fields():
    from plugin.calc.charts import ManageCharts

    mc = ManageCharts()
    impress = mc.get_parameters("impress")["properties"]
    assert "data_range" not in impress
    assert "sheet" not in impress
    assert "has_header" not in impress
    assert "headers" in impress
    calc = mc.get_parameters("calc")["properties"]
    assert "data_range" in calc
    assert "headers" not in calc
    assert "rows" not in calc


class _UnoRuntimeException(Exception):
    """Stand-in for com.sun.star.uno.RuntimeException (empty str, Message set)."""

    def __init__(self, message: str) -> None:
        super().__init__("")
        self.Message = message


def _calc_chart_create_parts():
    from plugin.calc.charts import UpsertChart

    ctx = MagicMock()
    ctx.doc = MagicMock()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    charts = MagicMock()
    sheet.getCharts.return_value = charts
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=5)
    bridge = MagicMock()
    bridge.get_active_sheet.return_value = sheet
    bridge.get_cell_range.return_value = cell_range
    chart_doc = MagicMock()
    chart_doc.HasLegend = True
    return UpsertChart(), ctx, charts, chart_doc, bridge


def test_legend_position_uses_chart_legend_position_enum():
    """Alignment is ChartLegendPosition, and a successful create keeps the chart."""
    from plugin.calc.charts import _CHART_LEGEND_POSITION_ENUM, _apply_chart_styling

    chart_doc = MagicMock()
    chart_doc.HasLegend = True
    with patch("plugin.calc.charts.uno.Enum", return_value="legend-bottom") as enum:
        _apply_chart_styling(chart_doc, legend_position="bottom")
    enum.assert_called_once_with(_CHART_LEGEND_POSITION_ENUM, "BOTTOM")
    assert _CHART_LEGEND_POSITION_ENUM == "com.sun.star.chart.ChartLegendPosition"
    assert chart_doc.getLegend.return_value.Alignment == "legend-bottom"

    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    chart_doc.HasLegend = True
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
        patch("plugin.calc.charts.uno.Enum", return_value="legend-right") as enum,
    ):
        result = tool.execute(ctx, action="create", chart_type="bar", data_range="A1:B6", legend_position="right")
    assert result["status"] == "ok"
    enum.assert_called_once_with("com.sun.star.chart.ChartLegendPosition", "RIGHT")
    charts.removeByName.assert_not_called()


def test_legend_runtime_exception_removes_inserted_calc_chart():
    """A legend RuntimeException after addNewByName must not leave the chart behind."""
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()

    def _raise_runtime(type_name, member):
        raise _UnoRuntimeException(f"unknown type {type_name}.{member}")

    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
        patch("plugin.calc.charts.uno.Enum", side_effect=_raise_runtime),
    ):
        result = tool.execute(ctx, action="create", chart_type="column", data_range="A1:B6", legend_position="top")

    assert result["status"] == "error"
    assert result.get("code") == "CHART_CREATE_ERROR"
    added = charts.addNewByName.call_args[0][0]
    charts.removeByName.assert_called_once_with(added)
    assert "RuntimeException" in result["message"] or "unknown type" in result["message"]


def test_calc_diagram_failure_after_insert_removes_chart():
    """A property set that fails after insert removes the new chart."""
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    chart_doc.setDiagram.side_effect = _UnoRuntimeException("diagram rejected")
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
    ):
        result = tool.execute(ctx, action="create", chart_type="bar", data_range="A1:B6")
    assert result["status"] == "error"
    added = charts.addNewByName.call_args[0][0]
    charts.removeByName.assert_called_once_with(added)


def test_missing_calc_chart_model_removes_insert():
    tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
    ):
        result = tool.execute(ctx, action="create", chart_type="line", data_range="A1:B6")
    assert result["status"] == "error"
    assert "Cannot access chart content" in result["message"]
    added = charts.addNewByName.call_args[0][0]
    charts.removeByName.assert_called_once_with(added)


def test_edit_legend_failure_does_not_remove_existing_chart():
    from plugin.calc.charts import UpsertChart, _drop_failed_chart_insert

    ctx = MagicMock()
    ctx.doc = MagicMock()
    with (
        patch("plugin.calc.charts._resolve_chart", return_value=MagicMock()),
        patch("plugin.calc.charts._chart_document_from_host", return_value=MagicMock()),
        patch("plugin.calc.charts._apply_chart_styling", side_effect=_UnoRuntimeException("legend")),
        patch("plugin.calc.charts._drop_failed_chart_insert", wraps=_drop_failed_chart_insert) as drop,
    ):
        result = UpsertChart().execute(ctx, action="edit", name="Chart_0", legend_position="bottom")
    assert result["status"] == "error"
    assert result.get("code") == "CHART_EDIT_ERROR"
    drop.assert_not_called()


def test_draw_legend_failure_removes_shape():
    tool, _ctx, _charts, chart_doc, _bridge = _calc_chart_create_parts()
    ctx = MagicMock()
    page = MagicMock()
    shape = MagicMock()
    ctx.doc.createInstance.return_value = shape
    ctx.doc.getCurrentController.return_value.getCurrentPage.return_value = page
    rect = MagicMock(Width=12000, Height=8000, X=1000, Y=1000)

    def _raise_runtime(type_name, member):
        raise _UnoRuntimeException(f"unknown type {type_name}.{member}")

    with (
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts.uno.Enum", side_effect=_raise_runtime),
        patch("plugin.calc.charts._process_events", return_value=True),
    ):
        try:
            tool._create_draw_chart(ctx, rect, "com.sun.star.chart.BarDiagram", legend_position="left")
        except _UnoRuntimeException:
            pass
        else:
            raise AssertionError("legend failure should propagate after the shape is removed")
    page.add.assert_called_once_with(shape)
    page.remove.assert_called_once_with(shape)


def test_writer_drop_falls_back_to_embedded_objects():
    from plugin.calc.charts import _drop_writer_chart_insert

    doc = MagicMock()
    text = MagicMock()
    chart_obj = MagicMock()
    text.removeTextContent.side_effect = RuntimeError("not in this text")
    embeds = doc.getEmbeddedObjects.return_value
    embeds.hasByName.return_value = True
    _drop_writer_chart_insert(doc, text, chart_obj, "Chart_2")
    embeds.removeByName.assert_called_once_with("Chart_2")

    text.removeTextContent.side_effect = None
    _drop_writer_chart_insert(doc, text, chart_obj, "Chart_2")
    text.removeTextContent.assert_called_with(chart_obj)
    assert embeds.removeByName.call_count == 1


def test_legend_position_none_hides_legend_without_enum():
    from plugin.calc.charts import _apply_chart_styling

    chart_doc = MagicMock()
    chart_doc.HasLegend = True
    with patch("plugin.calc.charts.uno.Enum") as enum:
        _apply_chart_styling(chart_doc, legend_position="none")
    assert chart_doc.HasLegend is False
    enum.assert_not_called()


def test_manage_charts_validate_create_requires_data_or_arrays():
    from plugin.calc.charts import ManageCharts

    mc = ManageCharts()
    ok, err = mc.validate(doc_type="writer", action="create", chart_type="column")
    assert ok is False
    assert "headers" in err
    ok, err = mc.validate(doc_type="calc", action="create", chart_type="column")
    assert ok is False
    assert "data_range" in err
    ok, err = mc.validate(doc_type="writer", action="create", chart_type="column", headers=["A"], rows=[["x", 1]])
    assert ok is True


