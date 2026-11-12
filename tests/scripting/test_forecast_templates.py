# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for forecast Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.domain_registry import get_domain_wiring


def test_get_forecast_template_is_executable():
    code = get_domain_wiring("forecast").templates()["forecast_time_series"]
    assert code is not None
    assert "from writeragent.scripting.forecast import forecast_time_series" in code
    assert "forecast_time_series(data," in code
    assert "null" not in code
    assert "false" not in code
    assert "# writeragent:forecast" not in code


def test_optional_forecast_params_are_python_none():
    code = get_domain_wiring("forecast").templates()["decompose_time_series"]
    assert code is not None
    assert "period=None" in code
    assert "null" not in code


def test_get_forecast_template_anomaly_helper():
    code = get_domain_wiring("forecast").templates()["anomaly_detection_time_series"]
    assert code is not None
    assert "anomaly_detection_time_series" in code
    assert "stl_residual" in code
    assert "from writeragent.scripting.forecast import anomaly_detection_time_series" in code

