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
from plugin.calc.sheets import (
    CreateSheet,
    DeleteSheet,
    GetSheetSummary,
    ListSheets,
    ProtectSheet,
    RenameSheet,
    SwitchSheet,
)


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
    from plugin.framework.prompts import SHEETS_CREATED_NOT_POPULATED_INSTRUCTION

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
    assert result["instruction"] == SHEETS_CREATED_NOT_POPULATED_INSTRUCTION
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


def test_create_sheet_validations():
    doc, sheets = _make_doc_with_sheets()
    sheets.getCount.return_value = 3
    ctx = SimpleNamespace(doc=doc)
    tool = CreateSheet()

    # Empty / whitespace
    res = tool.execute(ctx, sheet="")
    assert res["status"] == "error"
    assert "cannot be empty" in res["message"]

    res = tool.execute(ctx, sheet="   ")
    assert res["status"] == "error"
    assert "cannot be empty" in res["message"]

    # Invalid characters
    for ch in ("*", "?", ":", "/", "\\", "[", "]"):
        res = tool.execute(ctx, sheet=f"Sheet{ch}1")
        assert res["status"] == "error"
        assert f"cannot contain '{ch}'" in res["message"]

    # Leading underscore
    res = tool.execute(ctx, sheet="_hidden_new")
    assert res["status"] == "error"
    assert "reserved for internal sheets" in res["message"]

    # Case-insensitive duplicate
    res = tool.execute(ctx, sheet="sheet1")
    assert res["status"] == "error"
    assert "already exists" in res["message"]

    # Position out of range
    res = tool.execute(ctx, sheet="ValidName", position=-1)
    assert res["status"] == "error"
    assert "out of range" in res["message"]

    res = tool.execute(ctx, sheet="ValidName", position=4)
    assert res["status"] == "error"
    assert "out of range" in res["message"]


def test_create_sheet_success_payload_and_no_activation():
    doc, sheets = _make_doc_with_sheets()
    sheets.getCount.return_value = 2
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx = SimpleNamespace(doc=doc)

    res = CreateSheet().execute(ctx, sheet="Budget 2026")
    assert res["status"] == "ok"
    assert res["sheet"] == "Budget 2026"
    assert "Not activated" in res["message"]
    controller.setActiveSheet.assert_not_called()


def test_rename_sheet_set_name_noop_errors_and_does_not_migrate():
    doc, sheets = _make_doc_with_sheets()
    sheet = MagicMock()
    # getName stays "Sheet1" despite setName
    sheet.getName.return_value = "Sheet1"
    doc.getSheets.return_value.getByName.return_value = sheet
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls, patch(
        "plugin.calc.python.sheet_modify.migrate_sheet_rename"
    ) as migrate_mock:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = RenameSheet().execute(ctx, old_name="Sheet1", new_name="Sheet Renamed")
        assert res["status"] == "error"
        assert "did not rename" in res["message"]
        migrate_mock.assert_not_called()


def test_rename_sheet_empty_name_error():
    doc, _ = _make_doc_with_sheets()
    sheet = MagicMock()
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = RenameSheet().execute(ctx, old_name="Sheet1", new_name="")
        assert res["status"] == "error"
        assert "cannot be empty" in res["message"]


def test_rename_sheet_case_only_rename_allowed():
    doc, sheets = _make_doc_with_sheets()
    sheet = MagicMock()
    sheet.getName.return_value = "sheet1"
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls, patch(
        "plugin.calc.python.sheet_modify.migrate_sheet_rename"
    ) as migrate_mock:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = RenameSheet().execute(ctx, old_name="Sheet1", new_name="sheet1")
        assert res["status"] == "ok"
        sheet.setName.assert_called_once_with("sheet1")
        migrate_mock.assert_called_once_with(doc, "Sheet1", "sheet1")


def test_delete_sheet_last_visible_sheet_guard():
    sheets = MagicMock()
    sheets.getElementNames.return_value = ("Sheet1", "__Anonymous_Sheet_DB__0")
    sheets.getCount.return_value = 2
    sheet = MagicMock()
    sheets.getByName.return_value = sheet
    doc = MagicMock()
    doc.getSheets.return_value = sheets
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = DeleteSheet().execute(ctx, sheet="Sheet1")
        assert res["status"] == "error"
        assert "Cannot delete the only sheet" in res["message"]
        sheets.removeByName.assert_not_called()


def test_delete_sheet_refuses_internal_sheet():
    sheets = MagicMock()
    sheets.getElementNames.return_value = ("Sheet1", "Sheet2", "_internal")
    sheet = MagicMock()
    sheets.getByName.return_value = sheet
    doc = MagicMock()
    doc.getSheets.return_value = sheets
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = DeleteSheet().execute(ctx, sheet="_internal")
        assert res["status"] == "error"
        assert "Cannot delete internal sheet" in res["message"]
        sheets.removeByName.assert_not_called()


def test_protect_sheet_already_in_target_state():
    doc, _ = _make_doc_with_sheets()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    sheet.isProtected.return_value = True
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = ProtectSheet().execute(ctx, sheet="Sheet1", protect=True)
        assert res["status"] == "ok"
        assert "already protected" in res["message"]
        sheet.protect.assert_not_called()

        sheet.isProtected.return_value = False
        res = ProtectSheet().execute(ctx, sheet="Sheet1", protect=False)
        assert res["status"] == "ok"
        assert "already unprotected" in res["message"]
        sheet.unprotect.assert_not_called()


def test_protect_sheet_password_protected_unprotect():
    doc, _ = _make_doc_with_sheets()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    sheet.isProtected.return_value = True
    sheet.unprotect.side_effect = Exception("IllegalArgumentException: password required")
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = ProtectSheet().execute(ctx, sheet="Sheet1", protect=False)
        assert res["status"] == "error"
        assert "is password-protected" in res["message"]


def test_protect_sheet_state_not_changed_error():
    doc, _ = _make_doc_with_sheets()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    sheet.isProtected.return_value = False
    # protect("") is called, but isProtected stays False
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = ProtectSheet().execute(ctx, sheet="Sheet1", protect=True)
        assert res["status"] == "error"
        assert "Failed to set sheet" in res["message"]

