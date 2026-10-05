# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for Calc conditional formatting tools (failure modes and error harmonization)."""

from unittest.mock import MagicMock, patch

from plugin.calc.conditional import (
    AddConditionalFormat,
    ListConditionalFormats,
    RemoveConditionalFormats,
    condition_operator_code_to_name,
)


def test_condition_operator_code_to_name():
    assert condition_operator_code_to_name(0) == "NONE"
    assert condition_operator_code_to_name(1) == "EQUAL"
    assert condition_operator_code_to_name(7) == "BETWEEN"
    assert condition_operator_code_to_name(10) == "DUPLICATE"
    assert condition_operator_code_to_name(99) == "99"


def test_add_conditional_format_validation_errors():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    tool = AddConditionalFormat()

    # 1. Unknown operator
    res_bad_op = tool.execute(ctx, range=["A1:A10"], operator="INVALID_OP", style="Result")
    assert res_bad_op["status"] == "error"
    assert res_bad_op["code"] == "INVALID_OPERATOR"

    # 2. BETWEEN missing formula2
    res_missing_f2 = tool.execute(ctx, range=["A1:A10"], operator="BETWEEN", formula1="10", formula2="", style="Result")
    assert res_missing_f2["status"] == "error"
    assert res_missing_f2["code"] == "MISSING_FORMULA"

    # 3. GREATER missing formula1
    res_missing_f1 = tool.execute(ctx, range=["A1:A10"], operator="GREATER", formula1="", style="Result")
    assert res_missing_f1["status"] == "error"
    assert res_missing_f1["code"] == "MISSING_FORMULA"


def test_remove_conditional_formats_error_handling():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    bridge = MagicMock()
    cell_range = MagicMock()
    formats = MagicMock()
    formats.getCount.return_value = 0
    cell_range.getPropertyValue.return_value = formats
    bridge.resolve_range_or_address.return_value = cell_range

    tool = RemoveConditionalFormats()

    with patch("plugin.calc.conditional.CalcBridge", return_value=bridge):
        # 1. Remove by index when no formats exist
        res_no_formats = tool.execute(ctx, range=["A1:A10"], rule_index=0)
        assert res_no_formats["status"] == "error"
        assert "No conditional formats" in res_no_formats["message"]

        # 2. Out of range index
        formats.getCount.return_value = 2
        res_out_of_range = tool.execute(ctx, range=["A1:A10"], rule_index=5)
        assert res_out_of_range["status"] == "error"
        assert "Rule index 5 not found" in res_out_of_range["message"]


def test_list_conditional_formats_error_handling():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    bridge = MagicMock()
    bridge.get_active_sheet.side_effect = Exception("Failed to get sheet")

    tool = ListConditionalFormats()

    with patch("plugin.calc.conditional.CalcBridge", return_value=bridge):
        res = tool.execute(ctx, range=["A1:A10"])
        assert res["status"] == "error"
        assert res["code"] == "CONDITIONAL_FORMAT_ERROR"
        assert "Failed to list conditional formats" in res["message"]


def test_add_conditional_format_operator_path():
    import types
    import uno

    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    bridge = MagicMock()
    cell_range = MagicMock()
    formats = MagicMock()
    formats.getCount.return_value = 1
    cell_range.getPropertyValue.return_value = formats
    bridge.resolve_range_or_address.return_value = cell_range

    tool = AddConditionalFormat()

    with patch("plugin.calc.conditional.CalcBridge", return_value=bridge):
        with patch.object(uno, "createUnoStruct", side_effect=lambda *a, **k: types.SimpleNamespace()):
            with patch.object(uno, "Enum", side_effect=lambda t, v: types.SimpleNamespace(typeName=t, value=v)):
                # 1. Standard operator -> uno.Enum
                res_gt = tool.execute(ctx, range=["A1:A10"], operator="GREATER", formula1="5", style="Result")
                assert res_gt["status"] == "ok"
                added_props = formats.addNew.call_args[0][0]
                prop_dict = {p.Name: p.Value for p in added_props}
                assert prop_dict["Operator"].typeName == "com.sun.star.sheet.ConditionOperator"
                assert prop_dict["Operator"].value == "GREATER"
                assert prop_dict["Formula1"] == "5"
                assert prop_dict["StyleName"] == "Result"

            # 2. DUPLICATE operator -> integer 10
            res_dup = tool.execute(ctx, range=["A1:A10"], operator="DUPLICATE", style="Result")
            assert res_dup["status"] == "ok"
            added_props_dup = formats.addNew.call_args[0][0]
            prop_dict_dup = {p.Name: p.Value for p in added_props_dup}
            assert prop_dict_dup["Operator"] == 10


def test_entry_to_dict_logs_exceptions(caplog):
    import logging
    from plugin.calc.conditional import _entry_to_dict

    entry = MagicMock()
    entry.queryInterface.side_effect = RuntimeError("boom qi")
    entry.getOperator.side_effect = RuntimeError("boom op")
    entry.getFormula1.side_effect = RuntimeError("boom f1")
    entry.getFormula2.side_effect = RuntimeError("boom f2")
    entry.getStyleName.side_effect = RuntimeError("boom sn")

    with caplog.at_level(logging.DEBUG, logger="writeragent.calc"):
        d = _entry_to_dict(entry, 0)
    assert d == {"index": 0}
    assert any("Failed getting operator" in record.message for record in caplog.records)

