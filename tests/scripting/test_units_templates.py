# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for units Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.units import get_units_script_templates, parse_units_script_header


def test_get_units_script_templates_include_header():
    templates = get_units_script_templates()
    assert "convert_quantity" in templates
    assert "# writeragent:units" in templates["convert_quantity"]
    assert "run_units" in templates["convert_quantity"]


def test_parse_units_script_header_round_trip():
    code = get_units_script_templates()["parse_quantity"]
    meta = parse_units_script_header(code)
    assert meta is not None
    assert meta.helper == "parse_quantity"
    assert meta.params.get("quantity") == "10 m/s"
