# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract tests for domain helper templates."""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest

from plugin.scripting.domain_registry import get_domain_wiring
from plugin.scripting.helper_registry import names, specs

ANALYSIS_HELPERS = names("analysis")
FORECAST_HELPERS = names("forecast")
OPTIMIZE_HELPERS = names("optimize")
QUANT_HELPERS = names("quant")
TEXT_HELPERS = names("text")


@pytest.mark.parametrize(
    "wiring_id,helper_names,public_only",
    [
        ("analysis", ANALYSIS_HELPERS, False),
    ],
)
def test_domain_templates_cover_helpers(wiring_id, helper_names, public_only):
    templates = get_domain_wiring(wiring_id).templates()
    expected = {h for h in helper_names if not (public_only and h in ("diagnostics", "check"))}
    assert set(templates.keys()) == expected


def test_units_templates_cover_shipped_helpers():
    shipped = {s.name for s in specs("units") if s.ship}
    templates = get_domain_wiring("units").templates()
    assert set(templates.keys()) == shipped
    for helper, code in templates.items():
        assert f"from writeragent.scripting.units import {helper}" in code


@pytest.mark.parametrize(
    ("wiring_id", "spec_domain", "module_path"),
    [
        ("math", "symbolic", "writeragent.scripting.symbolic"),
        ("viz", "viz", "writeragent.scripting.viz"),
    ],
)
def test_shipped_helper_templates_import_from_domain(wiring_id, spec_domain, module_path):
    shipped = {s.name for s in specs(spec_domain) if s.ship}
    templates = get_domain_wiring(wiring_id).templates()
    assert set(templates.keys()) == shipped
    for helper, code in templates.items():
        assert f"from {module_path} import {helper}" in code
        assert "# writeragent:" not in code.splitlines()[0]


@pytest.mark.parametrize(
    "domain",
    ["analysis", "forecast", "optimize", "quant", "viz", "symbolic", "units", "text"],
)
def test_all_registered_helpers_have_descriptions(domain):
    domain_specs = specs(domain)
    assert domain_specs
    assert all(s.description for s in domain_specs), f"{domain} has helper without description"


def test_text_templates_cover_shipped_helpers():
    shipped = {s.name for s in specs("text") if s.ship}
    templates = get_domain_wiring("text").templates()
    assert set(templates.keys()) == shipped
    public = {h for h in TEXT_HELPERS if h not in ("diagnostics", "check")}
    assert set(templates.keys()) == public
    for helper, code in templates.items():
        assert "from writeragent.scripting.text_analytics import run_text_analytics" in code
        assert f'"helper": "{helper}"' in code or f"'helper': '{helper}'" in code


@pytest.mark.parametrize(
    "wiring_id,helper_names,module_path,import_name",
    [
        ("forecast", FORECAST_HELPERS, "writeragent.scripting.forecast", None),
        ("optimize", OPTIMIZE_HELPERS, "writeragent.scripting.optimize", None),
        ("quant", QUANT_HELPERS, "writeragent.scripting.quant", "run_quant"),
    ],
)
def test_per_helper_templates_are_executable(wiring_id, helper_names, module_path, import_name):
    templates = get_domain_wiring(wiring_id).templates()
    for helper in helper_names:
        code = templates[helper]
        imported = import_name or helper
        assert f"from {module_path} import {imported}" in code
        assert "# writeragent:" not in code.splitlines()[0]


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
    codes = [
        code
        for wid in ("analysis", "text", "units", "math", "viz", "forecast", "optimize", "quant")
        for code in get_domain_wiring(wid).templates().values()
    ]
    assert codes
    for code in codes:
        assert code
        _assert_template_is_python_and_binds(code)
