from __future__ import annotations
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for symbolic Run Python Script templates."""


from plugin.scripting.helper_registry import specs
from plugin.scripting.symbolic import (
    get_math_script_templates,
)


def test_get_math_script_templates_include_run_call():
    templates = get_math_script_templates()
    sym_specs = specs("symbolic")
    expected_keys = {"differentiate", "integrate", "solve_equation", "symbolic_simplify"}
    assert set(templates) == expected_keys
    assert {s.name for s in sym_specs if s.shipped_template} == expected_keys
    assert {s.name for s in sym_specs if s.description and s.shipped_template} == expected_keys
    assert "latex_to_math_object" not in templates

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
