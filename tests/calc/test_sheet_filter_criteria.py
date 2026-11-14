# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sheet-filter criterion ingest must return ToolExecutionError (INVALID_PARAM), not a deal assertion."""

import pytest

from plugin.calc.sheet_filter_criteria import (
    FilterCriterion,
    filter_connection_code,
    parse_sheet_filter_criterion,
    resolve_filter_operator_code,
)
from plugin.framework.deal_shim import DEAL_MAX_TOKEN
from plugin.framework.errors import ToolExecutionError


def test_odd_operator_is_invalid_param_not_precontract() -> None:
    raw = {"field": 0, "operator": "égal" * 40, "connection": "AND", "extra": 1}
    with pytest.raises(ToolExecutionError) as exc:
        parse_sheet_filter_criterion(raw, False)
    assert exc.value.code == "INVALID_PARAM"


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
    with pytest.raises(ToolExecutionError, match="must be an object"):
        parse_sheet_filter_criterion(None, True)

    # Boolean field
    with pytest.raises(ToolExecutionError, match="field"):
        parse_sheet_filter_criterion({"field": True, "operator": "EQUAL", "value": "a"}, True)

    # Negative field
    with pytest.raises(ToolExecutionError, match="negative"):
        parse_sheet_filter_criterion({"field": -1, "operator": "EQUAL", "value": "a"}, True)

    # Non-integral float field
    with pytest.raises(ToolExecutionError, match="field"):
        parse_sheet_filter_criterion({"field": 1.5, "operator": "EQUAL", "value": "a"}, True)

    # String with dot field
    with pytest.raises(ToolExecutionError, match="field"):
        parse_sheet_filter_criterion({"field": "1.5", "operator": "EQUAL", "value": "a"}, True)

    # Non-numeric string field
    with pytest.raises(ToolExecutionError, match="field"):
        parse_sheet_filter_criterion({"field": "abc", "operator": "EQUAL", "value": "a"}, True)

    # Missing value for non-force operator
    with pytest.raises(ToolExecutionError, match=r"Operator EQUAL requires 'value'\."):
        parse_sheet_filter_criterion({"field": 0, "operator": "EQUAL", "value": None}, True)

    # List value
    with pytest.raises(ToolExecutionError, match="value"):
        parse_sheet_filter_criterion({"field": 0, "operator": "EQUAL", "value": []}, True)

    # NaN float value
    with pytest.raises(ToolExecutionError, match="NaN"):
        parse_sheet_filter_criterion({"field": 0, "operator": "GREATER", "value": float("nan")}, True)

    # Inf float value
    with pytest.raises(ToolExecutionError, match="infinity"):
        parse_sheet_filter_criterion({"field": 0, "operator": "GREATER", "value": float("inf")}, True)

    # Operator requires numeric value
    with pytest.raises(ToolExecutionError, match="TOP_VALUES"):
        parse_sheet_filter_criterion({"field": 0, "operator": "top-values"}, True)

    # Unknown operator
    with pytest.raises(ToolExecutionError, match=r"Unknown filter operator: 'no-such-op'"):
        parse_sheet_filter_criterion({"field": 0, "operator": "no-such-op", "value": "a"}, True)


@pytest.mark.verification
@pytest.mark.parametrize(
    "conn, expected",
    [("AND", 0), ("and", 0), (None, 0), ("OR", 1), ("or", 1)],
)
def test_filter_connection_code_valid(conn: str | None, expected: int) -> None:
    assert filter_connection_code(conn) == expected


@pytest.mark.verification
def test_filter_connection_code_invalid() -> None:
    with pytest.raises(ToolExecutionError) as exc:
        filter_connection_code("INVALID_CONN")
    assert exc.value.code == "INVALID_PARAM"


# Values are FilterOperator2 IDL constants; _NAME_TO_CODE is derived from label order, so a reordered label must fail here.
@pytest.mark.verification
@pytest.mark.parametrize(
    "op, expected",
    [
        ("EQUAL", 2),
        ("NOT_EQUAL", 3),
        (" not-equal ", 3),
        ("GREATER", 4),
        ("LESS", 6),
        ("CONTAINS", 12),
        ("BEGINS_WITH", 14),
    ],
)
def test_resolve_filter_operator_code_valid(op: str, expected: int) -> None:
    assert resolve_filter_operator_code(op) == expected


@pytest.mark.verification
def test_resolve_filter_operator_code_invalid() -> None:
    with pytest.raises(ToolExecutionError):
        resolve_filter_operator_code("UNKNOWN_OPERATOR_123")


@pytest.mark.verification
def test_filter_string_longer_than_token_is_invalid_param() -> None:
    """An over-long operator is ToolExecutionError (INVALID_PARAM), not PreContractError."""
    too_long = "A" * (DEAL_MAX_TOKEN + 1)
    with pytest.raises(ToolExecutionError):
        resolve_filter_operator_code(too_long)
    with pytest.raises(ToolExecutionError):
        filter_connection_code(too_long)


@pytest.mark.verification
def test_parse_sheet_filter_criterion_basic() -> None:
    raw = {"field": 0, "operator": "EQUAL", "value": "test"}
    field, op_code, conn, is_num, num_val, str_val = parse_sheet_filter_criterion(raw, is_first=True)
    assert field == 0
    assert conn == 0  # First row connection is AND (0)
    assert is_num is False
    assert str_val == "test"


@pytest.mark.verification
def test_parse_sheet_filter_criterion_bad_field_is_invalid_param() -> None:
    with pytest.raises(ToolExecutionError, match="Invalid filter 'field'"):
        parse_sheet_filter_criterion({"field": "", "operator": "EQUAL", "value": "x"}, is_first=True)
    with pytest.raises(ToolExecutionError, match="Invalid filter 'field'"):
        parse_sheet_filter_criterion({"field": None, "operator": "EQUAL", "value": "x"}, is_first=True)
