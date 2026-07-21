# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for units Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.units import get_units_script_templates


def test_get_units_script_templates_include_run_call():
    templates = get_units_script_templates()
    assert "convert_quantity" in templates
    assert 'convert_quantity(10, "m/s", "km/h")' in templates["convert_quantity"]
    assert "from writeragent.scripting.units import convert_quantity" in templates["convert_quantity"]
    assert "# writeragent:units" not in templates["convert_quantity"]


def test_units_template_body_includes_helper_params():
    code = get_units_script_templates()["parse_quantity"]
    assert 'parse_quantity("10 m/s")' in code
    assert "from writeragent.scripting.units import parse_quantity" in code
