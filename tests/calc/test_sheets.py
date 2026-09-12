# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.calc.bridge import filter_agent_sheet_names, is_agent_visible_sheet
from plugin.calc.sheets import GetSheetSummary, ListSheets


def test_is_agent_visible_sheet_hides_leading_underscore():
    assert is_agent_visible_sheet("Sheet1")
    assert is_agent_visible_sheet("Sales")
    assert not is_agent_visible_sheet("_foo")
    assert not is_agent_visible_sheet("__Anonymous_Sheet_DB__0")
    assert not is_agent_visible_sheet("")


def test_filter_agent_sheet_names_keeps_normal_omits_internals():
    assert filter_agent_sheet_names(
        ["Sheet1", "_foo", "__Anonymous_Sheet_DB__0", "Q1"]
    ) == ["Sheet1", "Q1"]


def _sheets_doc(*names: str) -> MagicMock:
    sheets = MagicMock()
    sheets.getElementNames.return_value = names
    doc = MagicMock()
    doc.getSheets.return_value = sheets
    return doc


def test_list_sheets_omits_leading_underscore_names():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_active_document.return_value = _sheets_doc(
            "Sheet1", "_foo", "__Anonymous_Sheet_DB__0", "Data"
        )
        result = ListSheets().execute(ctx)

    assert result["status"] == "ok"
    assert result["result"] == ["Sheet1", "Data"]
    assert "_foo" not in result["result"]
    assert "__Anonymous_Sheet_DB__0" not in result["result"]


def test_get_sheet_summary_rejects_leading_underscore_names():
    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.sheets.CalcBridge"),
        patch("plugin.calc.sheets.SheetAnalyzer") as analyzer_cls,
    ):
        for hidden in ("_foo", "__Anonymous_Sheet_DB__0"):
            result = GetSheetSummary().execute(ctx, sheet=hidden)
            assert result["status"] == "error"
            assert hidden in result.get("message", "")
        analyzer_cls.return_value.get_sheet_summary.assert_not_called()


def test_get_sheet_summary_keeps_normal_names():
    ctx = SimpleNamespace(doc=MagicMock())
    summary = {"sheet_name": "Sheet1", "used_range": "A1:B2", "headers": ["A", "B"]}
    with (
        patch("plugin.calc.sheets.CalcBridge"),
        patch("plugin.calc.sheets.SheetAnalyzer") as analyzer_cls,
    ):
        analyzer_cls.return_value.get_sheet_summary.return_value = summary
        result = GetSheetSummary().execute(ctx, sheet="Sheet1")

    assert result["status"] == "ok"
    assert result["result"]["sheet_name"] == "Sheet1"
    analyzer_cls.return_value.get_sheet_summary.assert_called_once_with(sheet_name="Sheet1")


def test_create_sheet_description_and_ok_mentions_no_cells_copied():
    from plugin.calc.sheets import CreateSheet

    desc = CreateSheet.description
    assert "no cells copied" in desc
    assert "write_formula_range" in desc
    assert "source" in desc
    # CRUD-only: stay specialized (delegation), do not promote to core.
    assert CreateSheet.tier == "specialized"

    # L12 — exact-title contract: spaces preserved, no snake_case / aliases.
    sheet_param = CreateSheet.parameters["properties"]["sheet"]["description"]
    assert "exact title the user asked for" in sheet_param
    assert "preserve spaces" in sheet_param
    assert "snake_case" in sheet_param

    ctx = SimpleNamespace(doc=MagicMock())
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_active_document.return_value.getSheets.return_value = sheets
        result = CreateSheet().execute(ctx, sheet="Sample")

    assert result["status"] == "ok"
    assert "no cells copied" in result["message"]
    sheets.insertNewByName.assert_called_once_with("Sample", 1)
