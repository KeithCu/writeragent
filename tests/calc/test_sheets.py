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

from plugin.app import get_tools
from plugin.framework.errors import DocumentDisposedError
from plugin.framework.tool import prepare_tool_call
from plugin.calc.bridge import filter_agent_sheet_names, is_agent_visible_sheet


def _tool(name: str):
    return get_tools().get(name)


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


def _mock_sheet(*, protected: bool = False, visible: bool = True) -> MagicMock:
    sheet = MagicMock()
    sheet.isProtected.return_value = protected
    sheet.getPropertyValue.return_value = visible
    return sheet


def _sheets_doc(*names: str) -> MagicMock:
    sheets = MagicMock()
    sheets.getElementNames.return_value = names
    by_name = {name: _mock_sheet() for name in names}
    sheets.getByName.side_effect = lambda n: by_name[n]
    doc = MagicMock()
    doc.getSheets.return_value = sheets
    return doc


def test_list_sheets_omits_leading_underscore_names():
    ctx = SimpleNamespace(
        doc=_sheets_doc("Sheet1", "_foo", "__Anonymous_Sheet_DB__0", "Data")
    )
    result = _tool("sheet_list").execute_safe(ctx)

    assert result["status"] == "ok"
    assert result["result"] == [
        {"name": "Sheet1", "protected": False, "visible": True},
        {"name": "Data", "protected": False, "visible": True},
    ]
    names = [entry["name"] for entry in result["result"]]
    assert "_foo" not in names
    assert "__Anonymous_Sheet_DB__0" not in names


def test_list_sheets_result_entries_have_name_protected_visible():
    ctx = SimpleNamespace(
        doc=_sheets_doc("Sheet1", "_foo", "__Anonymous_Sheet_DB__0", "Data")
    )
    result = _tool("sheet_list").execute_safe(ctx)
    assert result["status"] == "ok"
    for entry in result["result"]:
        assert set(entry.keys()) == {"name", "protected", "visible"}
        assert isinstance(entry["name"], str)
        assert isinstance(entry["protected"], bool)
        assert isinstance(entry["visible"], bool)


def test_get_sheet_summary_rejects_leading_underscore_names():
    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.sheets.CalcBridge"),
        patch("plugin.calc.sheets.SheetAnalyzer") as analyzer_cls,
    ):
        for hidden in ("_foo", "__Anonymous_Sheet_DB__0"):
            result = _tool("get_sheet_summary").execute_safe(ctx, sheet=hidden)
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
        result = _tool("get_sheet_summary").execute_safe(ctx, sheet="Sheet1")

    assert result["status"] == "ok"
    assert result["result"]["sheet_name"] == "Sheet1"
    analyzer_cls.return_value.get_sheet_summary.assert_called_once_with(sheet_name="Sheet1")


def test_create_sheet_description_and_ok_mentions_no_cells_copied():
    from plugin.calc.base import ToolCalcSheetBase
    from plugin.framework.prompts import SHEETS_CREATED_NOT_POPULATED_INSTRUCTION

    tool = _tool("sheet_create")
    desc = tool.description
    assert "no cells copied" in desc
    assert "write_formula_range" in desc
    assert "source" in desc
    # CRUD-only: stay specialized (delegation), do not promote to core.
    assert tool.tier == "specialized"
    sheet_param = tool.parameters["properties"]["sheet"]["description"]
    assert "Exact" in sheet_param
    assert "preserve spaces" in sheet_param
    assert "snake_case" in sheet_param
    assert ToolCalcSheetBase.specialized_domain_description is not None
    assert "Create" in ToolCalcSheetBase.specialized_domain_description

    ctx = SimpleNamespace(doc=MagicMock())
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    ctx.doc.getSheets.return_value = sheets
    result = tool.execute_safe(ctx, sheet="Q1 Actuals")

    assert result["status"] == "ok"
    assert "no cells copied" in result["message"]
    assert result["instruction"] == SHEETS_CREATED_NOT_POPULATED_INSTRUCTION
    sheets.insertNewByName.assert_called_once_with("Q1 Actuals", 1)


def test_list_sheets_disposal_is_document_disposed():
    tool = _tool("sheet_list")
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


def test_switch_sheet_missing_name_returns_uno_object_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    res = _tool("sheet_switch").execute_safe(ctx, sheet="NoSuchSheet")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "No sheet named 'NoSuchSheet'." in res["message"]
    assert "Available: Sheet1, Data" in res["message"]
    assert "sheet_list" in res["details"]["hint"]
    assert "_hidden" not in res["details"].get("similar", [])


def test_rename_sheet_missing_old_name_returns_uno_object_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    res = _tool("sheet_rename").execute_safe(ctx, old_name="NoSuchSheet", new_name="NewName")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "No sheet named 'NoSuchSheet'." in res["message"]
    assert "Available: Sheet1, Data" in res["message"]
    assert "_hidden" not in res["message"]


def test_rename_sheet_duplicate_new_name_returns_tool_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    res = _tool("sheet_rename").execute_safe(ctx, old_name="Sheet1", new_name="Data")
    assert res["status"] == "error"
    assert "already exists" in res["message"]


def test_delete_sheet_missing_name_returns_uno_object_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    res = _tool("sheet_delete").execute_safe(ctx, sheet="NoSuchSheet")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "No sheet named 'NoSuchSheet'." in res["message"]
    assert "Available: Sheet1, Data" in res["message"]
    assert "_hidden" not in res["message"]


def test_protect_sheet_missing_name_returns_uno_object_error():
    doc, _ = _make_doc_with_sheets()
    ctx = SimpleNamespace(doc=doc)
    res = _tool("sheet_protect").execute_safe(ctx, sheet="NoSuchSheet")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "No sheet named 'NoSuchSheet'." in res["message"]
    assert "Available: Sheet1, Data" in res["message"]
    assert "_hidden" not in res["message"]


def test_create_sheet_validations():
    doc, sheets = _make_doc_with_sheets()
    sheets.getCount.return_value = 3
    ctx = SimpleNamespace(doc=doc)
    tool = _tool("sheet_create")

    # Empty / whitespace
    res = tool.execute_safe(ctx, sheet="")
    assert res["status"] == "error"
    assert "cannot be empty" in res["message"]

    res = tool.execute_safe(ctx, sheet="   ")
    assert res["status"] == "error"
    assert "cannot be empty" in res["message"]

    # Invalid characters
    for ch in ("*", "?", ":", "/", "\\", "[", "]"):
        res = tool.execute_safe(ctx, sheet=f"Sheet{ch}1")
        assert res["status"] == "error"
        assert f"cannot contain '{ch}'" in res["message"]

    # Leading underscore
    res = tool.execute_safe(ctx, sheet="_hidden_new")
    assert res["status"] == "error"
    assert "reserved for internal sheets" in res["message"]

    # Case-insensitive duplicate
    res = tool.execute_safe(ctx, sheet="sheet1")
    assert res["status"] == "error"
    assert "already exists" in res["message"]

    # Position out of range
    res = tool.execute_safe(ctx, sheet="ValidName", position=-1)
    assert res["status"] == "error"
    assert "out of range" in res["message"]

    res = tool.execute_safe(ctx, sheet="ValidName", position=4)
    assert res["status"] == "error"
    assert "out of range" in res["message"]


def test_create_sheet_success_payload_and_no_activation():
    doc, sheets = _make_doc_with_sheets()
    sheets.getCount.return_value = 2
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx = SimpleNamespace(doc=doc)

    res = _tool("sheet_create").execute_safe(ctx, sheet="Budget 2026")
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
        res = _tool("sheet_rename").execute_safe(ctx, old_name="Sheet1", new_name="Sheet Renamed")
        assert res["status"] == "error"
        assert "did not rename" in res["message"]
        migrate_mock.assert_not_called()


def test_rename_sheet_empty_name_error():
    doc, _ = _make_doc_with_sheets()
    sheet = MagicMock()
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = sheet
        res = _tool("sheet_rename").execute_safe(ctx, old_name="Sheet1", new_name="")
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
        res = _tool("sheet_rename").execute_safe(ctx, old_name="Sheet1", new_name="sheet1")
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
        res = _tool("sheet_delete").execute_safe(ctx, sheet="Sheet1")
        assert res["status"] == "error"
        assert "Cannot delete the only sheet" in res["message"]
        sheets.removeByName.assert_not_called()


def test_protect_sheet_already_in_target_state():
    doc, _ = _make_doc_with_sheets()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    sheet.isProtected.return_value = True
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.sheet_or_active.return_value = sheet
        res = _tool("sheet_protect").execute_safe(ctx, sheet="Sheet1", protect=True)
        assert res["status"] == "ok"
        assert "already protected" in res["message"]
        sheet.protect.assert_not_called()

        sheet.isProtected.return_value = False
        res = _tool("sheet_protect").execute_safe(ctx, sheet="Sheet1", protect=False)
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
        bridge_cls.return_value.sheet_or_active.return_value = sheet
        res = _tool("sheet_protect").execute_safe(ctx, sheet="Sheet1", protect=False)
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
        bridge_cls.return_value.sheet_or_active.return_value = sheet
        res = _tool("sheet_protect").execute_safe(ctx, sheet="Sheet1", protect=True)
        assert res["status"] == "error"
        assert "Failed to set sheet" in res["message"]


def _doc_with_named_sheets(*names: str) -> tuple[MagicMock, MagicMock]:
    sheets = MagicMock()
    sheets.getElementNames.return_value = names
    sheets.getCount.return_value = len(names)

    def _sheet(name: str, *, visible: bool = True) -> MagicMock:
        sh = MagicMock()
        sh.getName.return_value = name
        sh.getPropertyValue.return_value = visible
        return sh

    by_name = {n: _sheet(n) for n in names}
    sheets.getByName.side_effect = lambda n: by_name[n]
    doc = MagicMock()
    doc.getSheets.return_value = sheets
    return doc, sheets


def test_sheet_copy_calls_copy_by_name_at_end():
    doc, sheets = _doc_with_named_sheets("Sheet1", "Data")
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = MagicMock()
        res = _tool("sheet_copy").execute_safe(ctx, source="Sheet1", new_name="Copy1")
    assert res["status"] == "ok"
    sheets.copyByName.assert_called_once_with("Sheet1", "Copy1", 2)


def test_sheet_copy_position_and_name_validation():
    doc, sheets = _doc_with_named_sheets("Sheet1")
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = MagicMock()
        bad = _tool("sheet_copy").execute_safe(ctx, source="Sheet1", new_name="_bad")
        assert bad["status"] == "error"
        assert "reserved" in bad["message"]
        sheets.copyByName.assert_not_called()

        out = _tool("sheet_copy").execute_safe(ctx, source="Sheet1", new_name="Q2", position=99)
        assert out["status"] == "error"
        assert "out of range" in out["message"]


def test_sheet_move_calls_move_by_name():
    doc, sheets = _doc_with_named_sheets("A", "B", "C")
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = MagicMock()
        res = _tool("sheet_move").execute_safe(ctx, sheet="B", position=0)
    assert res["status"] == "ok"
    sheets.moveByName.assert_called_once_with("B", 0)


def test_sheet_hide_last_visible_errors():
    doc, sheets = _doc_with_named_sheets("Only")
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = sheets.getByName("Only")
        res = _tool("sheet_hide").execute_safe(ctx, sheet="Only", visible=False)
    assert res["status"] == "error"
    assert "only visible" in res["message"]


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "verb"),
    [
        ("sheet_switch", {"sheet": "_hidden"}, "switch to"),
        ("sheet_rename", {"old_name": "_hidden", "new_name": "Visible"}, "rename"),
        ("sheet_delete", {"sheet": "_hidden"}, "delete"),
        ("sheet_copy", {"source": "_hidden", "new_name": "Copy"}, "copy"),
        ("sheet_move", {"sheet": "_hidden", "position": 0}, "move"),
        ("sheet_hide", {"sheet": "_hidden", "visible": False}, "hide"),
    ],
)
def test_sheet_verbs_refuse_internal_sheet_with_one_sentence(tool_name, kwargs, verb):
    doc, sheets = _doc_with_named_sheets("_hidden", "Sheet1")
    hidden_sheet = sheets.getByName("_hidden")
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = hidden_sheet
        res = _tool(tool_name).execute_safe(ctx, **kwargs)
    assert res == {
        "status": "error",
        "code": "TOOL_EXECUTION_ERROR",
        "message": f"Cannot {verb} internal sheet '_hidden'.",
    }
    sheets.removeByName.assert_not_called()
    sheets.copyByName.assert_not_called()
    sheets.moveByName.assert_not_called()
    hidden_sheet.setName.assert_not_called()
    hidden_sheet.setPropertyValue.assert_not_called()
    doc.getCurrentController().setActiveSheet.assert_not_called()


def test_insert_position_accepts_count_and_rejects_past_it():
    doc, sheets = _doc_with_named_sheets("Sheet1", "Data")
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = MagicMock()
        res_copy = _tool("sheet_copy").execute_safe(ctx, source="Sheet1", new_name="Q2", position=2)
    assert res_copy["status"] == "ok"
    sheets.copyByName.assert_called_once_with("Sheet1", "Q2", 2)

    doc3, sheets3 = _doc_with_named_sheets("Sheet1", "Data", "Third")
    ctx3 = SimpleNamespace(doc=doc3)
    res_create = _tool("sheet_create").execute_safe(ctx3, sheet="Fourth", position=3)
    assert res_create["status"] == "ok"
    sheets3.insertNewByName.assert_called_once_with("Fourth", 3)

    res_past = _tool("sheet_create").execute_safe(ctx3, sheet="Fifth", position=4)
    assert res_past["status"] == "error"
    assert res_past["message"] == "Position 4 is out of range (0..3)."


def test_sheet_move_position_bound_is_count_minus_one():
    doc, sheets = _doc_with_named_sheets("A", "B", "C")
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.calc.sheets.CalcBridge") as bridge_cls:
        bridge_cls.return_value.get_sheet.return_value = MagicMock()
        res = _tool("sheet_move").execute_safe(ctx, sheet="A", position=3)
    assert res["status"] == "error"
    assert res["message"] == "Position 3 is out of range (0..2)."
    sheets.moveByName.assert_not_called()


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("sheet_move", {"sheet": "A", "position": True}),
        ("sheet_create", {"sheet": "New", "position": "2"}),
        ("sheet_copy", {"source": "A", "new_name": "B", "position": 1.5}),
        ("sheet_hide", {"sheet": "A", "visible": "false"}),
    ],
)
def test_sheet_schema_rejects_wrong_types_before_execute(tool_name, kwargs):
    tool = _tool(tool_name)
    prep = prepare_tool_call(tool, SimpleNamespace(doc_type="calc"), tool_name, kwargs)
    assert prep.error is not None
    assert prep.error["code"] == "VALIDATION_ERROR"
