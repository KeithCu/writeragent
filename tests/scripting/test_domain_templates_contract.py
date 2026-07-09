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
        assert f'"helper": "{helper}"' in code
        assert "run_units" in code


def test_text_templates_cover_shipped_helpers():
    from plugin.scripting.text_analytics import _SHIPPED_TEMPLATES

    templates = get_text_analytics_script_templates()
    assert set(templates.keys()) == set(_SHIPPED_TEMPLATES)
    public = {h for h in TEXT_HELPERS if h not in ("diagnostics", "check")}
    assert set(templates.keys()) == public
    for helper, code in templates.items():
        assert f'"helper": "{helper}"' in code
        assert "run_text_analytics" in code


@pytest.mark.parametrize(
    "template_fn,helper_names,parse_fn",
    [
        (get_forecast_template, FORECAST_HELPERS, parse_forecast_script_header),
        (get_optimize_template, OPTIMIZE_HELPERS, parse_optimize_script_header),
        (get_quant_template, QUANT_HELPERS, parse_quant_script_header),
    ],
)
def test_per_helper_templates_cover_helpers(template_fn, helper_names, parse_fn):
    for helper in helper_names:
        code = template_fn(helper)
        assert code is not None
        meta = parse_fn(code)
        assert meta is not None
        assert meta.helper == helper
