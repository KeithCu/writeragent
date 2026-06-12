# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted Pint units helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.scripting.units import format_units_for_calc, resolve_output_style, split_helper_params
from plugin.scripting.units import run_units

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


def test_run_units_check_dimensionality_compatible():
    result = run_units(
        {
            "helper": "check_dimensionality",
            "params": {"quantity_a": "10 m/s", "quantity_b": "5 km/h"},
        },
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["compatible"] is True


def test_run_units_check_dimensionality_incompatible():
    result = run_units(
        {
            "helper": "check_dimensionality",
            "params": {"quantity_a": "10 m", "quantity_b": "5 kg"},
        },
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["compatible"] is False


def test_run_units_missing_package():
    with patch("plugin.scripting.venv.units._require_pint", return_value=None):
        result = run_units({"helper": "convert_quantity", "params": {"value": "1", "from_unit": "m", "to_unit": "ft"}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_run_units_parse_error():
    result = run_units({"helper": "parse_quantity", "params": {"quantity": "not-a-quantity"}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"
