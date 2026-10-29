# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sheet-filter criterion ingest must return UnoObjectError, not a deal assertion."""

import pytest

from plugin.calc.sheet_filter_criteria import (
    FILTER_OPERATOR2_LABELS,
    FilterCriterion,
    parse_sheet_filter_criterion,
    resolve_filter_operator_code,
)
from plugin.framework.errors import UnoObjectError


def test_odd_operator_is_uno_error_not_precontract() -> None:
    raw = {"field": 0, "operator": "égal" * 40, "connection": "AND", "extra": 1}
    with pytest.raises(UnoObjectError):
        parse_sheet_filter_criterion(raw, False)


def test_pin_all_18_filter_operator2_codes() -> None:
    expected = (
        "EMPTY",
        "NOT_EMPTY",
        "EQUAL",
        "NOT_EQUAL",
        "GREATER",
        "GREATER_EQUAL",
        "LESS",
        "LESS_EQUAL",
        "TOP_VALUES",
        "TOP_PERCENT",
        "BOTTOM_VALUES",
        "BOTTOM_PERCENT",
        "CONTAINS",
        "DOES_NOT_CONTAIN",
        "BEGINS_WITH",
        "DOES_NOT_BEGIN_WITH",
        "ENDS_WITH",
        "DOES_NOT_END_WITH",
    )
    assert FILTER_OPERATOR2_LABELS == expected
    assert len(FILTER_OPERATOR2_LABELS) == 18
    for code, name in enumerate(expected):
        assert resolve_filter_operator_code(name) == code


def test_criterion_named_tuple_and_valid_cases() -> None:
    res = parse_sheet_filter_criterion({"field": 0, "operator": "EQUAL", "value": "hello"}, True)
    assert isinstance(res, FilterCriterion)
    assert res.field == 0
    assert res.operator == 2
    assert res.connection == 0
    assert res.is_numeric is False
    assert res.string_value == "hello"

    # Number with text operator automatically sets is_numeric
    res_num = parse_sheet_filter_criterion({"field": 1, "operator": "EQUAL", "value": 42}, False)
    assert res_num.is_numeric is True
    assert res_num.numeric_value == 42.0

    # Connection handling (first row ignores connection, later rows default AND or parse OR)
    assert parse_sheet_filter_criterion({"field": 0, "operator": "EQUAL", "value": "a", "connection": "OR"}, True).connection == 0
    assert parse_sheet_filter_criterion({"field": 0, "operator": "EQUAL", "value": "a", "connection": "XOR"}, True).connection == 0
    assert parse_sheet_filter_criterion({"field": 1, "operator": "EQUAL", "value": "b"}, False).connection == 0
    assert parse_sheet_filter_criterion({"field": 1, "operator": "EQUAL", "value": "b", "connection": ""}, False).connection == 0
    assert parse_sheet_filter_criterion({"field": 1, "operator": "EQUAL", "value": "b", "connection": "or"}, False).connection == 1

    # Empty operator parsing
    res_empty = parse_sheet_filter_criterion({"field": 0, "operator": "empty"}, True)
    assert res_empty.operator == 0
    assert res_empty.is_numeric is False
    assert res_empty.string_value == ""


def test_criterion_strict_parsing_rejections() -> None:
    # Non-dict (None)
    with pytest.raises(UnoObjectError, match="must be an object"):
        parse_sheet_filter_criterion(None, True)

    # Boolean field
    with pytest.raises(UnoObjectError, match="field"):
        parse_sheet_filter_criterion({"field": True, "operator": "EQUAL", "value": "a"}, True)

    # Negative field
    with pytest.raises(UnoObjectError, match="negative"):
        parse_sheet_filter_criterion({"field": -1, "operator": "EQUAL", "value": "a"}, True)

    # Non-integral float field
    with pytest.raises(UnoObjectError, match="field"):
        parse_sheet_filter_criterion({"field": 1.5, "operator": "EQUAL", "value": "a"}, True)

    # List value
    with pytest.raises(UnoObjectError, match="value"):
        parse_sheet_filter_criterion({"field": 0, "operator": "EQUAL", "value": []}, True)

    # NaN float value
    with pytest.raises(UnoObjectError, match="NaN"):
        parse_sheet_filter_criterion({"field": 0, "operator": "GREATER", "value": float("nan")}, True)

    # Inf float value
    with pytest.raises(UnoObjectError, match="infinity"):
        parse_sheet_filter_criterion({"field": 0, "operator": "GREATER", "value": float("inf")}, True)

    # Operator requires numeric value
    with pytest.raises(UnoObjectError, match="TOP_VALUES"):
        parse_sheet_filter_criterion({"field": 0, "operator": "top-values"}, True)

    # Unknown operator
    with pytest.raises(UnoObjectError, match=r"Unknown filter operator: 'no-such-op'"):
        parse_sheet_filter_criterion({"field": 0, "operator": "no-such-op", "value": "a"}, True)
