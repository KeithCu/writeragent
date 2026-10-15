# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Unit tests for sheet filter disposal handling.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from plugin.calc.sheet_filter import (
    ApplySheetFilter,
    ClearSheetFilter,
    GetSheetFilter,
    _field_to_dict,
)


class DisposedException(Exception):
    pass


class RuntimeException(Exception):
    pass


def test_apply_sheet_filter_disposal_propagates():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheet_filter._get_filterable_for_range", side_effect=DisposedException("gone")):
        tool = ApplySheetFilter()
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["A1:B2"], criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])

        result = tool.execute_safe(ctx, range=["A1:B2"], criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])
        assert result["status"] == "error"
        assert result["code"] == "DOCUMENT_DISPOSED"


def test_apply_sheet_filter_runtime_exception_reaches_execute_safe():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheet_filter._get_filterable_for_range", side_effect=RuntimeException("no descriptor")):
        tool = ApplySheetFilter()
        with pytest.raises(RuntimeException):
            tool.execute(ctx, range=["A1:B2"], criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])
        result = tool.execute_safe(ctx, range=["A1:B2"], criteria=[{"field": 0, "operator": "EQUAL", "value": "x"}])
    assert result["status"] == "error"
    assert result["code"] == "TOOL_EXECUTION_ERROR"
    assert "no descriptor" in result["message"]


def test_clear_sheet_filter_disposal_propagates():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheet_filter._get_filterable_for_range", side_effect=DisposedException("gone")):
        tool = ClearSheetFilter()
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["A1:B2"])


def test_get_sheet_filter_disposal_propagates():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.sheet_filter._get_filterable_for_range", side_effect=DisposedException("gone")):
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

    with pytest.raises(DisposedException):
        _field_to_dict(FieldObj(), 0, MagicMock())


def test_field_to_dict_missing_operator_omits_key():
    class FieldObj:
        @property
        def Operator(self):
            raise AttributeError("no operator")

        Field = 0
        Connection = 0
        IsNumeric = False
        StringValue = "abc"

    result = _field_to_dict(FieldObj(), 0, MagicMock())
    assert "operator" not in result
    assert result["index"] == 0


def test_get_sheet_filter_contains_header_disposal_propagates():
    ctx = SimpleNamespace(doc=MagicMock())
    xf = MagicMock()
    fd = MagicMock()
    xf.createFilterDescriptor.return_value = fd

    fd2 = MagicMock()
    fd2.getFilterFields2.return_value = ()

    ps = MagicMock()
    ps.getPropertyValue.side_effect = DisposedException("gone")

    def fake_query_interface(obj, iface_name):
        if iface_name == "com.sun.star.sheet.XSheetFilterDescriptor2":
            return fd2
        if iface_name == "com.sun.star.beans.XPropertySet":
            return ps
        return None

    with patch("plugin.calc.sheet_filter._get_filterable_for_range", return_value=(xf, object())), \
         patch("plugin.calc.sheet_filter._query_interface", side_effect=fake_query_interface):
        tool = GetSheetFilter()
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["A1:B2"])
