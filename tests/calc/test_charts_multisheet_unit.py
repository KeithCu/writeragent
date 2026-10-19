# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
"""Unit tests for Calc multi-sheet chart helpers and non-empty exception formatting (no UNO required)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.charts import (
    ChartLookup,
    ChartRef,
    GetChartInfo,
    ManageCharts,
    UpsertChart,
    _apply_chart_data_arrays,
    _apply_chart_styling,
    _find_calc_chart_and_sheet,
    _format_chart_exception_msg,
    _get_all_calc_chart_names,
    _writer_body_has_no_text,
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

    mc = ManageCharts()
    ok, err = mc.validate(doc_type="calc")
    assert ok is False
    assert err and "action" in err

    ok_bad, err_bad = mc.validate(doc_type="calc", action="invalid_action")
    assert ok_bad is False
    assert "Invalid value" in (err_bad or "")

    ok_type, err_type = mc.validate(doc_type="calc", action="create")
    assert ok_type is False
    assert err_type and "chart_type" in err_type

    ok_range, err_range = mc.validate(doc_type="calc", action="create", chart_type="bar")
    assert ok_range is False
    assert err_range == "Parameter 'data_range' is required when action is 'create' in Calc"

    uc = UpsertChart()
    with patch("plugin.calc.charts.supportsService", return_value=True), patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup()):
        res_edit_nf = uc.execute(ctx, action="edit", name="NonExistentChart")
        assert res_edit_nf["status"] == "error"
        assert "not found" in res_edit_nf["message"]


def test_manage_charts_schema_impress_strips_calc_fields():
    from plugin.calc.charts import ManageCharts

    mc = ManageCharts()
    impress_params = mc.get_parameters("impress")
    assert impress_params is not None
    impress = impress_params["properties"]
    assert "data_range" not in impress
    assert "sheet" not in impress
    assert "has_header" not in impress
    assert "headers" in impress
    calc_params = mc.get_parameters("calc")
    assert calc_params is not None
    calc = calc_params["properties"]
    assert "data_range" in calc
    assert "headers" not in calc
    assert "rows" not in calc


class _UnoRuntimeException(Exception):
    """Stand-in for com.sun.star.uno.RuntimeException (empty str, Message set)."""

    def __init__(self, message: str) -> None:
        super().__init__("")
        self.Message = message


def _empty_uno_struct(*_args, **_kwargs):
    """Same shape as the leaked pytest uno.createUnoStruct stub: no .X."""
    return SimpleNamespace()


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
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts.uno.Enum", return_value="legend-right") as enum,
    ):
        result = tool.execute(ctx, action="create", chart_type="bar", data_range="A1:B6", legend_position="right")
    assert result["status"] == "ok"
    enum.assert_called_once_with("com.sun.star.chart.ChartLegendPosition", "RIGHT")
    charts.removeByName.assert_not_called()


def test_legend_position_enables_legend_when_initially_false():
    from plugin.calc.charts import _CHART_LEGEND_POSITION_ENUM, _apply_chart_styling

    chart_doc = MagicMock()
    chart_doc.HasLegend = False
    with patch("plugin.calc.charts.uno.Enum", return_value="legend-bottom") as enum:
        warnings = _apply_chart_styling(chart_doc, legend_position="bottom")
    assert chart_doc.HasLegend is True
    enum.assert_called_once_with(_CHART_LEGEND_POSITION_ENUM, "BOTTOM")
    assert warnings == []


def test_legend_position_ignored_when_has_legend_is_false():
    from plugin.calc.charts import _apply_chart_styling

    chart_doc = MagicMock()
    chart_doc.HasLegend = False
    with patch("plugin.calc.charts.uno.Enum") as enum:
        warnings = _apply_chart_styling(chart_doc, has_legend=False, legend_position="bottom")
    assert chart_doc.HasLegend is False
    enum.assert_not_called()
    assert any("legend_position" in w and "has_legend" in w for w in warnings)


def test_legend_position_none_with_has_legend_false_stays_false():
    from plugin.calc.charts import _apply_chart_styling

    chart_doc = MagicMock()
    chart_doc.HasLegend = False
    with patch("plugin.calc.charts.uno.Enum") as enum:
        warnings = _apply_chart_styling(chart_doc, legend_position="none")
    assert chart_doc.HasLegend is False
    enum.assert_not_called()
    assert warnings == []


def test_legend_runtime_exception_removes_inserted_calc_chart():
    """A legend RuntimeException produces a warning and keeps the chart."""
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()

    def _raise_runtime(type_name, member):
        raise _UnoRuntimeException(f"unknown type {type_name}.{member}")

    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts.uno.Enum", side_effect=_raise_runtime),
    ):
        result = tool.execute(ctx, action="create", chart_type="column", data_range="A1:B6", legend_position="top")

    assert result["status"] == "ok"
    assert any("legend position" in w.lower() for w in result.get("warnings", []))
    charts.removeByName.assert_not_called()


def test_calc_diagram_failure_after_insert_removes_chart():
    """A property set that fails after insert removes the new chart."""
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    chart_doc.setDiagram.side_effect = _UnoRuntimeException("diagram rejected")
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
    ):
        result = tool.execute(ctx, action="create", chart_type="bar", data_range="A1:B6")
    assert result["status"] == "error"
    added = charts.addNewByName.call_args[0][0]
    charts.removeByName.assert_called_once_with(added)


def test_writer_diagram_failure_after_insert_removes_chart():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    chart_doc = MagicMock()
    chart_doc.createInstance.return_value = MagicMock()
    chart_doc.setDiagram.side_effect = _UnoRuntimeException("diagram rejected")
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts._await_writer_chart_document", return_value=chart_doc),
        patch("plugin.calc.charts._drop_writer_chart_insert") as drop_mock,
        patch("plugin.calc.charts._apply_chart_styling") as styling_mock,
    ):
        res = UpsertChart().execute(ctx, action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

    assert res["status"] == "error"
    assert res.get("code") == "CHART_CREATE_ERROR"
    assert "diagram rejected" in res["message"]
    drop_mock.assert_called_once()
    styling_mock.assert_not_called()


def test_writer_diagram_create_instance_none_removes_chart():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    chart_doc = MagicMock()
    chart_doc.createInstance.return_value = None
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts._await_writer_chart_document", return_value=chart_doc),
        patch("plugin.calc.charts._drop_writer_chart_insert") as drop_mock,
        patch("plugin.calc.charts._apply_chart_styling") as styling_mock,
    ):
        res = UpsertChart().execute(ctx, action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

    assert res["status"] == "error"
    assert res.get("code") == "CHART_CREATE_ERROR"
    drop_mock.assert_called_once()
    styling_mock.assert_not_called()


def test_missing_calc_chart_model_removes_insert():
    tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
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
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(MagicMock(), MagicMock(), "calc_sheet"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=MagicMock()),
        patch("plugin.calc.charts._apply_chart_styling", side_effect=_UnoRuntimeException("legend")),
        patch("plugin.calc.charts._drop_failed_chart_insert", wraps=_drop_failed_chart_insert) as drop,
    ):
        result = UpsertChart().execute(ctx, action="edit", name="Chart_0", legend_position="bottom")
    assert result["status"] == "error"
    assert result.get("code") == "CHART_EDIT_ERROR"
    drop.assert_not_called()


def test_draw_legend_failure_removes_shape():
    # A throwing getTitle() on create returns status ok, a warning, and does not remove the chart
    tool, _ctx, _charts, chart_doc, _bridge = _calc_chart_create_parts()
    ctx = MagicMock()
    page = MagicMock()
    shape = MagicMock()
    ctx.doc.createInstance.return_value = shape
    ctx.doc.getCurrentController.return_value.getCurrentPage.return_value = page
    draw_pages = MagicMock()
    draw_pages.getCount.return_value = 1
    draw_pages.getByIndex.return_value = page
    ctx.doc.getDrawPages.return_value = draw_pages
    rect = MagicMock(Width=12000, Height=8000, X=1000, Y=1000)

    chart_doc.getTitle.side_effect = RuntimeError("title failure")

    with (
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._process_events", return_value=True),
    ):
        res = tool._create_draw_chart(ctx, rect, "com.sun.star.chart.BarDiagram", title="My Title", headers=["Cat", "A"], rows=[["x", 1]])

    assert res["status"] == "ok"
    assert res["page"] == 0
    assert any("title failure" in w for w in res.get("warnings", []))
    page.add.assert_called_once_with(shape)
    page.remove.assert_not_called()


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
    assert err is not None
    assert "headers" in err
    ok, err = mc.validate(doc_type="calc", action="create", chart_type="column")
    assert ok is False
    assert err is not None
    assert "data_range" in err
    ok, err = mc.validate(doc_type="writer", action="create", chart_type="column", headers=["Category", "A"], rows=[["x", 1]])
    assert ok is True


def test_manage_charts_validate_name_required_for_get_info_and_delete():
    from plugin.calc.charts import ManageCharts

    mc = ManageCharts()
    for act in ("get_info", "delete"):
        ok, err = mc.validate(doc_type="calc", action=act)
        assert ok is False
        assert err is not None
        assert "name" in err

        ok, err = mc.validate(doc_type="calc", action=act, name="Chart_0")
        assert (ok, err) == (True, None)

    ok, err = mc.validate(doc_type="calc", action="list")
    assert (ok, err) == (True, None)


def test_find_calc_chart_honors_sheet_name():
    doc = MagicMock()
    sheet_summary = MagicMock()
    sheet_summary.getName.return_value = "Summary"
    sheet_summary.getCharts.return_value.hasByName.return_value = True

    sheet_data = MagicMock()
    sheet_data.getName.return_value = "Data"
    sheet_data.getCharts.return_value.hasByName.return_value = True

    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda n: n in ("Summary", "Data")
    sheets.getByName.side_effect = lambda n: sheet_summary if n == "Summary" else sheet_data
    doc.getSheets.return_value = sheets

    chart_obj, sheet = _find_calc_chart_and_sheet(doc, "Chart_0", "Data")
    assert sheet is sheet_data


class DisposedException(Exception):
    pass


class RuntimeException(Exception):
    pass


def test_calc_chart_create_disposal_is_not_chart_create_error():
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    bridge.get_sheet.side_effect = DisposedException("gone")
    with patch("plugin.calc.charts.CalcBridge", return_value=bridge):
        with pytest.raises(DisposedException):
            tool.execute(ctx, action="create", chart_type="bar", data_range="A1:B6", sheet="Missing")
        charts.addNewByName.assert_not_called()

        res = ManageCharts().execute_safe(ctx, action="create", chart_type="bar", data_range="A1:B6", sheet="Missing")
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


def test_calc_chart_create_missing_sheet_stays_not_found():
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    bridge.get_sheet.side_effect = RuntimeException("missing")
    with patch("plugin.calc.charts.CalcBridge", return_value=bridge):
        res = tool.execute(ctx, action="create", chart_type="bar", data_range="A1:B6", sheet="Missing")
        assert res["status"] == "error"
        assert "not found" in res["message"]


def test_calc_diagram_disposal_propagates_after_removing_chart():
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    chart_doc.setDiagram.side_effect = DisposedException("gone")
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
    ):
        with pytest.raises(DisposedException):
            tool.execute(ctx, action="create", chart_type="bar", data_range="A1:B6")
        added = charts.addNewByName.call_args[0][0]
        charts.removeByName.assert_called_once_with(added)

        res = ManageCharts().execute_safe(ctx, action="create", chart_type="bar", data_range="A1:B6")
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


def test_get_chart_info_ranges_disposal_propagates():
    chart_obj = MagicMock()
    chart_obj.getRanges.side_effect = DisposedException("gone")
    ctx: Any = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, MagicMock(), "calc_sheet"))):
        with pytest.raises(DisposedException):
            GetChartInfo().execute(ctx, name="Chart_1")

    chart_obj.getRanges.side_effect = RuntimeException("ranges gone")
    with (
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, MagicMock(), "calc_sheet"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
    ):
        res = GetChartInfo().execute(ctx, name="Chart_1")
        assert res["status"] == "ok"
        assert res["data_ranges"] == []


def test_upsert_edit_disposal_is_not_chart_edit_error():
    chart_obj = MagicMock()
    chart_doc = MagicMock()
    chart_doc.setDiagram.side_effect = DisposedException("gone")
    ctx: Any = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, MagicMock(), "calc_sheet"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
    ):
        with pytest.raises(DisposedException):
            UpsertChart().execute(ctx, action="edit", name="Chart_1", chart_type="bar")

    chart_doc.setDiagram.side_effect = RuntimeException("edit error")
    with (
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, MagicMock(), "calc_sheet"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
    ):
        res = UpsertChart().execute(ctx, action="edit", name="Chart_1", chart_type="bar")
        assert res["status"] == "error"
        assert res.get("code") == "CHART_EDIT_ERROR"


def test_writer_chart_insert_failure_reports_chart_create_error():
    """Every Writer insert fallback failing reaches the create arm (CHART_CREATE_ERROR)."""
    from plugin.calc.charts import UpsertChart

    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value = []
    text = doc.getText.return_value
    text.getString.return_value = "body"
    text.insertTextContent.side_effect = RuntimeError("insert rejected")
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())
    with patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct):
        res = UpsertChart().execute(ctx, action="create", chart_type="bar", headers=["a", "b"], rows=[["x", 1]])
    assert res["status"] == "error"
    assert res.get("code") == "CHART_CREATE_ERROR"
    assert "insert rejected" in res["message"]
    assert text.insertTextContent.call_count == 2


def test_data_arrays_validation_rejects_bad_shapes():
    # Headers must have at least 2 entries; rows must match header length
    chart_doc = MagicMock()
    chart_data = MagicMock()
    chart_doc.getData.return_value = chart_data

    # Non-numeric cell in data column
    with pytest.raises(ValueError) as exc:
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["A", "not_a_num"]])
    assert "must be a number" in str(exc.value)
    chart_data.setData.assert_not_called()

    # Short row
    with pytest.raises(ValueError) as exc:
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["A"]])
    assert "does not match" in str(exc.value)
    chart_data.setData.assert_not_called()

    # Long row
    with pytest.raises(ValueError) as exc:
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["A", 1, 2]])
    assert "does not match" in str(exc.value)
    chart_data.setData.assert_not_called()

    # Boolean rejected
    with pytest.raises(ValueError) as exc:
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["A", True]])
    assert "cannot be boolean" in str(exc.value)


def test_data_apply_disposal_raises():
    # Disposal during data application must propagate
    chart_doc = MagicMock()
    chart_data = MagicMock()
    chart_data.setData.side_effect = DisposedException("data gone")
    chart_doc.getData.return_value = chart_data

    with pytest.raises(DisposedException):
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["A", 10.0]])


def test_create_data_apply_failure_removes_chart_and_errors():
    # Writer chart create failure in data apply removes the inserted chart
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    text = doc.getText.return_value
    text.getString.return_value = "hello"

    chart_doc = MagicMock()
    chart_data = MagicMock()
    chart_data.setData.side_effect = RuntimeException("setData failed")
    chart_doc.getData.return_value = chart_data
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts._await_writer_chart_document", return_value=chart_doc),
        patch("plugin.calc.charts._drop_writer_chart_insert") as drop_mock,
    ):
        res = UpsertChart().execute(ctx, action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])
    assert res["status"] == "error"
    assert res.get("code") == "CHART_CREATE_ERROR"
    assert "setData failed" in res["message"]
    drop_mock.assert_called_once()


def test_writer_create_two_paragraphs_skips_control_char():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    text = doc.getText.return_value
    p1 = MagicMock()
    p2 = MagicMock()
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, True]
    enum.nextElement.side_effect = [p1, p2]
    text.createEnumeration.return_value = enum

    chart_doc = MagicMock()
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts._await_writer_chart_document", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts._process_events"),
    ):
        res = UpsertChart().execute(ctx, action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

    assert res["status"] == "ok"
    text.getString.assert_not_called()
    text.insertControlCharacter.assert_not_called()
    assert text.insertTextContent.call_count >= 1


def test_writer_create_non_empty_paragraph_skips_control_char():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    text = doc.getText.return_value
    p1 = MagicMock()
    p1.getString.return_value = "hello"
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = p1
    text.createEnumeration.return_value = enum

    chart_doc = MagicMock()
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts._await_writer_chart_document", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts._process_events"),
    ):
        res = UpsertChart().execute(ctx, action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

    assert res["status"] == "ok"
    text.getString.assert_not_called()
    text.insertControlCharacter.assert_not_called()


def test_writer_create_single_empty_paragraph_inserts_break_and_content():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    text = doc.getText.return_value
    p1 = MagicMock()
    p1.getString.return_value = ""
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = p1
    text.createEnumeration.return_value = enum

    chart_doc = MagicMock()
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts.uno.getConstantByName", return_value=123),
        patch("plugin.calc.charts._await_writer_chart_document", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts._process_events"),
    ):
        res = UpsertChart().execute(ctx, action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

    assert res["status"] == "ok"
    text.getString.assert_not_called()
    text.insertControlCharacter.assert_called_once()
    assert text.insertTextContent.call_count >= 1


def test_writer_create_enumeration_disposed_propagates():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    text = doc.getText.return_value
    text.createEnumeration.side_effect = DisposedException("teardown")
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        pytest.raises(DisposedException),
    ):
        UpsertChart().execute(ctx, action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

    text.insertControlCharacter.assert_not_called()
    text.insertTextContent.assert_not_called()


def test_writer_default_mock_enumeration_skips_break():
    text = MagicMock()
    assert _writer_body_has_no_text(text) is False
    text.getString.assert_not_called()


def test_edit_data_apply_failure_returns_error_and_does_not_remove_chart():
    # Chart edit data failure returns error without removing the chart
    chart_obj = MagicMock()
    chart_doc = MagicMock()
    chart_data = MagicMock()
    chart_data.setData.side_effect = RuntimeException("setData failed")
    chart_doc.getData.return_value = chart_data

    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    ctx: Any = SimpleNamespace(doc=doc)
    with (
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, MagicMock(), "writer_embed"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._drop_failed_chart_insert") as drop_mock,
    ):
        res = UpsertChart().execute(ctx, action="edit", name="Chart_1", headers=["Cat", "A"], rows=[["x", 1]])
    assert res["status"] == "error"
    assert res.get("code") == "CHART_EDIT_ERROR"
    assert "setData failed" in res["message"]
    drop_mock.assert_not_called()


def test_styling_background_failure_returns_warnings_not_removed():
    # Background color failure produces warnings on status ok and keeps chart
    chart_doc = MagicMock()
    bg = MagicMock()
    bg.setPropertyValue.side_effect = RuntimeException("bg failed")
    chart_doc.getPageBackground.return_value = bg

    warnings = _apply_chart_styling(chart_doc, bg_color="#FF0000")
    assert any("background" in w.lower() for w in warnings)

    # Invalid color string is also a warning
    warnings_invalid = _apply_chart_styling(chart_doc, bg_color="not-a-color-at-all-xyz")
    assert any("invalid bg_color" in w.lower() for w in warnings_invalid)


def test_styling_logging_counts_not_payloads(caplog):
    # Logs record counts, not cell contents, at INFO
    import logging
    chart_doc = MagicMock()
    chart_data = MagicMock()
    chart_doc.getData.return_value = chart_data

    with caplog.at_level(logging.INFO, logger="writeragent.calc"):
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["SecretDistinctCellXYZ", 999.0]])
    assert "SecretDistinctCellXYZ" not in caplog.text

    # At DEBUG, first row may be inspected
    with caplog.at_level(logging.DEBUG, logger="writeragent.calc"):
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["SecretDistinctCellXYZ", 999.0]])
    assert "SecretDistinctCellXYZ" in caplog.text


def test_position_contracts_and_schema():
    # Draw/Impress omit position; Writer accepts 'cursor' and 'end'; Calc accepts cell address
    mc = ManageCharts()
    draw_params = mc.get_parameters("draw")
    assert draw_params is not None
    draw_props = draw_params["properties"]
    assert "position" not in draw_props
    impress_params = mc.get_parameters("impress")
    assert impress_params is not None
    impress_props = impress_params["properties"]
    assert "position" not in impress_props

    writer_params = mc.get_parameters("writer")
    assert writer_params is not None
    writer_props = writer_params["properties"]
    assert "position" in writer_props

    # Writer invalid position
    ok, err = mc.validate(doc_type="writer", action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]], position="page-2")
    assert ok is False
    assert err is not None
    assert "cursor" in err and "end" in err

    # Writer valid position
    ok, _ = mc.validate(doc_type="writer", action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]], position="cursor")
    assert ok is True

    # Writer omitted position
    ok, _ = mc.validate(doc_type="writer", action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])
    assert ok is True

    # Calc position
    ok, _ = mc.validate(doc_type="calc", action="create", chart_type="bar", data_range="A1:B2", position="C1")
    assert ok is True


def test_schema_symmetry_across_apps():
    # ManageCharts and UpsertChart expose matching properties for each doc_type
    mc = ManageCharts()
    uc = UpsertChart()
    for dt in ("calc", "writer", "draw", "impress"):
        mc_params = mc.get_parameters(dt)
        uc_params = uc.get_parameters(dt)
        assert mc_params is not None and uc_params is not None
        mc_props = set(mc_params["properties"].keys())
        uc_props = set(uc_params["properties"].keys())
        assert mc_props == uc_props

    # Calc missing data_range
    ok, err = mc.validate(doc_type="calc", action="create", chart_type="bar")
    assert ok is False
    assert err == "Parameter 'data_range' is required when action is 'create' in Calc"

    # Writer missing headers/rows
    ok, err = mc.validate(doc_type="writer", action="create", chart_type="bar")
    assert ok is False
    assert err == "Both 'headers' and 'rows' are required to create a chart in Writer or Draw/Impress"

    # Non-numeric row validation error
    ok, err = mc.validate(doc_type="writer", action="create", chart_type="bar", headers=["Cat", "A"], rows=[["x", "bad"]])
    assert ok is False
    assert err is not None
    assert "must be a number" in err


def test_calc_edit_updates_data_range_and_headers():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    chart_obj = MagicMock()
    chart_doc = MagicMock()

    cell_range = MagicMock()
    addr = MagicMock()
    cell_range.getRangeAddress.return_value = addr

    bridge = MagicMock()
    bridge.get_cell_range.return_value = cell_range

    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, sheet, "calc_sheet"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
    ):
        res = UpsertChart().execute(
            ctx,
            action="edit",
            name="Chart_0",
            data_range="A1:C10",
            has_header=False,
        )

    assert res["status"] == "ok"
    chart_obj.setRanges.assert_called_once_with((addr,))
    chart_obj.setHasColumnHeaders.assert_called_once_with(False)
    chart_obj.setHasRowHeaders.assert_called_once_with(False)


def test_calc_edit_range_error_does_not_call_set_diagram():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    chart_obj = MagicMock()
    chart_doc = MagicMock()

    bridge = MagicMock()
    bridge.get_cell_range.side_effect = ValueError("invalid range")

    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, sheet, "calc_sheet"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
    ):
        res = UpsertChart().execute(
            ctx,
            action="edit",
            name="Chart_0",
            chart_type="pie",
            data_range="Invalid:Range",
        )

    assert res["status"] == "error"
    assert res.get("code") == "CHART_EDIT_ERROR"
    chart_doc.setDiagram.assert_not_called()


def test_calc_create_bad_position_does_not_call_add_new_by_name():
    tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    rect = MagicMock(Width=12000, Height=8000, X=1000, Y=1000)

    with patch("plugin.calc.charts.CalcBridge", return_value=bridge):
        # 1. Mismatched sheet prefix
        res1 = tool._create_calc_chart(ctx, rect, "com.sun.star.chart.BarDiagram", data_range="A1:B2", position="OtherSheet.A1")
        assert res1["status"] == "error"
        assert "Position names sheet" in res1["message"]
        charts.addNewByName.assert_not_called()

        # 2. Invalid position address (range or unparseable)
        res2 = tool._create_calc_chart(ctx, rect, "com.sun.star.chart.BarDiagram", data_range="A1:B2", position="A1:B2")
        assert res2["status"] == "error"
        assert "Invalid position" in res2["message"]
        charts.addNewByName.assert_not_called()


def test_calc_create_valid_position_sets_rect_and_creates():
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    sheet = bridge.get_active_sheet()
    cell_obj = MagicMock()
    cell_obj.getPosition.return_value = MagicMock(X=2500, Y=3500)
    cell_obj.getSize.return_value = MagicMock(Width=5000, Height=2000)
    sheet.getCellRangeByName.return_value = cell_obj

    rect = MagicMock(Width=12000, Height=8000, X=1000, Y=1000)

    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
    ):
        res = tool._create_calc_chart(ctx, rect, "com.sun.star.chart.BarDiagram", data_range="A1:B2", position="B5")

    assert res["status"] == "ok"
    assert rect.X == 2500
    assert rect.Y == 3500
    assert rect.Width == 5000
    assert rect.Height == 2000
    charts.addNewByName.assert_called_once()


def test_calc_edit_position_warns_and_does_not_move():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    sheet = MagicMock()
    chart_obj = MagicMock()
    chart_doc = MagicMock()
    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, sheet, "calc_sheet"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
    ):
        res = UpsertChart().execute(
            ctx,
            action="edit",
            name="Chart_0",
            position="D5",
        )

    assert res["status"] == "ok"
    assert any("placement is unchanged" in w.lower() for w in res.get("warnings", []))
    sheet.getCellRangeByName.assert_not_called()


def test_manage_charts_execute_unknown_action_returns_error():
    mc = ManageCharts()
    ctx: Any = SimpleNamespace(doc=MagicMock())
    res = mc.execute(ctx, action="nope")
    assert res["status"] == "error"
    assert "Unsupported action" in res["message"]


def test_unsupported_dim3d_yields_warning():
    chart_doc = MagicMock()
    diagram = MagicMock(spec=["Stacked"])  # no Dim3D attribute
    chart_doc.getDiagram.return_value = diagram
    warnings = _apply_chart_styling(chart_doc, is_3d=True)
    assert any("Dim3D" in w for w in warnings)


def test_data_applied_before_series_colors():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    chart_obj = MagicMock()
    chart_doc = MagicMock()
    chart_data = MagicMock()
    chart_doc.getData.return_value = chart_data

    call_order = []
    chart_data.setData.side_effect = lambda data: call_order.append("setData")

    series = MagicMock()
    series.setPropertyValue.side_effect = lambda prop, val: call_order.append(f"setPropertyValue:{prop}")

    coord = MagicMock()
    ctype = MagicMock()
    ctype.getDataSeries.return_value = [series]
    coord.getChartTypes.return_value = [ctype]
    first_diag = MagicMock()
    first_diag.getCoordinateSystems.return_value = [coord]
    chart_doc.getFirstDiagram.return_value = first_diag

    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch("plugin.calc.charts._lookup_chart", return_value=ChartLookup(ref=ChartRef(chart_obj, MagicMock(), "writer_draw_page"))),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
    ):
        res = UpsertChart().execute(
            ctx,
            action="edit",
            name="Chart_0",
            headers=["Cat", "Val"],
            rows=[["A", 10.0]],
            colors=["#FF0000"],
        )

    assert res["status"] == "ok"
    assert "setData" in call_order
    assert any("setPropertyValue" in c for c in call_order)
    assert call_order.index("setData") < next(i for i, c in enumerate(call_order) if "setPropertyValue" in c)


def test_validate_headers_and_rows_nan_inf_and_cell_cap():
    from plugin.calc.charts import _validate_headers_and_rows

    # Reject NaN and Inf
    ok, err = _validate_headers_and_rows(["Cat", "Val"], [["A", float("nan")]])
    assert not ok and "finite number" in (err or "")
    ok, err = _validate_headers_and_rows(["Cat", "Val"], [["A", "nan"]])
    assert not ok and "finite number" in (err or "")
    ok, err = _validate_headers_and_rows(["Cat", "Val"], [["A", float("inf")]])
    assert not ok and "finite number" in (err or "")
    ok, err = _validate_headers_and_rows(["Cat", "Val"], [["A", "-inf"]])
    assert not ok and "finite number" in (err or "")

    # Reject cell count above 10,000
    headers = ["Cat"] + [f"C{i}" for i in range(100)]  # 101 columns
    rows = [["R"] + [1.0] * 100 for _ in range(100)]  # 100 rows * 101 cols = 10100 > 10000
    ok, err = _validate_headers_and_rows(headers, rows)
    assert not ok and "exceeds limit" in (err or "")


