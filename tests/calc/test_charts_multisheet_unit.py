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

from plugin.app import get_tools
from plugin.framework.errors import name_miss_details

from plugin.calc.charts import (
    ChartRef,
    DrawPage,
    SheetCharts,
    WriterEmbeds,
    _apply_chart_data_arrays,
    _apply_chart_styling,
    _find_calc_chart_and_sheet,
    _format_chart_exception_msg,
    _get_all_calc_chart_names,
    _writer_body_has_no_text,
)
from plugin.framework.errors import ToolExecutionError, UnoObjectError


def _tool(name: str):
    return get_tools().get(name)


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

    # 4. Blank exception raised inside another exception shows the cause's text
    try:
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise BlankException() from inner
    except BlankException as chained:
        assert _format_chart_exception_msg(chained) == "BlankException: 'inner'"


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
    bridge.sheet_or_active.return_value = sheet
    bridge.resolve_range_or_address.return_value = cell_range

    rect = MagicMock()
    service = "com.sun.star.chart.BarDiagram"

    with MagicMock() as mock_bridge_cls:
        mock_bridge_cls.return_value = bridge
        from unittest.mock import patch
        with patch("plugin.calc.charts.CalcBridge", return_value=bridge), patch("plugin.calc.charts._chart_document_from_host", return_value=MagicMock()):
            SheetCharts(ctx.doc).create(ctx, rect, service, data_range="A1:B6", has_header=False)

    charts.addNewByName.assert_called_once()
    # Check that False was passed for HasCategories and HasSingleCellHeader
    args = charts.addNewByName.call_args[0]
    assert args[3] is False
    assert args[4] is False


@pytest.mark.parametrize(
    ("kwargs", "expected_ok", "expected_substrings"),
    [
        pytest.param({"doc_type": "calc", "name": "Chart_0"}, True, (), id="calc-edit-by-name"),
        pytest.param({"doc_type": "calc"}, False, ("chart_type",), id="calc-missing-chart-type"),
        pytest.param({"doc_type": "calc", "chart_type": "bar"}, False, ("data_range",), id="calc-missing-data-range"),
        pytest.param({"doc_type": "calc", "action": "invalid_action"}, False, ("Unknown parameter",), id="calc-unknown-param"),
        pytest.param({"doc_type": "writer", "chart_type": "column"}, False, ("headers", "rows"), id="writer-missing-headers-rows"),
        pytest.param({"doc_type": "writer", "chart_type": "column", "headers": ["Category", "A"], "rows": [["x", 1]]}, True, (), id="writer-valid-arrays"),
        pytest.param({"doc_type": "writer", "chart_type": "bar", "headers": ["Cat", "A"], "rows": [["x", 1]], "position": "page-2"}, False, ("cursor", "end"), id="writer-invalid-position"),
        pytest.param({"doc_type": "writer", "chart_type": "bar", "headers": ["Cat", "A"], "rows": [["x", 1]], "position": "cursor"}, True, (), id="writer-cursor-position"),
        pytest.param({"doc_type": "calc", "chart_type": "bar", "data_range": "A1:B2", "position": "C1"}, True, (), id="calc-cell-position"),
        pytest.param({"doc_type": "writer", "chart_type": "bar", "headers": ["Cat", "A"], "rows": [["x", "bad"]]}, False, ("must be a number",), id="writer-non-numeric-row"),
    ],
)
def test_chart_upsert_validate_cases(kwargs, expected_ok, expected_substrings):
    ok, err = _tool("chart_upsert").validate(**kwargs)
    assert ok is expected_ok
    if expected_ok:
        assert err is None
    else:
        assert err is not None
        for snippet in expected_substrings:
            assert snippet in err


def test_chart_upsert_edit_nonexistent_chart_returns_error():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    with (
        patch("plugin.calc.charts._supports_service", return_value=True),
        patch.object(SheetCharts, "find", return_value=None),
    ):
        res_edit_nf = _tool("chart_upsert").execute_safe(ctx, name="NonExistentChart")
        assert res_edit_nf["status"] == "error"
        assert "not found" in res_edit_nf["message"]


@pytest.mark.parametrize(
    ("doc_type", "present", "absent"),
    [
        ("calc", {"data_range"}, {"headers", "rows"}),
        ("impress", {"headers"}, {"data_range", "sheet", "has_header", "position"}),
        ("draw", set(), {"position"}),
        ("writer", {"position"}, set()),
    ],
)
def test_chart_upsert_parameters_per_doc_type(doc_type, present, absent):
    props = _tool("chart_upsert").get_parameters(doc_type)["properties"]
    assert present <= props.keys()
    assert absent.isdisjoint(props.keys())


class _UnoRuntimeException(Exception):
    """Stand-in for com.sun.star.uno.RuntimeException (empty str, Message set)."""

    def __init__(self, message: str) -> None:
        super().__init__("")
        self.Message = message


def _empty_uno_struct(*_args, **_kwargs):
    """Same shape as the leaked pytest uno.createUnoStruct stub: no .X."""
    return SimpleNamespace()


def _calc_chart_create_parts():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    charts = MagicMock()
    sheet.getCharts.return_value = charts
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=5)
    bridge = MagicMock()
    bridge.sheet_or_active.return_value = sheet
    bridge.resolve_range_or_address.return_value = cell_range
    chart_doc = MagicMock()
    chart_doc.HasLegend = True
    return _tool("chart_upsert"), ctx, charts, chart_doc, bridge


def test_legend_position_uses_chart_legend_position_enum():
    """Alignment is ChartLegendPosition, and a successful create keeps the chart."""
    from plugin.calc.charts import _CHART_LEGEND_POSITION_ENUM, _apply_chart_styling

    chart_doc = MagicMock()
    chart_doc.HasLegend = True
    with patch("plugin.calc.charts.uno.Enum", return_value="legend-bottom") as enum:
        _apply_chart_styling(chart_doc, legend_position="bottom")
    enum.assert_called_once_with(_CHART_LEGEND_POSITION_ENUM, "BOTTOM")
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
        result = tool.execute_safe(ctx, chart_type="bar", data_range="A1:B6", legend_position="right")
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
        result = tool.execute_safe(ctx, chart_type="column", data_range="A1:B6", legend_position="top")

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
        result = tool.execute_safe(ctx, chart_type="bar", data_range="A1:B6")
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
        patch("plugin.calc.charts._drop_failed_chart_insert") as drop_mock,
        patch("plugin.calc.charts._apply_chart_styling") as styling_mock,
    ):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

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
        patch("plugin.calc.charts._drop_failed_chart_insert") as drop_mock,
        patch("plugin.calc.charts._apply_chart_styling") as styling_mock,
    ):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

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
        result = tool.execute_safe(ctx, chart_type="line", data_range="A1:B6")
    assert result["status"] == "error"
    assert "Cannot access chart content" in result["message"]
    added = charts.addNewByName.call_args[0][0]
    charts.removeByName.assert_called_once_with(added)


def test_edit_legend_failure_does_not_remove_existing_chart():
    from plugin.calc.charts import _drop_failed_chart_insert

    ctx = MagicMock()
    ctx.doc = MagicMock()
    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(MagicMock(), MagicMock(), "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=MagicMock()),
        patch("plugin.calc.charts._apply_chart_styling", side_effect=_UnoRuntimeException("legend")),
        patch("plugin.calc.charts._drop_failed_chart_insert", wraps=_drop_failed_chart_insert) as drop,
    ):
        result = _tool("chart_upsert").execute_safe(ctx, name="Chart_0", legend_position="bottom")
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
        res = DrawPage(ctx.doc).create(ctx, rect, "com.sun.star.chart.BarDiagram", title="My Title", headers=["Cat", "A"], rows=[["x", 1]])

    assert res["status"] == "ok"
    assert res["page"] == 0
    assert any("title failure" in w for w in res.get("warnings", []))
    page.add.assert_called_once_with(shape)
    page.remove.assert_not_called()


def test_writer_drop_falls_back_to_embedded_objects():
    from plugin.calc.charts import _remove_writer_chart

    doc = MagicMock()
    chart_obj = MagicMock()
    chart_obj.getAnchor().getText.return_value.removeTextContent.side_effect = RuntimeError("not in this text")
    embeds = doc.getEmbeddedObjects.return_value
    embeds.hasByName.return_value = True
    _remove_writer_chart(doc, chart_obj, "Chart_2")
    embeds.removeByName.assert_called_once_with("Chart_2")

    chart_obj.getAnchor().getText.return_value.removeTextContent.side_effect = None
    _remove_writer_chart(doc, chart_obj, "Chart_2")
    chart_obj.getAnchor().getText.return_value.removeTextContent.assert_called_with(chart_obj)
    assert embeds.removeByName.call_count == 1


def test_legend_position_none_hides_legend_without_enum():
    from plugin.calc.charts import _apply_chart_styling

    chart_doc = MagicMock()
    chart_doc.HasLegend = True
    with patch("plugin.calc.charts.uno.Enum") as enum:
        _apply_chart_styling(chart_doc, legend_position="none")
    assert chart_doc.HasLegend is False
    enum.assert_not_called()


def test_chart_get_info_and_delete_validate_require_name():
    get_info = _tool("chart_get_info")
    delete = _tool("chart_delete")
    for tool in (get_info, delete):
        ok, err = tool.validate(doc_type="calc")
        assert ok is False
        assert err is not None
        assert "name" in err

        ok, err = tool.validate(doc_type="calc", name="Chart_0")
        assert (ok, err) == (True, None)

    ok, err = _tool("chart_upsert").validate(doc_type="calc", chart_type="bar", data_range="A1:B2")
    assert (ok, err) == (True, None)


def test_chart_upsert_validate_rejects_unknown_chart_type():
    ok, err = _tool("chart_upsert").validate(
        doc_type="calc", chart_type="barz", data_range="A1:B2"
    )
    assert ok is False
    assert "chart_type" in (err or "")


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
    tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    bridge.sheet_or_active.side_effect = DisposedException("gone")
    with patch("plugin.calc.charts.CalcBridge", return_value=bridge):
        res = tool.execute_safe(ctx, chart_type="bar", data_range="A1:B6", sheet="Missing")
        charts.addNewByName.assert_not_called()
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


def test_calc_chart_create_missing_sheet_is_uno_object_error():
    tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    bridge.sheet_or_active.side_effect = UnoObjectError(
        "No sheet named 'Missing'. Available: Sheet1",
        details=name_miss_details("Missing", ["Sheet1"], "sheet_list"),
    )
    with patch("plugin.calc.charts.CalcBridge", return_value=bridge):
        res = tool.execute_safe(ctx, chart_type="bar", data_range="A1:B6", sheet="Missing")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "Available: Sheet1" in res["message"]
    assert "sheet_list" in res["details"]["hint"]
    charts.addNewByName.assert_not_called()


def test_calc_diagram_disposal_propagates_after_removing_chart():
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    chart_doc.setDiagram.side_effect = DisposedException("gone")
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
    ):
        res = tool.execute_safe(ctx, chart_type="bar", data_range="A1:B6")
        added = charts.addNewByName.call_args[0][0]
        charts.removeByName.assert_called_once_with(added)
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


def test_get_chart_info_ranges_disposal_propagates():
    chart_obj = MagicMock()
    chart_obj.getRanges.side_effect = DisposedException("gone")
    ctx: Any = SimpleNamespace(doc=MagicMock())
    host = MagicMock()
    ref = ChartRef(chart_obj, MagicMock(), "calc_sheet")
    with patch("plugin.calc.charts._require_chart", return_value=("Chart_1", ref, host)):
        res = _tool("chart_get_info").execute_safe(ctx, name="Chart_1")
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"

    chart_obj.getRanges.side_effect = RuntimeException("ranges gone")
    with (
        patch("plugin.calc.charts._require_chart", return_value=("Chart_1", ref, host)),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
    ):
        res = _tool("chart_get_info").execute_safe(ctx, name="Chart_1")
        assert res["status"] == "ok"
        assert res["data_ranges"] == []


def test_upsert_edit_disposal_is_not_chart_edit_error():
    chart_obj = MagicMock()
    chart_doc = MagicMock()
    chart_doc.setDiagram.side_effect = DisposedException("gone")
    ctx: Any = SimpleNamespace(doc=MagicMock())
    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(chart_obj, MagicMock(), "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
    ):
        res = _tool("chart_upsert").execute_safe(ctx, name="Chart_1", chart_type="bar")
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"

    chart_doc.setDiagram.side_effect = RuntimeException("edit error")
    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(chart_obj, MagicMock(), "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
    ):
        res = _tool("chart_upsert").execute_safe(ctx, name="Chart_1", chart_type="bar")
        assert res["status"] == "error"
        assert res.get("code") == "CHART_EDIT_ERROR"


def test_writer_chart_insert_failure_reports_chart_create_error():
    """Every Writer insert fallback failing reaches the create arm (CHART_CREATE_ERROR)."""
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value = []
    text = doc.getText.return_value
    text.getString.return_value = "body"
    text.insertTextContent.side_effect = RuntimeError("insert rejected")
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())
    with patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["a", "b"], rows=[["x", 1]])
    assert res["status"] == "error"
    assert res.get("code") == "CHART_CREATE_ERROR"
    assert "insert rejected" in res["message"]
    assert text.insertTextContent.call_count == 2


def test_data_arrays_validation_rejects_bad_shapes():
    tool = _tool("chart_upsert")
    for base in ({"name": "Chart_1"}, {"chart_type": "bar"}):
        # Non-numeric cell in data column
        ok, err = tool.validate(doc_type="writer", headers=["Cat", "Val"], rows=[["A", "not_a_num"]], **base)
        assert not ok and "must be a number" in (err or "")

        # Short row
        ok, err = tool.validate(doc_type="writer", headers=["Cat", "Val"], rows=[["A"]], **base)
        assert not ok and "does not match" in (err or "")

        # Long row
        ok, err = tool.validate(doc_type="writer", headers=["Cat", "Val"], rows=[["A", 1, 2]], **base)
        assert not ok and "does not match" in (err or "")

        # Boolean rejected
        ok, err = tool.validate(doc_type="writer", headers=["Cat", "Val"], rows=[["A", True]], **base)
        assert not ok and "cannot be boolean" in (err or "")


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
        patch("plugin.calc.charts._drop_failed_chart_insert") as drop_mock,
    ):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])
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
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

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
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

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
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])

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

    with patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"

    text.insertControlCharacter.assert_not_called()
    text.insertTextContent.assert_not_called()


def test_writer_default_mock_enumeration_counts_as_empty():
    text = MagicMock()
    assert _writer_body_has_no_text(text) is True
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
        patch.object(WriterEmbeds, "find", return_value=ChartRef(chart_obj, MagicMock(), "writer_embed")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._drop_failed_chart_insert") as drop_mock,
    ):
        res = _tool("chart_upsert").execute_safe(ctx, name="Chart_1", headers=["Cat", "A"], rows=[["x", 1]])
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

    with caplog.at_level(logging.INFO, logger="plugin.calc.charts"):
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["SecretDistinctCellXYZ", 999.0]])
    assert "SecretDistinctCellXYZ" not in caplog.text

    # At DEBUG, first row may be inspected
    with caplog.at_level(logging.DEBUG, logger="plugin.calc.charts"):
        _apply_chart_data_arrays(chart_doc, ["Cat", "Val"], [["SecretDistinctCellXYZ", 999.0]])
    assert "SecretDistinctCellXYZ" in caplog.text


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
    bridge.resolve_range_or_address.return_value = cell_range

    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(chart_obj, sheet, "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
    ):
        res = _tool("chart_upsert").execute_safe(
            ctx,
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
    bridge.resolve_range_or_address.side_effect = ValueError("invalid range")

    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(chart_obj, sheet, "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
    ):
        res = _tool("chart_upsert").execute_safe(
            ctx,
            name="Chart_0",
            chart_type="pie",
            data_range="Invalid:Range",
        )

    assert res["status"] == "error"
    assert res.get("code") == "INVALID_PARAM"
    chart_obj.setRanges.assert_not_called()
    chart_doc.setDiagram.assert_not_called()


def test_calc_edit_missing_sheet_in_data_range_is_uno_object_error():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    chart_obj = MagicMock()
    chart_doc = MagicMock()

    bridge = MagicMock()
    bridge.resolve_range_or_address.side_effect = UnoObjectError(
        "No sheet named 'Missing'. Available: Sheet1",
        details=name_miss_details("Missing", ["Sheet1"], "sheet_list"),
    )

    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(chart_obj, sheet, "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
    ):
        res = _tool("chart_upsert").execute_safe(
            ctx,
            name="Chart_0",
            chart_type="pie",
            data_range="Missing.A1:B5",
        )

    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "Available: Sheet1" in res["message"]
    assert "sheet_list" in res["details"]["hint"]
    chart_doc.setDiagram.assert_not_called()


def test_calc_create_bad_position_does_not_call_add_new_by_name():
    _tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    rect = MagicMock(Width=12000, Height=8000, X=1000, Y=1000)

    with patch("plugin.calc.charts.CalcBridge", return_value=bridge):
        # 1. Mismatched sheet prefix
        with pytest.raises(ToolExecutionError) as ei1:
            SheetCharts(ctx.doc).create(ctx, rect, "com.sun.star.chart.BarDiagram", data_range="A1:B2", position="OtherSheet.A1")
        assert ei1.value.code == "INVALID_PARAM"
        assert "Position names sheet" in ei1.value.message
        charts.addNewByName.assert_not_called()

        # 2. Invalid position address (range or unparseable)
        with pytest.raises(ToolExecutionError) as ei2:
            SheetCharts(ctx.doc).create(ctx, rect, "com.sun.star.chart.BarDiagram", data_range="A1:B2", position="A1:B2")
        assert ei2.value.code == "INVALID_PARAM"
        assert "Invalid position" in ei2.value.message
        charts.addNewByName.assert_not_called()


def test_calc_create_valid_position_sets_rect_and_creates():
    _tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    sheet = bridge.sheet_or_active()
    cell_obj = SimpleNamespace(
        Position=SimpleNamespace(X=2500, Y=3500),
        Size=SimpleNamespace(Width=5000, Height=2000),
    )
    sheet.getCellRangeByName.return_value = cell_obj

    rect = MagicMock(Width=12000, Height=8000, X=1000, Y=1000)

    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
    ):
        res = SheetCharts(ctx.doc).create(ctx, rect, "com.sun.star.chart.BarDiagram", data_range="A1:B2", position="B5")

    assert res["status"] == "ok"
    assert rect.X == 2500
    assert rect.Y == 3500
    assert rect.Width == 12000
    assert rect.Height == 8000
    charts.addNewByName.assert_called_once()


def test_calc_edit_position_warns_and_does_not_move():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    sheet = MagicMock()
    chart_obj = MagicMock()
    chart_doc = MagicMock()
    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(chart_obj, sheet, "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
    ):
        res = _tool("chart_upsert").execute_safe(
            ctx,
            name="Chart_0",
            position="D5",
        )

    assert res["status"] == "ok"
    assert any("placement is unchanged" in w.lower() for w in res.get("warnings", []))
    sheet.getCellRangeByName.assert_not_called()


def test_unsupported_dim3d_yields_warning():
    chart_doc = MagicMock()
    diagram = MagicMock(spec=["Stacked"])  # no Dim3D attribute
    chart_doc.getDiagram.return_value = diagram
    warnings = _apply_chart_styling(chart_doc, is_3d=True)
    assert any("Dim3D" in w for w in warnings)


def test_chart_upsert_name_hit_edits_without_create():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    sheet = MagicMock()
    chart_obj = MagicMock()
    chart_doc = MagicMock()
    ctx: Any = SimpleNamespace(doc=doc)

    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(chart_obj, sheet, "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts.SheetCharts.create") as mock_create,
    ):
        res = _tool("chart_upsert").execute_safe(ctx, name="Chart_0", title="Updated")
    assert res["status"] == "ok"
    assert res["message"] == "Chart updated."
    mock_create.assert_not_called()


def test_chart_upsert_name_miss_calc_create_uses_requested_name():
    _tool_unused, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch.object(SheetCharts, "find", return_value=None),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
    ):
        res = _tool("chart_upsert").execute_safe(ctx, name="Sales", chart_type="bar", data_range="A1:B6")
    assert res["status"] == "ok"
    assert charts.addNewByName.call_args[0][0] == "Sales"


def test_chart_upsert_name_miss_without_create_fields_not_found():
    tool, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    with patch.object(SheetCharts, "find", return_value=None):
        res_nf = tool.execute_safe(ctx, name="Sales")
        res_no_range = tool.execute_safe(ctx, name="Sales", chart_type="bar")
    assert res_nf["status"] == "error"
    assert res_nf["message"] == "Chart 'Sales' not found."
    assert "chart_list" in res_nf["details"]["hint"]
    assert res_no_range["status"] == "error"
    assert "to create it also pass data_range" in res_no_range["message"]
    charts.addNewByName.assert_not_called()


def test_chart_upsert_create_sets_created_flag():
    _tool_unused, ctx, charts, chart_doc, bridge = _calc_chart_create_parts()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value=set()),
    ):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", data_range="A1:B6")
    assert res["status"] == "ok"
    assert res.get("created") is True


def test_writer_create_requested_name_already_used():
    from plugin.calc.charts import WriterEmbeds

    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    rect = MagicMock()
    host = WriterEmbeds(ctx.doc)
    with patch.object(WriterEmbeds, "names", return_value={"Sales"}):
        with pytest.raises(ToolExecutionError) as ei:
            host.create(
                ctx,
                rect,
                "com.sun.star.chart.BarDiagram",
                name="Sales",
                chart_type="bar",
                headers=["Cat", "A"],
                rows=[["x", 1]],
            )
    assert ei.value.code == "INVALID_PARAM"
    assert ei.value.message == "Chart name 'Sales' is already used."


def test_draw_create_requested_name_already_used():
    from plugin.calc.charts import DrawPage

    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc
    doc.getDrawPages.return_value.getCount.return_value = 1
    doc.getDrawPages.return_value.getByIndex.return_value = MagicMock()
    doc.getCurrentController.return_value = None
    doc.createInstance.return_value = MagicMock()
    rect = MagicMock()
    host = DrawPage(doc)
    page = doc.getDrawPages.return_value.getByIndex.return_value

    with patch.object(DrawPage, "names", return_value={"Sales"}):
        with patch("plugin.calc.charts._chart_document_from_host", return_value=MagicMock()):
            with patch("plugin.calc.charts._apply_chart_data_arrays"):
                with patch("plugin.calc.charts._apply_chart_styling", return_value=[]):
                    with pytest.raises(ToolExecutionError) as ei:
                        host.create(
                            ctx,
                            rect,
                            "com.sun.star.chart.BarDiagram",
                            name="Sales",
                            chart_type="bar",
                            headers=["Cat", "A"],
                            rows=[["x", 1]],
                        )
    page.remove.assert_called_once()
    assert ei.value.code == "INVALID_PARAM"
    assert ei.value.message == "Chart name 'Sales' is already used."


def test_chart_upsert_requested_name_already_used():
    tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    with (
        patch("plugin.calc.charts.CalcBridge", return_value=bridge),
        patch.object(SheetCharts, "find", return_value=None),
        patch("plugin.calc.charts._get_all_calc_chart_names", return_value={"Sales"}),
    ):
        res = tool.execute_safe(ctx, name="Sales", chart_type="bar", data_range="A1:B6")
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["message"] == "Chart name 'Sales' is already used."
    charts.addNewByName.assert_not_called()


def test_calc_create_missing_sheet_in_data_range_is_uno_object_error():
    tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    bridge.resolve_range_or_address.side_effect = UnoObjectError(
        "No sheet named 'Missing'. Available: Sheet1",
        details=name_miss_details("Missing", ["Sheet1"], "sheet_list"),
    )
    with patch("plugin.calc.charts.CalcBridge", return_value=bridge):
        res = tool.execute_safe(ctx, chart_type="bar", data_range="Missing.A1:B6")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert res["message"].startswith("No sheet named 'Missing'")
    assert "sheet_list" in res["details"]["hint"]
    charts.addNewByName.assert_not_called()


def test_calc_create_bad_range_is_invalid_param():
    tool, ctx, charts, _chart_doc, bridge = _calc_chart_create_parts()
    bridge.resolve_range_or_address.side_effect = ValueError("bad range")
    with patch("plugin.calc.charts.CalcBridge", return_value=bridge):
        res = tool.execute_safe(ctx, chart_type="bar", data_range="A1:ZZ")
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["message"] == "Invalid data_range 'A1:ZZ': ValueError: bad range"
    charts.addNewByName.assert_not_called()


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
        patch.object(WriterEmbeds, "find", return_value=ChartRef(chart_obj, MagicMock(), "writer_draw_page")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
    ):
        res = _tool("chart_upsert").execute_safe(
            ctx,
            name="Chart_0",
            headers=["Cat", "Val"],
            rows=[["A", 10.0]],
            colors=["#FF0000"],
        )

    assert res["status"] == "ok"
    assert "setData" in call_order
    assert any("setPropertyValue" in c for c in call_order)
    assert call_order.index("setData") < next(i for i, c in enumerate(call_order) if "setPropertyValue" in c)


def test_upsert_validate_rejects_nan_inf_and_cell_cap():
    tool = _tool("chart_upsert")

    # Reject NaN and Inf
    ok, err = tool.validate(doc_type="writer", chart_type="bar", headers=["Cat", "Val"], rows=[["A", float("nan")]])
    assert not ok and "finite number" in (err or "")
    ok, err = tool.validate(doc_type="writer", chart_type="bar", headers=["Cat", "Val"], rows=[["A", "nan"]])
    assert not ok and "finite number" in (err or "")
    ok, err = tool.validate(doc_type="writer", chart_type="bar", headers=["Cat", "Val"], rows=[["A", float("inf")]])
    assert not ok and "finite number" in (err or "")
    ok, err = tool.validate(doc_type="writer", chart_type="bar", headers=["Cat", "Val"], rows=[["A", "-inf"]])
    assert not ok and "finite number" in (err or "")

    # Reject cell count above 10,000
    headers = ["Cat"] + [f"C{i}" for i in range(100)]  # 101 columns
    rows = [["R"] + [1.0] * 100 for _ in range(100)]  # 100 rows * 101 cols = 10100 > 10000
    ok, err = tool.validate(doc_type="writer", chart_type="bar", headers=headers, rows=rows)
    assert not ok and "exceeds limit" in (err or "")


class _RenamedChartHost(MagicMock):
    """Host that ignores Name writes and keeps its own name (LO can rename an embed)."""

    @property
    def Name(self):
        return "Object 1"

    @Name.setter
    def Name(self, value):
        pass


def test_writer_create_reports_host_assigned_name():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    doc.createInstance.return_value = _RenamedChartHost()
    chart_doc = MagicMock()
    chart_doc.createInstance.return_value = MagicMock()
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())
    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts._await_writer_chart_document", return_value=chart_doc),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts._process_events"),
    ):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])
    assert res["status"] == "ok"
    assert res["name"] == "Object 1"
    assert "'Object 1'" in res["message"]


def test_writer_create_rollback_targets_host_assigned_name():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    doc.createInstance.return_value = _RenamedChartHost()
    chart_doc = MagicMock()
    chart_doc.createInstance.return_value = MagicMock()
    chart_doc.setDiagram.side_effect = _UnoRuntimeException("diagram rejected")
    ctx: Any = SimpleNamespace(doc=doc, ctx=MagicMock())
    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts._await_writer_chart_document", return_value=chart_doc),
        patch("plugin.calc.charts._drop_failed_chart_insert") as drop_mock,
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts._process_events"),
    ):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", headers=["Cat", "A"], rows=[["x", 1]])
    assert res["status"] == "error"
    assert res.get("code") == "CHART_CREATE_ERROR"
    assert drop_mock.call_args[0][1] == "Object 1"


def _two_sheet_calc_doc_for_cross_sheet_charts():
    sheet1 = MagicMock()
    sheet1.getName.return_value = "Sheet1"
    charts = MagicMock()
    charts.getElementNames.return_value = []
    chart_obj = MagicMock()
    chart_doc = MagicMock()
    chart_obj.getEmbeddedObject.return_value = chart_doc
    charts.getByName.return_value = chart_obj
    sheet1.getCharts.return_value = charts

    data_sheet = MagicMock()
    data_sheet.getName.return_value = "Data"
    data_range_obj = MagicMock()
    addr = MagicMock(Sheet=1, StartColumn=0, StartRow=0, EndColumn=1, EndRow=5)
    data_range_obj.getRangeAddress.return_value = addr
    data_sheet.getCellRangeByPosition.return_value = data_range_obj

    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda n: n in ("Sheet1", "Data")
    sheets.getByName.side_effect = lambda n: sheet1 if n == "Sheet1" else data_sheet

    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    doc.getSheets.return_value = sheets
    doc.getCurrentController.return_value.getActiveSheet.return_value = sheet1
    return doc, sheet1, data_sheet, charts, chart_obj, chart_doc, addr


def test_calc_chart_create_cross_sheet_data_range_on_named_sheet():
    doc, _sheet1, data_sheet, charts, _chart_obj, chart_doc, addr = _two_sheet_calc_doc_for_cross_sheet_charts()
    ctx: Any = SimpleNamespace(doc=doc)
    with (
        patch("plugin.calc.charts.uno.createUnoStruct", side_effect=_empty_uno_struct),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts._process_events"),
    ):
        res = _tool("chart_upsert").execute_safe(ctx, chart_type="bar", data_range="Data.A1:B6", sheet="Sheet1")

    assert res["status"] == "ok"
    data_sheet.getCellRangeByPosition.assert_called_once_with(0, 0, 1, 5)
    charts.addNewByName.assert_called_once()
    assert charts.addNewByName.call_args[0][2] == (addr,)
    assert chart_doc is not None


def test_calc_chart_edit_cross_sheet_data_range_from_host_sheet():
    doc, sheet1, data_sheet, _charts, chart_obj, chart_doc, addr = _two_sheet_calc_doc_for_cross_sheet_charts()
    ctx: Any = SimpleNamespace(doc=doc)
    with (
        patch.object(SheetCharts, "find", return_value=ChartRef(chart_obj, sheet1, "calc_sheet")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
    ):
        res = _tool("chart_upsert").execute_safe(ctx, name="Chart_0", data_range="Data.A1:B6")

    assert res["status"] == "ok"
    data_sheet.getCellRangeByPosition.assert_called_once_with(0, 0, 1, 5)
    chart_obj.setRanges.assert_called_once_with((addr,))



