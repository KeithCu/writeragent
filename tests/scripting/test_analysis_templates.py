# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for built-in analysis script templates."""

from __future__ import annotations

from plugin.scripting.domain_registry import get_domain_wiring
from plugin.scripting.helper_registry import names


def test_templates_cover_all_helpers():
    templates = get_domain_wiring("analysis").templates()
    assert set(templates.keys()) == names("analysis")


def test_template_body_round_trip():
    templates = get_domain_wiring("analysis").templates()
    code = templates["describe_data"]
    assert "# writeragent:analysis" not in code
    assert "from writeragent.scripting.analysis import describe_data" in code

    assert "format_currency(data, symbol='$', decimals=2)" in templates["format_currency"]
    assert "format_percent(data, decimals=1)" in templates["format_percent"]


def test_defaults_and_descriptions_cover_all_helpers():
    from plugin.scripting.helper_registry import names, specs

    analysis_specs = specs("analysis")
    assert {s.name for s in analysis_specs} == names("analysis")
    assert {s.name for s in analysis_specs if s.description} == names("analysis")

