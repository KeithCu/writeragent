# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted Pint units helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from plugin.scripting.calc_range import CalcRange
from plugin.scripting.units import (
    extract_units_insert_kwargs,
    format_units_for_calc,
    insert_units_result_into_writer,
    is_units_result,
    resolve_output_style,
    units_display_text,
)
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
    assert result["units"] == "km/h"
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


def test_extract_units_insert_kwargs():
    header = "from writeragent.scripting.units import run_units\n"
    code_detailed = (
        header
        + 'result = run_units({"helper": "convert_quantity", "params": {"value": "1", "output_style": "detailed"}}, None, {})\n'
    )
    assert extract_units_insert_kwargs(None, code_detailed) == {"output_style": "detailed"}

    code_padded = (
        header
        + 'result = run_units({"helper": "convert_quantity", "params": {"value": "1", "output_style": "  detailed  "}}, None, {})\n'
    )
    assert extract_units_insert_kwargs(None, code_padded) == {"output_style": "detailed"}

    code_blank = (
        header
        + 'result = run_units({"helper": "convert_quantity", "params": {"value": "1", "output_style": "   "}}, None, {})\n'
    )
    assert extract_units_insert_kwargs(None, code_blank) == {}

    code_none = (
        header
        + 'result = run_units({"helper": "convert_quantity", "params": {"value": "1"}}, None, {})\n'
    )
    assert extract_units_insert_kwargs(None, code_none) == {}


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

    degc_mismatch = run_units(
        {"helper": "convert_quantity", "params": {"value": "1", "from": "degC", "to": "m"}},
        None,
        {},
    )
    assert degc_mismatch["status"] == "error"
    assert degc_mismatch["code"] == "UNITS_ERROR"

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


def test_convert_overflow_after_conversion_is_rejected():
    assert convert_quantity(1e308, "m", "mm") == "#VALUE!"
    result = run_units(
        {"helper": "convert_quantity", "params": {"value": 1e308, "from": "m", "to": "mm"}},
        None,
        {},
    )
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"
    assert "finite" in result["message"]


def test_parse_rejects_non_finite_quantity():
    assert parse_quantity(quantity="nan m") == "#VALUE!"
    result = run_units({"helper": "parse_quantity", "params": {"quantity": "inf m"}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"
    assert "finite" in result["message"]


def test_run_units_missing_helper_is_missing_helper():
    res1 = run_units({}, None, {})
    assert res1["code"] == "MISSING_HELPER"
    res2 = run_units({"helper": "   "}, None, {})
    assert res2["code"] == "MISSING_HELPER"


def test_run_units_spec_fallback_ignores_non_param_keys(monkeypatch: pytest.MonkeyPatch):
    from plugin.scripting.venv import units as units_venv

    captured: dict[str, Any] = {}

    def capture(params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {"status": "ok", "helper": "parse_quantity"}

    monkeypatch.setitem(units_venv._DISPATCH, "parse_quantity", capture)
    res = run_units(
        {"helper": "parse_quantity", "quantity": "5 km/h", "params": None, "headers": True, "header_row": 1},
        None,
        {},
    )
    assert res["status"] == "ok"
    assert captured == {"quantity": "5 km/h"}


def test_check_dimensionality_alias_conflict_is_invalid_spec():
    res = run_units(
        {"helper": "check_dimensionality", "params": {"quantity_a": "10 m", "unit_a": "s", "quantity_b": "5 m"}},
        None,
        {},
    )
    assert res["status"] == "error"
    assert res["code"] == "INVALID_SPEC"

    res_ok = run_units(
        {"helper": "check_dimensionality", "params": {"quantity_a": "10 m", "unit_a": "10 m", "quantity_b": "5 m"}},
        None,
        {},
    )
    assert res_ok["status"] == "ok"
    assert res_ok["compatible"] is True


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


def test_run_units_pairwise_units_broadcast_suffixes():
    result = run_units(
        {
            "helper": "convert_quantity",
            "params": {"value": [10, 100], "from": ["m/s", "cm"], "to": ["km/h", "m"]},
        },
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["magnitudes"] == [36.0, 1.0]
    assert result["formatted"] == ["36 km/h", "1 m"]
    assert result["units"] == ["km/h", "m"]
    assert result["values"] == [[36.0, 1.0]]
    assert format_units_for_calc(result) == [[36.0, 1.0]]

    col_result = run_units(
        {
            "helper": "convert_quantity",
            "params": {"value": [[10], [100]], "from": [["m/s"], ["cm"]], "to": [["km/h"], ["m"]]},
        },
        None,
        {},
    )
    assert col_result["status"] == "ok"
    assert col_result["formatted"] == ["36 km/h", "1 m"]
    assert col_result["units"] == ["km/h", "m"]
    assert col_result["values"] == [[36.0], [1.0]]


def test_units_display_text_joins_formatted_list():
    assert units_display_text({"formatted": ["36 km/h", "72 km/h"], "text": "2 quantities converted"}) == "36 km/h, 72 km/h"
    assert units_display_text({"formatted": "36 km/h"}) == "36 km/h"
    assert units_display_text({"formatted": [], "text": "2 quantities converted"}) == "2 quantities converted"


def test_format_units_for_calc_joins_formatted_list_without_values():
    result = {"helper": "convert_quantity", "formatted": ["36 km/h", "72 km/h"]}
    assert format_units_for_calc(result) == [["36 km/h, 72 km/h"]]


def test_writer_insert_joins_vector_formatted():
    with patch("plugin.writer.format.insert_content_at_position") as mock_insert:
        insert_units_result_into_writer(
            object(),
            object(),
            {"helper": "convert_quantity", "formatted": ["36 km/h", "1 m"]},
        )
        mock_insert.assert_called_once()
        assert mock_insert.call_args[0][2] == "36 km/h, 1 m"


def test_dimensionless_format_has_no_trailing_space():
    parsed = run_units({"helper": "parse_quantity", "params": {"quantity": "42"}}, None, {})
    assert parsed["formatted"] == "42"
    assert parsed["units"] == "dimensionless"

    assert format_quantity(magnitude=42, units="dimensionless") == "42"

    formatted = run_units(
        {
            "helper": "format_quantity",
            "params": {"magnitude": 42, "units": "dimensionless", "format_spec": ".2f"},
        },
        None,
        {},
    )
    assert formatted["formatted"] == "42.00"


def test_bad_format_spec_is_invalid_spec():
    result = run_units(
        {
            "helper": "format_quantity",
            "params": {"magnitude": 3.5, "units": "m", "format_spec": "bad"},
        },
        None,
        {},
    )
    assert result["status"] == "error"
    assert result["code"] == "INVALID_SPEC"
    assert "format specifier" in result["message"]

    assert format_quantity(magnitude=3.5, units="m", format_spec="bad") == "#VALUE!"


def test_whitespace_unit_is_missing_param():
    r1 = convert_quantity(10, " ", "m")
    assert isinstance(r1, dict) and r1["code"] == "MISSING_PARAM"

    r2 = convert_quantity(10, "m", "\t")
    assert isinstance(r2, dict) and r2["code"] == "MISSING_PARAM"

    r3 = format_quantity(magnitude=1, units=" ")
    assert isinstance(r3, dict) and r3["code"] == "MISSING_PARAM"

    r_scalar = run_units(
        {"helper": "convert_quantity", "params": {"value": "10", "from": " ", "to": "m"}},
        None,
        {},
    )
    assert r_scalar["status"] == "error"
    assert r_scalar["code"] == "MISSING_PARAM"

    r_vec = run_units(
        {"helper": "convert_quantity", "params": {"value": [10, 20], "from": "m", "to": " "}},
        None,
        {},
    )
    assert r_vec["status"] == "error"
    assert r_vec["code"] == "MISSING_PARAM"
    assert "values" not in r_vec

    res_blanks = convert_quantity([10, 20], "m", ["km", " "])
    assert res_blanks[0] == pytest.approx(0.01)
    assert res_blanks[1] == ""


def test_conflicting_unit_aliases_are_invalid_spec():
    res1 = run_units(
        {"helper": "convert_quantity", "params": {"value": "10", "from": "m", "from_unit": "km", "to": "ft"}},
        None,
        {},
    )
    assert res1["status"] == "error"
    assert res1["code"] == "INVALID_SPEC"
    assert res1["message"] == "from and from_unit disagree"
    assert res1["helper"] == "convert_quantity"

    res2 = run_units(
        {"helper": "convert_quantity", "params": {"value": "10", "from": "m", "to": "km", "to_unit": "ft"}},
        None,
        {},
    )
    assert res2["status"] == "error"
    assert res2["code"] == "INVALID_SPEC"
    assert res2["message"] == "to and to_unit disagree"
    assert res2["helper"] == "convert_quantity"

    res3 = run_units(
        {
            "helper": "convert_quantity",
            "params": {"value": "10", "from": "m", "from_unit": "km", "to": "ft", "to_unit": "m"},
        },
        None,
        {},
    )
    assert res3["status"] == "error"
    assert res3["code"] == "INVALID_SPEC"
    assert res3["message"] == "from and from_unit disagree"

    res4 = convert_quantity(10, from_unit="m", **{"from": "km", "to": "ft"})
    assert isinstance(res4, dict)
    assert res4["status"] == "error"
    assert res4["code"] == "INVALID_SPEC"
    assert res4["message"] == "from and from_unit disagree"
    assert res4["helper"] == "convert_quantity"

    res5 = convert_quantity(10, "m", to_unit="km", **{"to": "ft"})
    assert isinstance(res5, dict)
    assert res5["status"] == "error"
    assert res5["code"] == "INVALID_SPEC"
    assert res5["message"] == "to and to_unit disagree"
    assert res5["helper"] == "convert_quantity"

    res6 = run_units(
        {
            "helper": "convert_quantity",
            "params": {"value": [10], "from": ["m/s"], "from_unit": ["km/h"], "to": "km/h"},
        },
        None,
        {},
    )
    assert res6["status"] == "error"
    assert res6["code"] == "INVALID_SPEC"
    assert "values" not in res6

    res7 = run_units(
        {
            "helper": "convert_quantity",
            "params": {"value": "10", "from": "m/s", "from_unit": " m/s ", "to": "km/h"},
        },
        None,
        {},
    )
    assert res7["status"] == "ok"
    assert res7["magnitude"] == pytest.approx(36.0)

    res8 = run_units(
        {
            "helper": "convert_quantity",
            "params": {"value": [10], "from": ["m/s"], "from_unit": ["m/s"], "to": "km/h"},
        },
        None,
        {},
    )
    assert res8["status"] == "ok"


@pytest.mark.parametrize("value", [None, ""])
def test_run_units_blank_parse_quantity_is_missing_param(value):
    res_rpc = run_units({"helper": "parse_quantity", "params": {"quantity": value}}, None, {})
    assert res_rpc["code"] == "MISSING_PARAM"
    assert res_rpc["message"] == "quantity is required"

    res_direct = parse_quantity(quantity=value)
    assert isinstance(res_direct, dict)
    assert res_direct["code"] == "MISSING_PARAM"
    assert res_direct["message"] == "quantity is required"

    # Lock the whitespace split:
    res_ws = run_units({"helper": "parse_quantity", "params": {"quantity": "   "}}, None, {})
    assert res_ws["code"] == "PARSE_ERROR"
    assert res_ws["message"] == "empty quantity"
    assert parse_quantity(quantity="   ") == ""

    # A blank cell inside a range stays blank:
    res_range = parse_quantity(quantity=["", "5 km/h"])
    assert res_range[0] == ""
    assert res_range[1] == pytest.approx(5.0)


def test_units_display_text_drops_blank_cells():
    assert units_display_text({"formatted": ["36 km/h", "", "72 km/h"]}) == "36 km/h, 72 km/h"
    assert units_display_text({"formatted": ["36 km/h", "  ", "72 km/h"]}) == "36 km/h, 72 km/h"
    assert units_display_text({"formatted": ["36 km/h", "#VALUE!", "72 km/h"]}) == "36 km/h, #VALUE!, 72 km/h"
    assert units_display_text({"formatted": ["", ""], "text": "2 quantities converted"}) == ""


def test_is_units_result_uses_helper_names():
    assert is_units_result({"status": "ok", "helper": "convert_quantity", "formatted": "36 km/h"}) is True
    assert is_units_result({"status": "error", "helper": "parse_quantity", "code": "PARSE_ERROR"}) is True
    assert is_units_result({"status": "error", "code": "UNITS_ERROR"}) is True
    assert is_units_result({"status": "ok", "formatted": "36 km/h", "magnitude": 36.0}) is False
    assert is_units_result({"status": "ok", "values": [[36.0]], "magnitudes": [36.0]}) is False
    assert is_units_result({"status": "error", "code": "PARSE_ERROR"}) is False
    assert is_units_result("36 km/h") is False
