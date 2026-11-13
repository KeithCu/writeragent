# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for Calc conditional formatting tools (failure modes and error harmonization)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import uno
from com.sun.star.lang import IllegalArgumentException

from plugin.app import get_tools
from plugin.calc.conditional import _entry_to_dict
from plugin.framework.tool import prepare_tool_call


def _tool(name: str):
    return get_tools().get(name)


def test_conditional_formatting_add_validation_errors():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    cell_styles = MagicMock()
    cell_styles.hasByName.return_value = True
    doc.getStyleFamilies.return_value.getByName.return_value = cell_styles

    tool = _tool("conditional_formatting_add")

    # 1. Unknown operator rejected by schema validator
    for bad_op in ("INVALID_OP", "NONE", "greater"):
        prep = prepare_tool_call(
            tool,
            SimpleNamespace(doc_type="calc"),
            "conditional_formatting_add",
            {"range": ["A1:A10"], "operator": bad_op, "style": "Result"},
        )
        assert prep.error is not None
        assert prep.error["code"] == "VALIDATION_ERROR"

    # 2. BETWEEN missing formula2
    res_missing_f2 = tool.execute_safe(ctx, range=["A1:A10"], operator="BETWEEN", formula1="10", formula2="", style="Result")
    assert res_missing_f2["status"] == "error"
    assert res_missing_f2["code"] == "MISSING_FORMULA"

    # 3. GREATER missing formula1
    res_missing_f1 = tool.execute_safe(ctx, range=["A1:A10"], operator="GREATER", formula1="", style="Result")
    assert res_missing_f1["status"] == "error"
    assert res_missing_f1["code"] == "MISSING_FORMULA"

    # 4. Unknown style
    cell_styles.hasByName.return_value = False
    cell_styles.getElementNames.return_value = ("Standard", "Result", "Heading")
    res_bad_style = tool.execute_safe(ctx, range=["A1:A10"], operator="GREATER", formula1="10", style="Reslt")
    assert res_bad_style["status"] == "error"
    assert res_bad_style["code"] == "INVALID_STYLE"
    assert res_bad_style["message"] == "Unknown cell style 'Reslt'."
    assert res_bad_style["details"]["similar"][0] == "Result"
    assert res_bad_style["details"]["hint"] == "Call style_list with family='CellStyles' to list names."
    assert "Close matches" not in res_bad_style["message"]

    # 5. Multiple ranges
    cell_styles.hasByName.return_value = True
    res_multi = tool.execute_safe(ctx, range=["A1:A10", "B1:B10"], operator="GREATER", formula1="10", style="Result")
    assert res_multi["status"] == "error"
    assert res_multi["code"] == "UNO_OBJECT_ERROR"
    assert res_multi["message"] == "Pass exactly one range."


def test_conditional_formatting_remove_error_handling():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    bridge = MagicMock()
    cell_range = MagicMock()
    formats = MagicMock()
    formats.getCount.return_value = 0
    cell_range.getPropertyValue.return_value = formats
    bridge.resolve_range_or_address.return_value = cell_range

    tool = _tool("conditional_formatting_remove")

    with patch("plugin.calc.conditional.CalcBridge", return_value=bridge):
        # 1. Remove by index when no formats exist
        res_no_formats = tool.execute_safe(ctx, range=["A1:A10"], rule_index=0)
        assert res_no_formats["status"] == "error"
        assert "No conditional formats" in res_no_formats["message"]

        # 2. Out of range index
        formats.getCount.return_value = 2
        res_out_of_range = tool.execute_safe(ctx, range=["A1:A10"], rule_index=5)
        assert res_out_of_range["status"] == "error"
        assert "Rule index 5 not found" in res_out_of_range["message"]


def test_conditional_formatting_remove_refuses_two_ranges():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc
    formats = MagicMock()
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value.getPropertyValue.return_value = formats

    with patch("plugin.calc.conditional.CalcBridge", return_value=bridge):
        res = _tool("conditional_formatting_remove").execute_safe(ctx, range=["A1:A10", "B1:B10"])
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert res["message"] == "Pass exactly one range."
    formats.removeByIndex.assert_not_called()
    formats.clear.assert_not_called()


def test_conditional_formatting_list_error_handling():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    bridge = MagicMock()
    bridge.resolve_range_or_address.side_effect = IllegalArgumentException("Failed to resolve range")

    tool = _tool("conditional_formatting_list")

    with patch("plugin.calc.conditional.CalcBridge", return_value=bridge):
        res = tool.execute_safe(ctx, range=["A1:A10"])
        assert res["status"] == "error"
        assert res["code"] == "CONDITIONAL_FORMAT_ERROR"
        assert "Failed to list conditional formats" in res["message"]
        bridge.get_active_sheet.assert_not_called()


def test_conditional_formatting_add_operator_path():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    cell_styles = MagicMock()
    cell_styles.hasByName.return_value = True
    doc.getStyleFamilies.return_value.getByName.return_value = cell_styles

    bridge = MagicMock()
    cell_range = MagicMock()
    ra = SimpleNamespace(Sheet=0, StartColumn=2, StartRow=4, EndColumn=2, EndRow=8)
    cell_range.getRangeAddress.return_value = ra
    formats = MagicMock()
    formats.getCount.return_value = 1
    cell_range.getPropertyValue.return_value = formats
    bridge.resolve_range_or_address.return_value = cell_range

    tool = _tool("conditional_formatting_add")

    with (
        patch("plugin.calc.conditional.CalcBridge", return_value=bridge),
        patch.object(uno, "createUnoStruct", side_effect=lambda *a, **k: SimpleNamespace()),
    ):
        # 1. Standard operator -> int code and SourcePosition top-left
        res_gt = tool.execute_safe(ctx, range=["C5:C9"], operator="GREATER", formula1="5", style="Result")
        assert res_gt["status"] == "ok"
        added_props = formats.addNew.call_args[0][0]
        prop_dict = {p.Name: p.Value for p in added_props}
        assert prop_dict["Operator"] == 3
        assert prop_dict["Formula1"] == "5"
        assert prop_dict["StyleName"] == "Result"
        assert prop_dict["SourcePosition"].Sheet == 0
        assert prop_dict["SourcePosition"].Column == 2
        assert prop_dict["SourcePosition"].Row == 4

        # 2. DUPLICATE operator -> int 10 (bare string wrapped by prepare_tool_call)
        prep = prepare_tool_call(
            tool,
            SimpleNamespace(doc_type="calc"),
            "conditional_formatting_add",
            {"range": "C5:C9", "operator": "DUPLICATE", "style": "Result"},
        )
        assert prep.error is None
        assert prep.kwargs["range"] == ["C5:C9"]
        res_dup = tool.execute_safe(ctx, **prep.kwargs)
        assert res_dup["status"] == "ok"
        added_props_dup = formats.addNew.call_args[0][0]
        prop_dict_dup = {p.Name: p.Value for p in added_props_dup}
        assert prop_dict_dup["Operator"] == 10

        # 3. FORMULA -> int 9
        res_form = tool.execute_safe(ctx, range=["C5:C9"], operator="FORMULA", formula1="C5>10", style="Result")
        assert res_form["status"] == "ok"
        prop_dict_form = {p.Name: p.Value for p in formats.addNew.call_args[0][0]}
        assert prop_dict_form["Operator"] == 9

        # 4. NOT_BETWEEN -> int 8
        res_nb = tool.execute_safe(ctx, range=["C5:C9"], operator="NOT_BETWEEN", formula1="1", formula2="10", style="Result")
        assert res_nb["status"] == "ok"
        prop_dict_nb = {p.Name: p.Value for p in formats.addNew.call_args[0][0]}
        assert prop_dict_nb["Operator"] == 8

        # 5. NOT_DUPLICATE -> int 11
        res_nd = tool.execute_safe(ctx, range=["C5:C9"], operator="NOT_DUPLICATE", style="Result")
        assert res_nd["status"] == "ok"
        prop_dict_nd = {p.Name: p.Value for p in formats.addNew.call_args[0][0]}
        assert prop_dict_nd["Operator"] == 11


def test_entry_to_dict():
    entry = MagicMock()
    entry.queryInterface.return_value = None
    op = MagicMock()
    op.value = "GREATER"
    entry.getOperator.return_value = op
    entry.getFormula1.return_value = "5"
    entry.getFormula2.return_value = "0"
    entry.getStyleName.return_value = "Result"

    d = _entry_to_dict(entry, 0)
    assert d == {
        "index": 0,
        "operator": "GREATER",
        "formula1": "5",
        "style": "Result",
    }

    # BETWEEN keeps formula2 even when "0"
    entry_between = MagicMock()
    entry_between.queryInterface.return_value = None
    op_b = MagicMock()
    op_b.value = "BETWEEN"
    entry_between.getOperator.return_value = op_b
    entry_between.getFormula1.return_value = "-5"
    entry_between.getFormula2.return_value = "0"
    entry_between.getStyleName.return_value = "Result"
    d_b = _entry_to_dict(entry_between, 1)
    assert d_b["formula2"] == "0"


def test_conditional_formatting_list_used_area_and_range_handling():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    bridge = MagicMock()
    sheet = MagicMock()
    cursor = MagicMock()
    addr = SimpleNamespace(StartColumn=0, StartRow=0, EndColumn=3, EndRow=9)
    cursor.getRangeAddress.return_value = addr
    formats = MagicMock()
    formats.getCount.return_value = 0
    cursor.getPropertyValue.return_value = formats
    sheet.createCursor.return_value = cursor
    bridge.get_active_sheet.return_value = sheet

    tool = _tool("conditional_formatting_list")

    with patch("plugin.calc.conditional.CalcBridge", return_value=bridge):
        # Bare string works via prepare_tool_call wrapping
        cell_range = MagicMock()
        cell_range.getPropertyValue.return_value = formats
        bridge.resolve_range_or_address.return_value = cell_range
        prep = prepare_tool_call(
            tool,
            SimpleNamespace(doc_type="calc"),
            "conditional_formatting_list",
            {"range": "A1:D10"},
        )
        assert prep.error is None
        assert prep.kwargs["range"] == ["A1:D10"]
        res_str = tool.execute_safe(ctx, **prep.kwargs)
        assert res_str["status"] == "ok"
        assert res_str["range"] == "A1:D10"

        # Two ranges -> error, and resolve_range_or_address not called
        bridge.resolve_range_or_address.reset_mock()
        res_multi = tool.execute_safe(ctx, range=["A1:D10", "E1:H10"])
        assert res_multi["status"] == "error"
        assert res_multi["code"] == "UNO_OBJECT_ERROR"
        assert res_multi["message"] == "Pass exactly one range."
        bridge.resolve_range_or_address.assert_not_called()

        # Blank item -> error
        res_blank = tool.execute_safe(ctx, range=["  "])
        assert res_blank["status"] == "error"
        assert res_blank["code"] == "UNO_OBJECT_ERROR"
        assert res_blank["message"] == "Pass exactly one range."

        # Omitted or empty list -> scans used area and returns resolved address
        res_none = tool.execute_safe(ctx)
        assert res_none["status"] == "ok"
        assert res_none["range"] == "A1:D10"

        res_empty = tool.execute_safe(ctx, range=[])
        assert res_empty["status"] == "ok"
        assert res_empty["range"] == "A1:D10"


def test_conditional_sub_agent_includes_style_list_not_style_create():
    from plugin.framework.tool import ToolRegistry
    from plugin.writer.styles import StyleCreate, StyleList

    registry = ToolRegistry(MagicMock())
    registry.register(_tool("conditional_formatting_add"))
    registry.register(StyleList())
    registry.register(StyleCreate())
    names = {t.name for t in registry.get_tools(doc_type="calc", active_domain="conditional_formatting", exclude_tiers=())}
    assert "style_list" in names
    assert "style_create" not in names


def test_get_image_supports_calc_document():
    from plugin.framework.doc_type import CALC_DOCUMENT_SERVICE
    from plugin.framework.tool import tool_supports_document
    from plugin.writer.get_image import GetImage

    assert tool_supports_document(
        GetImage(),
        doc_type="calc",
        uno_services_supported=frozenset({CALC_DOCUMENT_SERVICE}),
    ) is True


def test_calc_specialized_catalog_excludes_styles_domain():
    from plugin.doc.specialized_catalog import get_specialized_domain_catalog

    domains = {entry["domain"] for entry in get_specialized_domain_catalog(agent_label="Calc")}
    assert "styles" not in domains

