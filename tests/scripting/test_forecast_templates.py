# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for forecast Run Python Script templates and header parsing."""

from __future__ import annotations

from plugin.scripting.forecast import (
    get_forecast_template,
    parse_forecast_script_header,
)


def test_get_forecast_template_is_executable():
    code = get_forecast_template("forecast_time_series")
    assert code is not None
    assert "run_forecast" in code
    assert "forecast_time_series" in code
    assert "# writeragent:forecast" not in code


def test_parse_forecast_script_header_round_trip():
    code = '# writeragent:forecast helper=decompose_time_series params={"date_col":"Date","value_col":"Value"}\n'
    parsed = parse_forecast_script_header(code)
    assert parsed is not None
    assert parsed.helper == "decompose_time_series"
    assert parsed.params["date_col"] == "Date"
    assert parsed.params["value_col"] == "Value"


def test_get_forecast_template_anomaly_helper():
    code = get_forecast_template("anomaly_detection_time_series")
    assert code is not None
    assert "anomaly_detection_time_series" in code
    assert "stl_residual" in code
    assert "run_forecast" in code


def test_parse_forecast_script_header_invalid():
    assert parse_forecast_script_header("# not a forecast header\n") is None
