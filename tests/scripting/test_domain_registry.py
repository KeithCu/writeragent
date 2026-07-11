# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for the trusted-helper domain registry."""

from __future__ import annotations

from unittest.mock import patch

from plugin.scripting.domain_registry import (
    POST_VENV_DOMAIN_ORDER,
    get_picker_domains,
    get_post_venv_domains,
    get_rps_domains,
)


def test_rps_domain_order():
    ids = [s.id for s in get_rps_domains()]
    assert ids == [
        "vision",
        "viz",
        "math",
        "units",
        "text",
        "quant",
        "optimize",
        "forecast",
        "analysis",
    ]


def test_rps_domains_have_post_venv_hooks():
    for spec in get_rps_domains():
        assert callable(spec.insert)
        assert callable(spec.format_ok)
        assert callable(spec.is_result)


def test_post_venv_order_matches_constant():
    ids = [s.id for s in get_post_venv_domains()]
    assert ids == list(POST_VENV_DOMAIN_ORDER)


def test_script_header_needs_data_binding_on_calc_domains():
    from plugin.scripting.domain_registry import script_header_needs_data_binding

    calc_doc = object()
    with patch("plugin.scripting.domain_registry.is_calc", return_value=True):
        assert script_header_needs_data_binding(
            "from writeragent.scripting.analysis import run_analysis\nresult = run_analysis(...)\n",
            doc=calc_doc,
        ) is True
        assert script_header_needs_data_binding(
            'from writeragent.scripting.forecast import run_forecast\nresult = run_forecast({"helper": "forecast_time_series", "params": {}}, data, {})\n',
            doc=calc_doc,
        ) is True
        assert script_header_needs_data_binding("# writeragent:text helper=full params={}\n", doc=calc_doc) is False


def test_picker_domains_unique_origins_and_prefixes():
    domains = get_picker_domains()
    origins = [d.origin for d in domains]
    prefixes = [d.display_prefix for d in domains]
    assert len(origins) == len(set(origins))
    assert len(prefixes) == len(set(prefixes))
    for d in domains:
        assert d.display_prefix
        assert callable(d.supports)
        assert callable(d.templates)
        assert callable(d.title_fn)


def test_picker_order_starts_with_analysis_sql_vision():
    origins = [d.origin for d in get_picker_domains()]
    assert origins[:3] == ["analysis", "sql", "vision"]
