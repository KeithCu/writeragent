"""Unit tests for SheetAnalyzer helpers, including Calc chat context."""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from plugin.calc.analyzer import SheetAnalyzer, _header_text, get_calc_context_for_chat, get_full_calc_text
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError


def test_get_sheet_summary_used_range_letters():
    bridge = MagicMock()
    sheet = bridge.get_active_sheet.return_value
    sheet.getName.return_value = "Sheet1"
    addr = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=2)
    sheet.createCursor.return_value.getRangeAddress.return_value = addr
    sheet.getCellRangeByPosition.return_value.getDataArray.return_value = (("Name", "Amt"),)
    sheet.getCharts.return_value.getCount.return_value = 0
    sheet.getCharts.return_value.getElementNames.return_value = ()
    sheet.getAnnotations.return_value.getCount.return_value = 0
    sheet.getPropertyValue.return_value = False
    sheet.DrawPage.getCount.return_value = 0

    summary = SheetAnalyzer(bridge).get_sheet_summary()

    assert summary["used_range"] == "A1:B3"
    bridge._index_to_column.assert_not_called()


def test_get_sheet_summary_reraises_disposed():
    class DisposedException(Exception):
        pass

    bridge = MagicMock()
    bridge.get_active_sheet.side_effect = DisposedException("Binary URP bridge disposed during call")
    with pytest.raises(DisposedException, match="URP"):
        SheetAnalyzer(bridge).get_sheet_summary()


def test_get_sheet_summary_wraps_other_errors():
    bridge = MagicMock()
    bridge.get_active_sheet.side_effect = ValueError("bad sheet")
    with pytest.raises(ToolExecutionError, match="bad sheet"):
        SheetAnalyzer(bridge).get_sheet_summary()


def test_get_calc_context_for_chat_requires_ctx():
    with pytest.raises(ValueError, match="ctx is required"):
        get_calc_context_for_chat(object())


def test_get_full_calc_text_uses_sheet_summary():
    summary = {"sheet_name": "Sheet1", "used_range": "A1:C3", "headers": ["Name", None, "Amt"]}
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = summary
    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=MagicMock()),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
    ):
        text = get_full_calc_text(MagicMock(), max_chars=100)
    assert "Sheet: Sheet1" in text
    assert "Used Range: A1:C3" in text
    assert "Columns: Name, Amt" in text


def test_get_calc_context_for_chat_omits_leading_underscore_sheets():
    summary = {
        "sheet_name": "Sheet1",
        "used_range": "A1:B2",
        "row_count": 2,
        "col_count": 2,
        "headers": ["A", "B"],
    }
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = summary
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/demo.ods"
    model.getSheets.return_value.getElementNames.return_value = (
        "Sheet1",
        "_foo",
        "__Anonymous_Sheet_DB__0",
    )
    controller = MagicMock()
    controller.getSelection.return_value = None
    model.getCurrentController.return_value = controller
    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=MagicMock()),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
        patch("plugin.calc.analyzer.check_disposed"),
        patch("plugin.calc.analyzer.safe_call", side_effect=lambda fn, _label: fn()),
    ):
        text = get_calc_context_for_chat(model, ctx=object())
    assert "Sheets: ['Sheet1']" in text
    assert "_foo" not in text
    assert "__Anonymous_Sheet_DB__0" not in text
    assert "Active Sheet: Sheet1" in text


def test_get_full_calc_text_skips_internal_active_sheet():
    visible = {"sheet_name": "Sales", "used_range": "A1:C3", "headers": ["A", "B", "C"]}
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = visible
    model = MagicMock()
    model.getSheets.return_value.getElementNames.return_value = (
        "__Anonymous_Sheet_DB__0",
        "Sales",
    )
    bridge = MagicMock()
    bridge.get_active_sheet.return_value.getName.return_value = "__Anonymous_Sheet_DB__0"
    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=bridge),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
    ):
        text = get_full_calc_text(model, max_chars=100)
    assert "Sheet: Sales" in text
    assert "__Anonymous_Sheet_DB__0" not in text
    analyzer.get_sheet_summary.assert_called_once_with(sheet_name="Sales", include_extras=False)


def test_get_calc_context_for_chat_disposed():
    model = MagicMock()
    with patch("plugin.calc.analyzer.SheetAnalyzer.get_sheet_summary", side_effect=DocumentDisposedError("stale")):
        with pytest.raises(DocumentDisposedError):
            get_calc_context_for_chat(model, 1000, ctx=MagicMock())


def test_header_text_normalization():
    assert _header_text(None) is None
    assert _header_text("") is None
    assert _header_text(2024.0) == "2024"
    assert _header_text(0.0) == "0"
    assert _header_text("Name") == "Name"
    assert _header_text("A, B") == "A, B"
    assert _header_text(1e16) == "1e+16"


def test_get_sheet_summary_header_normalization():
    bridge = MagicMock()
    sheet = bridge.get_active_sheet.return_value
    sheet.getName.return_value = "Sheet1"
    addr = MagicMock(StartColumn=0, StartRow=0, EndColumn=4, EndRow=1)
    sheet.createCursor.return_value.getRangeAddress.return_value = addr
    sheet.getCellRangeByPosition.return_value.getDataArray.return_value = (
        ("Name", 2024.0, 0.0, "A, B", ""),
    )
    sheet.getCharts.return_value.getCount.return_value = 0
    sheet.getCharts.return_value.getElementNames.return_value = ()
    sheet.getAnnotations.return_value.getCount.return_value = 0
    sheet.getPropertyValue.return_value = False
    sheet.DrawPage.getCount.return_value = 0

    summary = SheetAnalyzer(bridge).get_sheet_summary()
    assert summary["headers"] == ["Name", "2024", "0", "A, B", None]


def test_get_calc_context_for_chat_caps_columns_at_100():
    headers = [f"Col{i}" for i in range(105)]
    summary = {
        "sheet_name": "Sheet1",
        "used_range": "A1:DA1",
        "row_count": 1,
        "col_count": 105,
        "headers": headers,
    }
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = summary
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/demo.ods"
    model.getSheets.return_value.getElementNames.return_value = ("Sheet1",)
    bridge = MagicMock()
    bridge.get_active_sheet.return_value.getName.return_value = "Sheet1"
    controller = MagicMock()
    controller.getSelection.return_value = None
    model.getCurrentController.return_value = controller
    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=bridge),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
        patch("plugin.calc.analyzer.safe_call", side_effect=lambda fn, _label: fn()),
    ):
        text = get_calc_context_for_chat(model, ctx=object())
    assert "Col0" in text
    assert "Col99" in text
    assert "(+5 more columns)" in text
    assert "Col100" not in text


def test_get_calc_context_for_chat_truncation():
    summary = {
        "sheet_name": "Sheet1",
        "used_range": "A1:B10",
        "row_count": 10,
        "col_count": 2,
        "headers": ["ColA", "ColB"],
    }
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = summary
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/demo.ods"
    model.getSheets.return_value.getElementNames.return_value = ("Sheet1",)
    bridge = MagicMock()
    bridge.get_active_sheet.return_value.getName.return_value = "Sheet1"
    controller = MagicMock()
    controller.getSelection.return_value = None
    model.getCurrentController.return_value = controller

    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=bridge),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
        patch("plugin.calc.analyzer.safe_call", side_effect=lambda fn, _label: fn()),
    ):
        out = get_calc_context_for_chat(model, max_context=50, ctx=object())
        assert out.endswith("\n\n[... document truncated ...]")
        assert len(out) == 50 + len("\n\n[... document truncated ...]")

        out_neg = get_calc_context_for_chat(model, max_context=-1, ctx=object())
        assert out_neg == "\n\n[... document truncated ...]"

        out_bool = get_calc_context_for_chat(model, max_context=True, ctx=object())
        assert out_bool == "\n\n[... document truncated ...]"


def test_get_full_calc_text_truncation():
    summary = {"sheet_name": "Sheet1", "used_range": "A1:C3", "headers": ["Name", None, "Amt"]}
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = summary
    bridge = MagicMock()
    bridge.get_active_sheet.return_value.getName.return_value = "Sheet1"
    model = MagicMock()
    model.getSheets.return_value.getElementNames.return_value = ("Sheet1",)
    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=bridge),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
    ):
        out = get_full_calc_text(model, max_chars=20)
    assert out.endswith("\n\n[... document truncated ...]")
    assert len(out) == 20 + len("\n\n[... document truncated ...]")


def test_get_calc_context_for_chat_long_selection_truncation():
    summary = {
        "sheet_name": "Sheet1",
        "used_range": "A1:B10",
        "row_count": 10,
        "col_count": 2,
        "headers": ["ColA", "ColB"],
    }
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = summary
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/demo.ods"
    model.getSheets.return_value.getElementNames.return_value = ("Sheet1",)
    bridge = MagicMock()
    bridge.get_active_sheet.return_value.getName.return_value = "Sheet1"
    controller = MagicMock()
    selection = MagicMock()
    addr = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=10)
    selection.getRangeAddress.return_value = addr
    controller.getSelection.return_value = selection
    model.getCurrentController.return_value = controller

    inspector = MagicMock()
    inspector.read_range.return_value = [[{"value": "V" * 200}, {"value": "W" * 200}]]

    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=bridge),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
        patch("plugin.calc.inspector.CellInspector", return_value=inspector),
        patch("plugin.calc.analyzer.safe_call", side_effect=lambda fn, _label: fn()),
    ):
        out = get_calc_context_for_chat(model, max_context=150, ctx=object())
    assert out.endswith("\n\n[... document truncated ...]")
    assert len(out) == 150 + len("\n\n[... document truncated ...]")


def test_get_calc_context_for_chat_hidden_active_sheet_omits_selection():
    visible_summary = {
        "sheet_name": "Sales",
        "used_range": "A1:B2",
        "row_count": 2,
        "col_count": 2,
        "headers": ["A", "B"],
    }
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = visible_summary
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/demo.ods"
    model.getSheets.return_value.getElementNames.return_value = (
        "__Anonymous_Sheet_DB__0",
        "Sales",
    )
    bridge = MagicMock()
    bridge.get_active_sheet.return_value.getName.return_value = "__Anonymous_Sheet_DB__0"

    controller = MagicMock()
    selection = MagicMock()
    controller.getSelection.return_value = selection
    model.getCurrentController.return_value = controller

    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=bridge),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
        patch("plugin.calc.analyzer.safe_call", side_effect=lambda fn, _label: fn()),
    ):
        text = get_calc_context_for_chat(model, ctx=object())

    assert "Active Sheet: Sales" in text
    assert "Selection omitted: active tab is an internal sheet." in text
    assert "Selection Content (CSV-like):" not in text
    analyzer.get_sheet_summary.assert_called_once_with(sheet_name="Sales", include_extras=False)


@pytest.mark.parametrize("scenario", ["none_controller", "selection_error", "read_range_error"])
def test_get_calc_context_for_chat_selection_failure_preserves_summary(scenario):
    summary = {
        "sheet_name": "Sheet1",
        "used_range": "A1:B2",
        "row_count": 2,
        "col_count": 2,
        "headers": ["A", "B"],
    }
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = summary
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/demo.ods"
    model.getSheets.return_value.getElementNames.return_value = ("Sheet1",)
    bridge = MagicMock()
    bridge.get_active_sheet.return_value.getName.return_value = "Sheet1"

    if scenario == "none_controller":
        model.getCurrentController.return_value = None
    elif scenario == "selection_error":
        controller = MagicMock()
        controller.getSelection.side_effect = RuntimeError("failed to get selection")
        model.getCurrentController.return_value = controller
    else:
        controller = MagicMock()
        selection = MagicMock()
        selection.getRangeAddress.return_value = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=1)
        controller.getSelection.return_value = selection
        model.getCurrentController.return_value = controller

    inspector = MagicMock()
    inspector.read_range.side_effect = RuntimeError("range read error")

    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=bridge),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
        patch("plugin.calc.inspector.CellInspector", return_value=inspector),
        patch("plugin.calc.analyzer.safe_call", side_effect=lambda fn, _label: fn()),
    ):
        text = get_calc_context_for_chat(model, ctx=object())

    assert "Active Sheet: Sheet1" in text
    assert "Used Range: A1:B2" in text
    assert "Selection Content (CSV-like):" not in text


def test_get_calc_context_for_chat_disposed_from_read_range():
    class DisposedException(Exception):
        pass

    summary = {
        "sheet_name": "Sheet1",
        "used_range": "A1:B2",
        "row_count": 2,
        "col_count": 2,
        "headers": ["A", "B"],
    }
    analyzer = MagicMock()
    analyzer.get_sheet_summary.return_value = summary
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/demo.ods"
    model.getSheets.return_value.getElementNames.return_value = ("Sheet1",)
    bridge = MagicMock()
    bridge.get_active_sheet.return_value.getName.return_value = "Sheet1"
    controller = MagicMock()
    selection = MagicMock()
    selection.getRangeAddress.return_value = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=1)
    controller.getSelection.return_value = selection
    model.getCurrentController.return_value = controller

    inspector = MagicMock()
    inspector.read_range.side_effect = DisposedException("object disposed")

    with (
        patch("plugin.calc.analyzer.CalcBridge", return_value=bridge),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
        patch("plugin.calc.inspector.CellInspector", return_value=inspector),
        patch("plugin.calc.analyzer.safe_call", side_effect=lambda fn, _label: fn()),
    ):
        with pytest.raises(DocumentDisposedError):
            get_calc_context_for_chat(model, ctx=object())


def test_get_calc_context_for_chat_disposed_raw_disposed_exception_from_summary():
    class DisposedException(Exception):
        pass

    model = MagicMock()
    with patch("plugin.calc.analyzer.SheetAnalyzer.get_sheet_summary", side_effect=DisposedException("stale")):
        with pytest.raises(DocumentDisposedError):
            get_calc_context_for_chat(model, ctx=object())


@pytest.mark.parametrize("failing_attr", ["getCharts", "getAnnotations", "getPropertyValue", "DrawPage"])
def test_get_sheet_summary_disposed_on_optional_metric_raises(failing_attr):
    class DisposedException(Exception):
        pass

    bridge = MagicMock()
    sheet = bridge.get_active_sheet.return_value
    sheet.getName.return_value = "Sheet1"
    addr = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=2)
    sheet.createCursor.return_value.getRangeAddress.return_value = addr
    sheet.getCellRangeByPosition.return_value.getDataArray.return_value = (("Name", "Amt"),)

    sheet.getCharts.return_value.getCount.return_value = 0
    sheet.getCharts.return_value.getElementNames.return_value = ()
    sheet.getAnnotations.return_value.getCount.return_value = 0
    sheet.getPropertyValue.return_value = False
    sheet.DrawPage.getCount.return_value = 0

    if failing_attr == "DrawPage":
        type(sheet).DrawPage = PropertyMock(side_effect=DisposedException("DrawPage disposed"))
    elif failing_attr == "getPropertyValue":
        sheet.getPropertyValue.side_effect = DisposedException("property disposed")
    elif failing_attr == "getAnnotations":
        sheet.getAnnotations.side_effect = DisposedException("annotations disposed")
    elif failing_attr == "getCharts":
        sheet.getCharts.side_effect = DisposedException("charts disposed")

    analyzer = SheetAnalyzer(bridge)
    with pytest.raises(DocumentDisposedError):
        analyzer.get_sheet_summary(include_extras=True)


@pytest.mark.parametrize("failing_attr", ["getCharts", "getAnnotations", "getPropertyValue", "DrawPage"])
def test_get_sheet_summary_value_error_on_optional_metric_uses_defaults(failing_attr):
    bridge = MagicMock()
    sheet = bridge.get_active_sheet.return_value
    sheet.getName.return_value = "Sheet1"
    addr = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=2)
    sheet.createCursor.return_value.getRangeAddress.return_value = addr
    sheet.getCellRangeByPosition.return_value.getDataArray.return_value = (("Name", "Amt"),)

    sheet.getCharts.return_value.getCount.return_value = 0
    sheet.getCharts.return_value.getElementNames.return_value = ()
    sheet.getAnnotations.return_value.getCount.return_value = 0
    sheet.getPropertyValue.return_value = False
    sheet.DrawPage.getCount.return_value = 0

    if failing_attr == "DrawPage":
        type(sheet).DrawPage = PropertyMock(side_effect=ValueError("no draw page"))
    elif failing_attr == "getPropertyValue":
        sheet.getPropertyValue.side_effect = ValueError("no property")
    elif failing_attr == "getAnnotations":
        sheet.getAnnotations.side_effect = ValueError("no annotations")
    elif failing_attr == "getCharts":
        sheet.getCharts.side_effect = ValueError("no charts")

    analyzer = SheetAnalyzer(bridge)
    summary = analyzer.get_sheet_summary(include_extras=True)
    if failing_attr == "DrawPage":
        assert summary["shape_count"] == 0
    elif failing_attr == "getPropertyValue":
        assert summary["has_merges"] is None
    elif failing_attr == "getAnnotations":
        assert summary["annotation_count"] == 0
    elif failing_attr == "getCharts":
        assert summary["chart_count"] == 0
        assert summary["charts"] == []


def test_get_sheet_summary_include_extras_false_skips_optional_metrics():
    bridge = MagicMock()
    sheet = bridge.get_active_sheet.return_value
    sheet.getName.return_value = "Sheet1"
    addr = MagicMock(StartColumn=0, StartRow=0, EndColumn=1, EndRow=2)
    sheet.createCursor.return_value.getRangeAddress.return_value = addr
    sheet.getCellRangeByPosition.return_value.getDataArray.return_value = (("Name", "Amt"),)

    analyzer = SheetAnalyzer(bridge)
    summary = analyzer.get_sheet_summary(include_extras=False)
    sheet.getCharts.assert_not_called()
    sheet.getAnnotations.assert_not_called()
    sheet.getPropertyValue.assert_not_called()
    assert "chart_count" not in summary
    assert "charts" not in summary
    assert "annotation_count" not in summary
    assert "has_merges" not in summary
    assert "shape_count" not in summary

