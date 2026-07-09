# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for symbolic Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.symbolic import get_math_script_templates


def test_get_math_script_templates_include_run_call():
    templates = get_math_script_templates()
    assert "solve_equation" in templates
    assert "run_symbolic" in templates["solve_equation"]
    assert "# writeragent:math" not in templates["solve_equation"]


def test_math_template_body_includes_helper_params():
    code = get_math_script_templates()["integrate"]
    assert '"expression":"sin(x)"' in code
    assert '"helper": "integrate"' in code
