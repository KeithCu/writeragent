"""Unit tests for SheetAnalyzer helpers, including Calc chat context."""

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.analyzer import get_calc_context_for_chat, get_full_calc_text


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
