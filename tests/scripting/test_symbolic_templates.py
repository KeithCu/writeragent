# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for symbolic Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.symbolic_templates import get_math_script_templates, parse_math_script_header


def test_get_math_script_templates_include_header():
    templates = get_math_script_templates()
    assert "solve_equation" in templates
    assert "# writeragent:math" in templates["solve_equation"]
    assert "run_symbolic" in templates["solve_equation"]


def test_parse_math_script_header_round_trip():
    code = get_math_script_templates()["integrate"]
    meta = parse_math_script_header(code)
    assert meta is not None
    assert meta.helper == "integrate"
    assert meta.params.get("expression") == "sin(x)"
