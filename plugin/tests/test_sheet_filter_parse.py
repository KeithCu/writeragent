# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Unit tests for Calc sheet filter JSON → UNO parsing (no LibreOffice required).

from __future__ import annotations

import pytest

from plugin.framework.calc_sheet_filter_criteria import (
    filter_connection_code,
    parse_sheet_filter_criterion,
)
from plugin.framework.errors import UnoObjectError


def test_filter_connection_code_and_or() -> None:
    assert filter_connection_code("AND") == 0
    assert filter_connection_code("and") == 0
    assert filter_connection_code(None) == 0
    assert filter_connection_code("") == 0
    assert filter_connection_code("OR") == 1
    assert filter_connection_code("or") == 1


def test_filter_connection_code_invalid() -> None:
    with pytest.raises(UnoObjectError, match="Invalid filter connection"):
        filter_connection_code("XOR")


def test_parse_criterion_first_row_ignores_connection() -> None:
    f, op, conn, is_num, num, s = parse_sheet_filter_criterion(
        {
            "field": 0,
            "operator": "EQUAL",
            "value": "a",
            "connection": "OR",
        },
        is_first=True,
    )
    assert f == 0
    assert conn == 0  # AND — OR on first row is ignored (UNO convention)
    assert is_num is False
    assert s == "a"


def test_parse_criterion_second_row_defaults_and() -> None:
    _, _, conn, _, _, _ = parse_sheet_filter_criterion(
        {"field": 1, "operator": "EQUAL", "value": "b"},
        is_first=False,
    )
    assert conn == 0


def test_parse_criterion_second_row_or() -> None:
    _, _, conn, _, _, _ = parse_sheet_filter_criterion(
        {
            "field": 1,
            "operator": "EQUAL",
            "value": "b",
            "connection": "OR",
        },
        is_first=False,
    )
    assert conn == 1


def test_parse_criterion_explicit_and_on_second_row() -> None:
    _, _, conn, _, _, _ = parse_sheet_filter_criterion(
        {
            "field": 2,
            "operator": "GREATER",
            "value": "5",
            "is_numeric": True,
            "connection": "AND",
        },
        is_first=False,
    )
    assert conn == 0
