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
import pytest

from plugin.framework.errors import DocumentDisposedError, UnoObjectError
from plugin.calc.bridge import filter_agent_sheet_names, is_agent_visible_sheet
from plugin.calc.sheets import DeleteSheet, GetSheetSummary, ListSheets, ProtectSheet, RenameSheet, SwitchSheet


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
    ctx = SimpleNamespace(
        doc=_sheets_doc("Sheet1", "_foo", "__Anonymous_Sheet_DB__0", "Data")
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
    from plugin.calc.base import ToolCalcSheetBase
    from plugin.calc.sheets import CreateSheet
    from plugin.framework.prompts import get_sheets_create_completion_instruction

    desc = CreateSheet.description
    assert "no cells copied" in desc
    assert "write_formula_range" in desc
    assert "source" in desc
    # CRUD-only: stay specialized (delegation), do not promote to core.
    assert CreateSheet.tier == "specialized"
    sheet_param = CreateSheet.parameters["properties"]["sheet"]["description"]
    assert "Exact" in sheet_param
    assert "preserve spaces" in sheet_param
    assert "snake_case" in sheet_param
    assert ToolCalcSheetBase.specialized_domain_description is not None
    assert "Create" in ToolCalcSheetBase.specialized_domain_description

    ctx = SimpleNamespace(doc=MagicMock())
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    ctx.doc.getSheets.return_value = sheets
    result = CreateSheet().execute(ctx, sheet="Q1 Actuals")

    assert result["status"] == "ok"
    assert "no cells copied" in result["message"]
    assert result["instruction"] == get_sheets_create_completion_instruction()
    sheets.insertNewByName.assert_called_once_with("Q1 Actuals", 1)


def test_list_sheets_disposal_is_document_disposed():
    tool = ListSheets()
    ctx = SimpleNamespace(doc=MagicMock())
    ctx.doc.getSheets.side_effect = DocumentDisposedError("gone")
    res = tool.execute_safe(ctx)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def _make_doc_with_sheets():
    doc = MagicMock()
    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda name: name in ("Sheet1", "Data", "_hidden")
    sheets.getElementNames.return_value = ("Sheet1", "Data", "_hidden")
    doc.getSheets.return_value = sheets
    return doc, sheets


def test_switch_sheet_missing_name_raises_uno_object_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    tool = SwitchSheet()
    with pytest.raises(UnoObjectError) as exc_info:
        tool.execute(ctx, sheet="NoSuchSheet")
    err_msg = str(exc_info.value)
    assert "No sheet named 'NoSuchSheet'." in err_msg
    assert "Available: Sheet1, Data" in err_msg
    assert "_hidden" not in err_msg

    res = tool.execute_safe(ctx, sheet="NoSuchSheet")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "No sheet named 'NoSuchSheet'." in res["message"]
    assert "Available: Sheet1, Data" in res["message"]
    assert "_hidden" not in res["message"]


def test_rename_sheet_missing_old_name_raises_uno_object_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    tool = RenameSheet()
    with pytest.raises(UnoObjectError) as exc_info:
        tool.execute(ctx, old_name="NoSuchSheet", new_name="NewName")
    err_msg = str(exc_info.value)
    assert "No sheet named 'NoSuchSheet'." in err_msg
    assert "Available: Sheet1, Data" in err_msg
    assert "_hidden" not in err_msg


def test_rename_sheet_duplicate_new_name_returns_tool_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    res = RenameSheet().execute(ctx, old_name="Sheet1", new_name="Data")
    assert res["status"] == "error"
    assert "already exists" in res["message"]


def test_delete_sheet_missing_name_raises_uno_object_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    tool = DeleteSheet()
    with pytest.raises(UnoObjectError) as exc_info:
        tool.execute(ctx, sheet="NoSuchSheet")
    err_msg = str(exc_info.value)
    assert "No sheet named 'NoSuchSheet'." in err_msg
    assert "Available: Sheet1, Data" in err_msg
    assert "_hidden" not in err_msg


def test_protect_sheet_missing_name_raises_uno_object_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    tool = ProtectSheet()
    with pytest.raises(UnoObjectError) as exc_info:
        tool.execute(ctx, sheet="NoSuchSheet")
    err_msg = str(exc_info.value)
    assert "No sheet named 'NoSuchSheet'." in err_msg
    assert "Available: Sheet1, Data" in err_msg
    assert "_hidden" not in err_msg
