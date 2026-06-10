# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted Pint units helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

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
    assert "kilometer" in str(result["formatted"]).lower()


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
    with patch("plugin.scripting.units._require_pint", return_value=None):
        result = run_units({"helper": "convert_quantity", "params": {"value": "1", "from_unit": "m", "to_unit": "ft"}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_run_units_parse_error():
    result = run_units({"helper": "parse_quantity", "params": {"quantity": "not-a-quantity"}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"
