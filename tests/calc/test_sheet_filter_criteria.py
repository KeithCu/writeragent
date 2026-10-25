# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sheet-filter criterion ingest must return UnoObjectError, not a deal assertion."""

import pytest

from plugin.calc.sheet_filter_criteria import (
    _FILTER_OPERATOR2,
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
        ("EMPTY", 0),
        ("NOT_EMPTY", 1),
        ("EQUAL", 2),
        ("NOT_EQUAL", 3),
        ("GREATER", 4),
        ("GREATER_EQUAL", 5),
        ("LESS", 6),
        ("LESS_EQUAL", 7),
        ("TOP_VALUES", 8),
        ("TOP_PERCENT", 9),
        ("BOTTOM_VALUES", 10),
        ("BOTTOM_PERCENT", 11),
        ("CONTAINS", 12),
        ("DOES_NOT_CONTAIN", 13),
        ("BEGINS_WITH", 14),
        ("DOES_NOT_BEGIN_WITH", 15),
        ("ENDS_WITH", 16),
        ("DOES_NOT_END_WITH", 17),
    )
    assert _FILTER_OPERATOR2 == expected
    assert len(FILTER_OPERATOR2_LABELS) == 18
    for name, code in expected:
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
