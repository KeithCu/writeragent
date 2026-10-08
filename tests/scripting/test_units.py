# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted Pint units helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.scripting.calc_range import CalcRange
from plugin.scripting.units import format_units_for_calc, resolve_output_style, split_helper_params
from plugin.scripting.units import run_units
from plugin.scripting.venv.units import (
    convert_quantity,
    format_quantity,
    parse_quantity,
)

pytest.importorskip("pint")


def test_run_units_convert_quantity():
    result = run_units(
        {"helper": "convert_quantity", "params": {"value": "10", "from_unit": "m/s", "to_unit": "km/h"}},
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["helper"] == "convert_quantity"
    assert result["magnitude"] == pytest.approx(36.0)
    assert result["formatted"] == "36 km/h"


def test_direct_convert_quantity_scalar():
    # Direct scalar float / int
    assert convert_quantity(10, "m/s", "km/h") == pytest.approx(36.0)
    assert convert_quantity("10", "m/s", "km/h") == pytest.approx(36.0)

    # 1x1 CalcRange -> scalar float
    assert convert_quantity(CalcRange([[10]]), "m/s", "km/h") == pytest.approx(36.0)


def test_direct_convert_quantity_1d_list():
    res = convert_quantity([10, 20, 30], "m/s", "km/h")
    assert isinstance(res, list)
    assert res == pytest.approx([36.0, 72.0, 108.0])


def test_direct_convert_quantity_column_vector():
    cr = CalcRange([[10], [20], [30]])
    res = convert_quantity(cr, "m/s", "km/h")
    assert res == [[36.0], [72.0], [108.0]]


def test_direct_convert_quantity_pairwise_units():
    res = convert_quantity([10, 100], ["m/s", "cm"], ["km/h", "m"])
    assert res == [36.0, 1.0]


def test_direct_convert_quantity_blanks_and_errors():
    # Missing cells (None, "", "#N/A") become ""
    res = convert_quantity([10, None, 20, "", "#N/A", 30], "m/s", "km/h")
    assert res[0] == pytest.approx(36.0)
    assert res[1] == ""
    assert res[2] == pytest.approx(72.0)
    assert res[3] == ""
    assert res[4] == ""
    assert res[5] == pytest.approx(108.0)

    # Bad unit per-element in vector -> #VALUE! for that element
    res_err = convert_quantity([10, 20], ["m/s", "bad_unit"], "km/h")
    assert res_err[0] == pytest.approx(36.0)
    assert res_err[1] == "#VALUE!"


def test_run_units_vector_rpc_single_payload():
    result = run_units(
        {
            "helper": "convert_quantity",
            "params": {"value": [10, 20], "from_unit": "m/s", "to_unit": "km/h"},
        },
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["helper"] == "convert_quantity"
    assert result["magnitudes"] == [36.0, 72.0]
    assert result["formatted"] == ["36 km/h", "72 km/h"]
    assert result["values"] == [[36.0, 72.0]]

    # Host format_units_for_calc egress returns rectangular grid
    grid = format_units_for_calc(result)
    assert grid == [[36.0, 72.0]]


def test_direct_parse_quantity_scalar_and_vector():
    # Scalar
    assert parse_quantity(quantity="5 km/h") == pytest.approx(5.0)

    # 1D list
    res_1d = parse_quantity(quantity=["5 km/h", "10 m/s"])
    assert res_1d == [5.0, 10.0]

    # CalcRange column
    cr = CalcRange([["5 km/h"], ["10 m/s"]])
    res_col = parse_quantity(quantity=cr)
    assert res_col == [[5.0], [10.0]]


def test_direct_format_quantity_scalar_and_vector():
    # Scalar
    assert "3.5" in format_quantity(magnitude=3.5, units="m")

    # 1D list
    res = format_quantity(magnitude=[1, 2], units="m")
    assert isinstance(res, list)
    assert len(res) == 2
    assert "1" in res[0] and "2" in res[1]


def test_resolve_output_style_defaults():
    assert resolve_output_style("convert_quantity", None) == "formatted"
    assert resolve_output_style("parse_quantity", None) == "formatted"
    assert resolve_output_style("check_dimensionality", None) == "detailed"
    assert resolve_output_style("convert_quantity", "detailed") == "detailed"


def test_split_helper_params_strips_output_style():
    clean, style = split_helper_params(
        {"value": "1", "from_unit": "m", "to_unit": "ft", "output_style": "detailed"}
    )
    assert clean == {"value": "1", "from_unit": "m", "to_unit": "ft"}
    assert style == "detailed"


def test_format_units_for_calc_formatted_mode():
    grid = format_units_for_calc(
        {"status": "ok", "helper": "convert_quantity", "formatted": "36 km/h", "magnitude": 36.0},
        output_style="formatted",
    )
    assert grid == [["36 km/h"]]


def test_format_units_for_calc_detailed_mode():
    grid = format_units_for_calc(
        {
            "status": "ok",
            "helper": "check_dimensionality",
            "formatted": "compatible",
            "compatible": True,
            "dimensionality_a": "[length] / [time]",
            "dimensionality_b": "[length] / [time]",
        },
        output_style="detailed",
    )
    assert grid[0] == ["compatible"]
    assert ["Compatible", True] in grid


def test_run_units_parse_quantity():
    result = run_units({"helper": "parse_quantity", "params": {"quantity": "5 km/h"}}, None, {})
    assert result["status"] == "ok"
    assert result["magnitude"] == pytest.approx(5.0)


def test_run_units_format_quantity():
    result = run_units(
        {"helper": "format_quantity", "params": {"magnitude": "3.5", "units": "m"}},
        None,
        {},
    )
    assert result["status"] == "ok"
    assert "3.5" in result["formatted"]


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        pytest.param("10 m/s", "5 km/h", True, id="test_run_units_check_dimensionality_compatible"),
        pytest.param("10 m", "5 kg", False, id="test_run_units_check_dimensionality_incompatible"),
    ],
)
def test_run_units_check_dimensionality_compatible(value, value_2, expected):
    result = run_units(
        {
            "helper": "check_dimensionality",
            "params": {"quantity_a": value, "quantity_b": value_2},
        },
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["compatible"] is expected

def test_run_units_missing_package():
    with patch("plugin.scripting.venv.units._require_pint", return_value=None):
        result = run_units({"helper": "convert_quantity", "params": {"value": "1", "from_unit": "m", "to_unit": "ft"}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_run_units_parse_error():
    result = run_units({"helper": "parse_quantity", "params": {"quantity": "not-a-quantity"}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"
