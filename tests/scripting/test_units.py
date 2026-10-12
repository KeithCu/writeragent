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
    assert result["units"] == "kilometer / hour"


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
    # Compact unit text. The long name used to be "3.5 meter".
    assert format_quantity(magnitude=3.5, units="m") == "3.5 m"
    assert format_quantity(magnitude=0, units="m") == "0 m"
    # Sheet blanks stay empty. map_over_range skips them before formatting.
    assert format_quantity(magnitude="", units="m") == ""
    assert format_quantity(magnitude=None, units="m") == ""

    # 1D list
    res = format_quantity(magnitude=[1, 2], units="m")
    assert res == ["1 m", "2 m"]


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
    # Pint's compact form inserts spaces. convert_quantity keeps the caller's "km/h".
    assert result["formatted"] == "5 km / h"
    assert result["units"] == "kilometer / hour"


def test_run_units_format_quantity():
    result = run_units(
        {"helper": "format_quantity", "params": {"magnitude": "3.5", "units": "m"}},
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["formatted"] == "3.5 m"
    assert result["units"] == "meter"


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


@pytest.mark.parametrize("value", ["", None])
def test_run_units_blank_value_is_parse_error(value):
    # Empty used to become a successful 0 km/h via `float(text or "0")`.
    result = run_units(
        {"helper": "convert_quantity", "params": {"value": value, "from": "m/s", "to": "km/h"}},
        None,
        {},
    )
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"
    assert result["message"] == "empty value"
    # Direct sheet calls still leave a blank cell blank.
    assert convert_quantity(value, "m/s", "km/h") == ""


def test_run_units_zero_magnitude_is_zero():
    result = run_units(
        {"helper": "convert_quantity", "params": {"value": 0, "from": "m/s", "to": "km/h"}},
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["magnitude"] == pytest.approx(0.0)
    assert result["formatted"] == "0 km/h"


def test_run_units_blank_format_magnitude_is_parse_error():
    result = run_units(
        {"helper": "format_quantity", "params": {"magnitude": "", "units": "m"}},
        None,
        {},
    )
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"
    assert result["message"] == "empty magnitude"


def test_unknown_unit_is_parse_error_and_mismatch_is_units_error():
    # UndefinedUnitError subclasses AttributeError, so it used to be UNITS_ERROR.
    bad = run_units(
        {"helper": "convert_quantity", "params": {"value": "10", "from": "m", "to": "not_a_unit"}},
        None,
        {},
    )
    assert bad["status"] == "error"
    assert bad["code"] == "PARSE_ERROR"

    mismatch = run_units(
        {"helper": "convert_quantity", "params": {"value": "10", "from": "m", "to": "kg"}},
        None,
        {},
    )
    assert mismatch["status"] == "error"
    assert mismatch["code"] == "UNITS_ERROR"

    # A shape mismatch must stay UNITS_ERROR. It is not a quantity parse failure.
    shape = run_units(
        {"helper": "convert_quantity", "params": {"value": [1, 2], "from": ["m", "cm", "km"], "to": "m"}},
        None,
        {},
    )
    assert shape["status"] == "error"
    assert shape["code"] == "UNITS_ERROR"
    assert "mismatch" in shape["message"]


def test_run_units_rejects_non_dict_params():
    result = run_units({"helper": "convert_quantity", "params": ["nope"]}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "INVALID_SPEC"
    assert "dict" in result["message"]


def test_convert_quantity_none_unit_is_missing_param():
    # None used to pass `!= ""` and come back as #VALUE!.
    result = convert_quantity(10, None, "m")
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PARAM"


def test_convert_quantity_from_alias_and_power_units():
    assert convert_quantity(10, **{"from": "m/s", "to": "km/h"}) == pytest.approx(36.0)
    assert convert_quantity(1, "km/s**2", "m/s**2") == pytest.approx(1000.0)
    assert convert_quantity(60, "1/s", "1/min") == pytest.approx(3600.0)


def test_convert_quantity_numpy_values_and_nonfinite():
    np = pytest.importorskip("numpy")
    assert convert_quantity(np.float64(10), "m/s", "km/h") == pytest.approx(36.0)
    assert convert_quantity([10, 100], np.array(["m/s", "cm"]), np.array(["km/h", "m"])) == pytest.approx([36.0, 1.0])
    # NaN is a blank cell on the direct path. inf is not, and must not become "inf km".
    assert convert_quantity(float("nan"), "m", "km") == ""
    assert convert_quantity(float("inf"), "m", "km") == "#VALUE!"
    nan_rpc = run_units(
        {"helper": "convert_quantity", "params": {"value": float("nan"), "from": "m", "to": "km"}},
        None,
        {},
    )
    assert nan_rpc["status"] == "error"
    assert nan_rpc["code"] == "PARSE_ERROR"
    assert "finite" in nan_rpc["message"]


def test_run_units_strips_output_style_and_copies_task_hint():
    result = run_units(
        {
            "helper": "convert_quantity",
            "params": {"value": "10", "from": "m/s", "to": "km/h", "output_style": "detailed"},
        },
        None,
        {"task_hint": "lab"},
    )
    assert result["status"] == "ok"
    assert result["formatted"] == "36 km/h"
    assert "output_style" not in result
    assert result["task_hint"] == "lab"


def test_dispatch_matches_helper_names():
    from plugin.scripting.calc_functions_common import UNITS_HELPER_NAMES
    from plugin.scripting.venv.units import _DISPATCH

    assert set(_DISPATCH) == set(UNITS_HELPER_NAMES)
