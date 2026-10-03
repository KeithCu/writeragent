"""Unit tests for SheetAnalyzer helpers, including Calc chat context."""

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.analyzer import get_calc_context_for_chat, get_full_calc_text


def test_get_sheet_summary_used_range_letters():
    from plugin.calc.analyzer import SheetAnalyzer

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
    from plugin.calc.analyzer import SheetAnalyzer

    class DisposedException(Exception):
        pass

    bridge = MagicMock()
    bridge.get_active_sheet.side_effect = DisposedException("Binary URP bridge disposed during call")
    with pytest.raises(DisposedException, match="URP"):
        SheetAnalyzer(bridge).get_sheet_summary()


def test_get_sheet_summary_wraps_other_errors():
    from plugin.calc.analyzer import SheetAnalyzer
    from plugin.framework.errors import ToolExecutionError

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
        patch("plugin.calc.bridge.CalcBridge", return_value=MagicMock()),
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
        patch("plugin.calc.bridge.CalcBridge", return_value=MagicMock()),
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
    hidden = {"sheet_name": "__Anonymous_Sheet_DB__0", "used_range": "A1:A1", "headers": []}
    visible = {"sheet_name": "Sales", "used_range": "A1:C3", "headers": ["A", "B", "C"]}
    analyzer = MagicMock()
    analyzer.get_sheet_summary.side_effect = [hidden, visible]
    model = MagicMock()
    model.getSheets.return_value.getElementNames.return_value = (
        "__Anonymous_Sheet_DB__0",
        "Sales",
    )
    with (
        patch("plugin.calc.bridge.CalcBridge", return_value=MagicMock()),
        patch("plugin.calc.analyzer.SheetAnalyzer", return_value=analyzer),
    ):
        text = get_full_calc_text(model, max_chars=100)
    assert "Sheet: Sales" in text
    assert "__Anonymous_Sheet_DB__0" not in text
