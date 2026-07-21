# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract tests for domain helper templates and header parsers."""

from __future__ import annotations

import pytest

from plugin.scripting.analysis import HELPER_NAMES as ANALYSIS_HELPERS, get_analysis_script_templates, parse_analysis_script_header
from plugin.scripting.forecast import HELPER_NAMES as FORECAST_HELPERS, get_forecast_template, parse_forecast_script_header
from plugin.scripting.optimize import HELPER_NAMES as OPTIMIZE_HELPERS, get_optimize_template, parse_optimize_script_header
from plugin.scripting.quant import HELPER_NAMES as QUANT_HELPERS, get_quant_template, parse_quant_script_header
from plugin.scripting.text_analytics import (
    HELPER_NAMES as TEXT_HELPERS,
    get_text_analytics_script_templates,
)
from plugin.scripting.units import (
    get_units_script_templates,
)


@pytest.mark.parametrize(
    "templates_fn,helper_names,parse_fn,public_only",
    [
        (get_analysis_script_templates, ANALYSIS_HELPERS, parse_analysis_script_header, False),
    ],
)
def test_domain_templates_cover_helpers(templates_fn, helper_names, parse_fn, public_only):
    templates = templates_fn()
    expected = {h for h in helper_names if not (public_only and h in ("diagnostics", "check"))}
    assert set(templates.keys()) == expected


def test_units_templates_cover_shipped_helpers():
    from plugin.scripting.units import _SHIPPED_TEMPLATES

    templates = get_units_script_templates()
    assert set(templates.keys()) == set(_SHIPPED_TEMPLATES)
    for helper, code in templates.items():
        assert f"from writeragent.scripting.units import {helper}" in code


def test_text_templates_cover_shipped_helpers():
    from plugin.scripting.text_analytics import _SHIPPED_TEMPLATES

    templates = get_text_analytics_script_templates()
    assert set(templates.keys()) == set(_SHIPPED_TEMPLATES)
    public = {h for h in TEXT_HELPERS if h not in ("diagnostics", "check")}
    assert set(templates.keys()) == public
    for helper, code in templates.items():
        assert f"from writeragent.scripting.text_analytics import {helper}" in code


@pytest.mark.parametrize(
    "template_fn,helper_names,module_path",
    [
        (get_forecast_template, FORECAST_HELPERS, "writeragent.scripting.forecast"),
        (get_optimize_template, OPTIMIZE_HELPERS, "writeragent.scripting.optimize"),
        (get_quant_template, QUANT_HELPERS, "writeragent.scripting.quant"),
    ],
)
def test_per_helper_templates_are_executable(template_fn, helper_names, module_path):
    for helper in helper_names:
        code = template_fn(helper)
        assert code is not None
        assert f"from {module_path} import {helper}" in code
        assert f"# writeragent:" not in code.splitlines()[0]


def test_legacy_header_parsers_still_work():
    code = '# writeragent:quant helper=technical_analysis params={"indicators":["rsi"]}\n'
    meta = parse_quant_script_header(code)
    assert meta is not None
    assert meta.helper == "technical_analysis"

    code = '# writeragent:optimize helper=linear_programming params={"c_col":"c"}\n'
    meta = parse_optimize_script_header(code)
    assert meta is not None
    assert meta.helper == "linear_programming"

    code = '# writeragent:forecast helper=forecast_time_series params={"periods":6}\n'
    meta = parse_forecast_script_header(code)
    assert meta is not None
    assert meta.helper == "forecast_time_series"
