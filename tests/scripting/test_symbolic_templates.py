# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for symbolic Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.symbolic import (
    _DEFAULT_PARAMS,
    _HELPER_DESCRIPTIONS,
    _SHIPPED_TEMPLATES,
    get_math_script_templates,
)


def test_get_math_script_templates_include_run_call():
    templates = get_math_script_templates()
    expected_keys = {"differentiate", "integrate", "solve_equation", "symbolic_simplify"}
    assert set(templates) == expected_keys
    assert set(_SHIPPED_TEMPLATES) == expected_keys
    assert set(_DEFAULT_PARAMS) == expected_keys
    assert set(_HELPER_DESCRIPTIONS) == expected_keys
    assert "latex_to_math_object" not in _SHIPPED_TEMPLATES

    assert "solve_equation" in templates
    assert "from writeragent.scripting.symbolic import solve_equation" in templates["solve_equation"]
    assert "# writeragent:math" not in templates["solve_equation"]

    diff_code = templates["differentiate"]
    assert "from writeragent.scripting.symbolic import differentiate" in diff_code
    assert "differentiate(expression='x**2', variable='x')" in diff_code
    assert "# writeragent:math" not in diff_code


def test_math_template_body_includes_helper_params():
    code = get_math_script_templates()["integrate"]
    assert "integrate(expression='sin(x)', variable='x')" in code
    assert "from writeragent.scripting.symbolic import integrate" in code
