# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Unit tests for sheet filter disposal handling and functionality.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from plugin.app import get_tools
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError, UnoObjectError
from plugin.calc.calc_utils import range_args
from plugin.calc.sheet_filter import (
    ApplySheetFilter,
    ClearSheetFilter,
    GetSheetFilter,
    _build_filter_fields2,
    _field_to_dict,
)


class DisposedException(Exception):
    pass


class RuntimeException(Exception):
    pass


def test_apply_sheet_filter_disposal_propagates():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls:
        bridge_cls.return_value.resolve_range_or_address.side_effect = DisposedException("gone")
        tool = ApplySheetFilter()
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["A1:B2"], criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])

        result = tool.execute_safe(ctx, range=["A1:B2"], criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])
        assert result["status"] == "error"
        assert result["code"] == "DOCUMENT_DISPOSED"


def test_apply_sheet_filter_runtime_exception_reaches_execute_safe():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls:
        bridge_cls.return_value.resolve_range_or_address.side_effect = RuntimeException("no descriptor")
        tool = ApplySheetFilter()
        with pytest.raises(RuntimeException):
            tool.execute(ctx, range=["A1:B2"], criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])
        result = tool.execute_safe(ctx, range=["A1:B2"], criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])
    assert result["status"] == "error"
    assert result["code"] == "TOOL_EXECUTION_ERROR"
    assert "no descriptor" in result["message"]


def test_clear_sheet_filter_disposal_propagates():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls:
        bridge_cls.return_value.resolve_range_or_address.side_effect = DisposedException("gone")
        tool = ClearSheetFilter()
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["A1:B2"])


def test_get_sheet_filter_disposal_propagates():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls:
        bridge_cls.return_value.resolve_range_or_address.side_effect = DisposedException("gone")
        tool = GetSheetFilter()
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["A1:B2"])

        result = tool.execute_safe(ctx, range=["A1:B2"])
        assert result["status"] == "error"
        assert result["code"] == "DOCUMENT_DISPOSED"


def test_field_to_dict_disposal_propagates():
    class FieldObj:
        @property
        def Operator(self):
            raise DisposedException("gone")

    with pytest.raises(DocumentDisposedError):
        _field_to_dict(FieldObj(), 0)


def test_field_to_dict_missing_operator_omits_key():
    class FieldObj:
        @property
        def Operator(self):
            raise AttributeError("no operator")

        Field = 0
        Connection = 0
        IsNumeric = False
        StringValue = "abc"

    result = _field_to_dict(FieldObj(), 0)
    assert "operator" not in result
    assert result["index"] == 0


def test_field_to_dict_enum_connection():
    class FieldObjOr:
        Operator = 1
        Field = 0
        Connection = SimpleNamespace(value="OR")
        IsNumeric = True
        NumericValue = 42.0

    class FieldObjAnd:
        Operator = 1
        Field = 1
        Connection = SimpleNamespace(value="AND")
        IsNumeric = False
        StringValue = "x"

    result_or = _field_to_dict(FieldObjOr(), 0)
    assert result_or["connection"] == "OR"
    assert result_or["operator_code"] == 1
    assert result_or["value"] == 42.0

    result_and = _field_to_dict(FieldObjAnd(), 1)
    assert result_and["connection"] == "AND"
    assert result_and["value"] == "x"


def test_get_sheet_filter_contains_header_disposal_propagates():
    ctx = SimpleNamespace(doc=MagicMock())
    cell_range = MagicMock()
    xf = MagicMock()
    fd = MagicMock()
    xf.createFilterDescriptor.return_value = fd

    fd2 = MagicMock()
    fd2.getFilterFields2.return_value = ()

    ps = MagicMock()
    ps.getPropertyValue.side_effect = DisposedException("gone")

    def fake_query_interface(obj, iface_name):
        if iface_name == "com.sun.star.sheet.XSheetFilterable":
            return xf
        if iface_name == "com.sun.star.sheet.XSheetFilterDescriptor2":
            return fd2
        if iface_name == "com.sun.star.beans.XPropertySet":
            return ps
        return None

    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls, \
         patch("plugin.calc.sheet_filter.query_interface", side_effect=fake_query_interface):
        bridge_cls.return_value.resolve_range_or_address.return_value = cell_range
        tool = GetSheetFilter()
        with pytest.raises(DocumentDisposedError):
            tool.execute(ctx, range=["A1:B2"])


def test_range_args_exactly_one_validation():
    with pytest.raises(ToolExecutionError, match="Pass exactly one range.") as exc:
        range_args([], exactly_one=True)
    assert exc.value.code == "INVALID_PARAM"

    with pytest.raises(ToolExecutionError, match="Pass exactly one range."):
        range_args(["A1:B2", "C1:D2"], exactly_one=True)

    with pytest.raises(ToolExecutionError, match="Pass exactly one range."):
        range_args(["   "], exactly_one=True)

    assert range_args(["A1:B2"], exactly_one=True) == ["A1:B2"]


def test_contains_header_fails_loudly_when_property_set_missing():
    ctx = SimpleNamespace(doc=MagicMock())
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=2)
    xf = MagicMock()
    fd = MagicMock()
    xf.createFilterDescriptor.return_value = fd

    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls, \
         patch("plugin.calc.sheet_filter.query_interface") as qi_mock:
        bridge_cls.return_value.resolve_range_or_address.return_value = cell_range
        def fake_qi(obj, iface):
            if iface == "com.sun.star.sheet.XSheetFilterable":
                return xf
            # Simulate missing XPropertySet on fd
            if iface == "com.sun.star.beans.XPropertySet":
                return None
            if iface == "com.sun.star.sheet.XSheetFilterDescriptor2":
                return MagicMock()
            return None
        qi_mock.side_effect = fake_qi

        tool = ApplySheetFilter()
        with pytest.raises(UnoObjectError, match="Cannot set ContainsHeader"):
            tool.execute(ctx, range=["A1:C10"], has_header=True, criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])


def test_field_greater_than_or_equal_range_width():
    ctx = SimpleNamespace(doc=MagicMock())
    cell_range = MagicMock()
    # 2 columns wide (column 0 and 1)
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=1)
    xf = MagicMock()
    fd = MagicMock()
    xf.createFilterDescriptor.return_value = fd

    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls, \
         patch("plugin.calc.sheet_filter.query_interface") as qi_mock:
        bridge_cls.return_value.resolve_range_or_address.return_value = cell_range
        def fake_qi(obj, iface):
            if iface == "com.sun.star.sheet.XSheetFilterable":
                return xf
            if iface == "com.sun.star.beans.XPropertySet":
                return MagicMock()
            if iface == "com.sun.star.sheet.XSheetFilterDescriptor2":
                return MagicMock()
            return None
        qi_mock.side_effect = fake_qi

        tool = ApplySheetFilter()
        with pytest.raises(ToolExecutionError, match="out of bounds") as exc:
            tool.execute(ctx, range=["A1:B10"], criteria=[{"field": 2, "operator": "EQUAL", "value": "x"}])
        assert exc.value.code == "INVALID_PARAM"


def test_build_filter_fields2_and_readback():
    uno_mock = MagicMock()
    uno_mock.createUnoStruct.side_effect = lambda name: SimpleNamespace()

    criteria = [
        {"field": 0, "operator": "EQUAL", "value": "val"},
        {"field": 1, "operator": "GREATER", "value": 10, "connection": "OR"},
    ]
    fields = _build_filter_fields2(uno_mock, criteria, range_width=5)
    assert len(fields) == 2
    assert fields[0].Field == 0
    assert fields[0].Operator == 2
    assert fields[0].Connection == 0
    assert fields[0].StringValue == "val"

    assert fields[1].Field == 1
    assert fields[1].Operator == 4
    assert fields[1].Connection == 1
    assert fields[1].NumericValue == 10.0
    assert fields[1].IsNumeric is True

    # Readback via _field_to_dict
    d0 = _field_to_dict(fields[0], 0)
    assert d0["field"] == 0
    assert d0["operator"] == "EQUAL"

    d1 = _field_to_dict(fields[1], 1)
    assert d1["field"] == 1
    assert d1["operator"] == "GREATER"
    assert d1["value"] == 10.0


def test_apply_and_clear_sheet_filter_success():
    ctx = SimpleNamespace(doc=MagicMock())
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=3)
    xf = MagicMock()
    fd = MagicMock()
    fd2 = MagicMock()
    xf.createFilterDescriptor.return_value = fd

    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls, \
         patch("plugin.calc.sheet_filter.query_interface") as qi_mock, \
         patch("plugin.calc.sheet_filter.uno") as uno_mock:
        bridge_cls.return_value.resolve_range_or_address.return_value = cell_range
        uno_mock.createUnoStruct.side_effect = lambda name: SimpleNamespace()

        def fake_qi(obj, iface):
            if iface == "com.sun.star.sheet.XSheetFilterable":
                return xf
            if iface == "com.sun.star.beans.XPropertySet":
                return MagicMock()
            if iface == "com.sun.star.sheet.XSheetFilterDescriptor2":
                return fd2
            return None
        qi_mock.side_effect = fake_qi

        apply_tool = ApplySheetFilter()
        res_apply = apply_tool.execute(
            ctx,
            range=["A1:D10"],
            criteria=[
                {"field": 0, "operator": "EQUAL", "value": "x"},
                {"field": 1, "operator": "GREATER", "value": 5, "connection": "OR"},
            ],
        )
        assert res_apply == {"status": "ok", "range": "A1:D10", "criteria_count": 2}
        assert fd2.setFilterFields2.called
        assert xf.filter.called

        clear_tool = ClearSheetFilter()
        res_clear = clear_tool.execute(ctx, range=["A1:D10"])
        assert res_clear == {"status": "ok", "range": "A1:D10", "cleared": True}


def test_sheet_filter_registry_invalid_param_and_no_filter_call():
    ctx = SimpleNamespace(doc=MagicMock())
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=1)
    xf = MagicMock()
    fd = MagicMock()
    xf.createFilterDescriptor.return_value = fd

    with patch("plugin.calc.sheet_filter.CalcBridge") as bridge_cls, \
         patch("plugin.calc.sheet_filter.query_interface") as qi_mock:
        bridge_cls.return_value.resolve_range_or_address.return_value = cell_range
        qi_mock.side_effect = lambda obj, iface: {
            "com.sun.star.sheet.XSheetFilterable": xf,
            "com.sun.star.beans.XPropertySet": MagicMock(),
            "com.sun.star.sheet.XSheetFilterDescriptor2": MagicMock(),
        }.get(iface)
        res_apply = get_tools().get("sheet_filter_apply").execute_safe(
            ctx,
            range=["A1:B10"],
            criteria=[{"field": 5, "operator": "EQUAL", "value": "x"}],
        )
    assert res_apply["status"] == "error"
    assert res_apply["code"] == "INVALID_PARAM"
    xf.filter.assert_not_called()

    ctx_get = SimpleNamespace(doc=MagicMock())
    res_get = get_tools().get("sheet_filter_get").execute_safe(ctx_get, range=["A1:B2", "C1:D2"])
    assert res_get["status"] == "error"
    assert res_get["code"] == "INVALID_PARAM"
    ctx_get.doc.getSheets.assert_not_called()
