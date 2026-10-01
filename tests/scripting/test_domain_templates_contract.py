# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract tests for domain helper templates and header parsers."""

from __future__ import annotations

import ast
import importlib
import inspect

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
        assert f"from writeragent.scripting.units import {helper}" in code


def test_text_templates_cover_shipped_helpers():
    from plugin.scripting.text_analytics import _SHIPPED_TEMPLATES

    templates = get_text_analytics_script_templates()
    assert set(templates.keys()) == set(_SHIPPED_TEMPLATES)
    public = {h for h in TEXT_HELPERS if h not in ("diagnostics", "check")}
    assert set(templates.keys()) == public
    for helper, code in templates.items():
        assert "from writeragent.scripting.text_analytics import run_text_analytics" in code
        assert f'"helper": "{helper}"' in code or f"'helper': '{helper}'" in code


@pytest.mark.parametrize(
    "template_fn,helper_names,module_path,import_name",
    [
        (get_forecast_template, FORECAST_HELPERS, "writeragent.scripting.forecast", None),
        (get_optimize_template, OPTIMIZE_HELPERS, "writeragent.scripting.optimize", None),
        (get_quant_template, QUANT_HELPERS, "writeragent.scripting.quant", "run_quant"),
    ],
)
def test_per_helper_templates_are_executable(template_fn, helper_names, module_path, import_name):
    for helper in helper_names:
        code = template_fn(helper)
        assert code is not None
        imported = import_name or helper
        assert f"from {module_path} import {imported}" in code
        assert "# writeragent:" not in code.splitlines()[0]


def test_legacy_header_parsers_still_work():
    code = '# writeragent:quant helper=technical_analysis params={"indicators":["rsi"]}\n'
    meta = parse_quant_script_header(code)
    assert meta is not None
    assert meta.helper == "technical_analysis"

    code = '# writeragent:optimize helper=linear_programming params={"c_col":"c"}\n'
    meta = parse_optimize_script_header(code)
    assert meta is not None
    assert meta.helper == "linear_programming"

    code = '# writeragent:forecast helper=forecast_time_series params={"periods":6}\n'
    meta = parse_forecast_script_header(code)
    assert meta is not None
    assert meta.helper == "forecast_time_series"


def _assert_template_is_python_and_binds(code: str) -> None:
    """Templates used to emit JSON ``null``/``false`` and call keyword-only args by position."""
    tree = ast.parse(code)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert names.isdisjoint({"null", "false", "true"})
    imported_mod = None
    imported_name = None
    call = None
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module and stmt.names:
            imported_mod = stmt.module
            imported_name = stmt.names[0].name
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            call = stmt.value
    assert imported_mod and imported_name and call is not None
    venv_mod_name = imported_mod.replace("writeragent.scripting.", "plugin.scripting.venv.")
    fn = getattr(importlib.import_module(venv_mod_name), imported_name)
    ns = {"data": [[1, 2], [3, 4]], "text": "hello world", "document_context": {}}
    safe_builtins = {"None": None, "True": True, "False": False}

    def _eval(node: ast.AST):
        return eval(compile(ast.Expression(node), "<template>", "eval"), {"__builtins__": safe_builtins}, ns)

    args = [_eval(arg) for arg in call.args]
    kwargs = {kw.arg: _eval(kw.value) for kw in call.keywords}
    try:
        inspect.signature(fn).bind(*args, **kwargs)
    except TypeError as exc:
        raise AssertionError(f"{imported_name} does not accept the generated call: {exc}\n{code}") from exc


def test_shipped_templates_bind_child_signatures():
    from plugin.scripting.symbolic import get_math_script_templates
    from plugin.scripting.viz import get_viz_script_templates

    codes: list[str] = []
    codes.extend(get_analysis_script_templates().values())
    codes.extend(get_text_analytics_script_templates().values())
    codes.extend(get_units_script_templates().values())
    codes.extend(get_math_script_templates().values())
    codes.extend(get_viz_script_templates().values())
    for helper in FORECAST_HELPERS:
        codes.append(get_forecast_template(helper))
    for helper in OPTIMIZE_HELPERS:
        codes.append(get_optimize_template(helper))
    for helper in QUANT_HELPERS:
        codes.append(get_quant_template(helper))
    assert codes
    for code in codes:
        assert code
        _assert_template_is_python_and_binds(code)
