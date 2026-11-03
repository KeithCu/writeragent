# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for units Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.helper_registry import names, specs
from plugin.scripting.units import get_units_script_templates


def test_get_units_script_templates_include_run_call():
    templates = get_units_script_templates()
    assert "convert_quantity" in templates
    assert "convert_quantity(data, 'm/s', 'km/h')" in templates["convert_quantity"]
    assert "from writeragent.scripting.units import convert_quantity" in templates["convert_quantity"]
    assert "# writeragent:units" not in templates["convert_quantity"]


def test_units_template_body_includes_helper_params():
    code = get_units_script_templates()["parse_quantity"]
    assert "parse_quantity(quantity=data)" in code
    assert "from writeragent.scripting.units import parse_quantity" in code


def test_units_template_sets_match_helpers():
    unit_specs = {s.name: s for s in specs("units")}
    unit_names = names("units")
    templates = get_units_script_templates()
    shipped = {s.name for s in unit_specs.values() if s.shipped_template}
    assert set(templates) == shipped <= unit_names
    assert {s.name for s in unit_specs.values() if s.description} == unit_names
    assert "format_quantity" in unit_names
    assert "format_quantity" not in shipped
