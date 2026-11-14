# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the shared @helper registry and per-domain parameter contracts."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from plugin.scripting.helper_registry import bound, get_spec, is_text_diagnostics, names


def test_helper_registry_ast_has_no_heavy_imports():
    registry_path = Path(__file__).resolve().parents[2] / "plugin" / "scripting" / "helper_registry.py"
    tree = ast.parse(registry_path.read_text("utf-8"))

    forbidden = {"numpy", "pandas", "venv", "helper_domain", "domain_registry"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for bad in forbidden:
                    assert bad not in alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for bad in forbidden:
                assert bad not in mod


def test_importing_venv_modules_binds_callables():
    pytest.importorskip("pandas")
    pytest.importorskip("numpy")

    # importing the venv module registers its helpers; run_domain needs them when this test runs alone
    import plugin.scripting.venv.analysis  # noqa: F401
    import plugin.scripting.venv.forecast  # noqa: F401
    import plugin.scripting.venv.optimize  # noqa: F401
    import plugin.scripting.venv.quant  # noqa: F401
    import plugin.scripting.venv.symbolic  # noqa: F401
    import plugin.scripting.venv.units  # noqa: F401
    import plugin.scripting.venv.viz  # noqa: F401

    for domain in ("analysis", "forecast", "optimize", "quant", "symbolic", "units", "viz"):
        for name in names(domain):
            spec = get_spec(domain, name)
            assert spec is not None
            fn = bound(domain, name)
            assert callable(fn)


def test_analysis_describe_data_none_and_extra_param(monkeypatch: pytest.MonkeyPatch):
    from plugin.scripting import helper_registry
    from plugin.scripting.venv.trusted_dispatch import run_domain

    captured: dict[str, Any] = {}

    def fake_describe(data: Any, **kw: Any) -> dict[str, Any]:
        captured.update(kw)
        return {"status": "ok", "helper": "describe_data"}

    monkeypatch.setitem(helper_registry._BOUND, ("analysis", "describe_data"), fake_describe)

    data = [["A", "B"], [1, 2], [3, 4], [5, 6]]
    spec = {
        "helper": "describe_data",
        "params": {
            "include_outliers": None,
            "extra_param_should_be_ignored": 123,
        },
    }
    res = run_domain("analysis", spec, data)
    assert res["status"] == "ok", res
    assert res["helper"] == "describe_data"
    assert captured == {"include_outliers": None}


def test_optimize_extra_param_returns_invalid_param():
    from plugin.scripting.venv.trusted_dispatch import run_domain

    spec = {
        "helper": "linear_programming",
        "params": {
            "c_col": "cost",
            "a_cols": ["x", "y"],
            "b_col": "rhs",
            "unsupported_extra_param": True,
        },
    }
    data = [["cost", "x", "y", "rhs"], [1, 2, 3, 4]]
    res = run_domain("optimize", spec, data)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"


def test_viz_plot_data_keeps_unknown_key_inside_spec(monkeypatch: pytest.MonkeyPatch):
    from plugin.scripting import helper_registry
    from plugin.scripting.venv.trusted_dispatch import run_domain

    captured: dict[str, Any] = {}

    def fake_plot(data: Any, spec: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        captured["spec"] = spec
        return {"status": "ok", "helper": "plot_data"}

    monkeypatch.setitem(helper_registry._BOUND, ("viz", "plot_data"), fake_plot)

    data = [["x", "y"], [1, 2], [3, 4]]
    spec = {
        "helper": "plot_data",
        "params": {
            "chart_type": "scatter",
            "x_col": "x",
            "y_col": "y",
            "custom_chart_option": "retained",
        },
    }
    res = run_domain("viz", spec, data)
    assert res["status"] == "ok", res
    assert res["helper"] == "plot_data"
    assert captured["spec"]["custom_chart_option"] == "retained"


def test_quant_extra_param_is_not_error(monkeypatch):
    from plugin.scripting import helper_registry
    from plugin.scripting.venv.trusted_dispatch import run_domain

    captured: dict[str, Any] = {}

    def fake_ta(data: Any, indicators: Any = None, **kw: Any) -> dict[str, Any]:
        captured["indicators"] = indicators
        captured.update(kw)
        return {"status": "ok", "helper": "technical_analysis"}

    monkeypatch.setitem(helper_registry._BOUND, ("quant", "technical_analysis"), fake_ta)

    spec = {
        "helper": "technical_analysis",
        "params": {
            "indicators": ["sma"],
            "extra_ignored_key": "some_value",
        },
    }
    res = run_domain("quant", spec, [["Close"], [100.0]])
    assert res["status"] == "ok", res
    assert res["helper"] == "technical_analysis"
    assert "extra_ignored_key" not in captured


def test_symbolic_explicit_none_variable_uses_default(monkeypatch: pytest.MonkeyPatch):
    from plugin.scripting import helper_registry
    from plugin.scripting.venv.trusted_dispatch import run_domain

    unset = object()
    captured: dict[str, Any] = {}

    def fake_solve(equation: str = "", variable: Any = unset) -> dict[str, Any]:
        captured["equation"] = equation
        captured["variable"] = variable
        return {"status": "ok", "helper": "solve_equation"}

    monkeypatch.setitem(helper_registry._BOUND, ("symbolic", "solve_equation"), fake_solve)

    spec = {
        "helper": "solve_equation",
        "params": {
            "equation": "x^2 - 4 = 0",
            "variable": None,
        },
    }
    res = run_domain("symbolic", spec, None)
    assert res["status"] == "ok", res
    assert res["helper"] == "solve_equation"
    assert captured["variable"] is unset


def test_units_output_style_popped(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("pint")
    from plugin.scripting import helper_registry
    from plugin.scripting.venv.trusted_dispatch import run_domain

    captured: dict[str, Any] = {}

    def fake_convert(params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {"status": "ok", "helper": "convert_quantity"}

    monkeypatch.setitem(helper_registry._BOUND, ("units", "convert_quantity"), fake_convert)

    spec = {
        "helper": "convert_quantity",
        "params": {
            "value": 100,
            "from": "meter",
            "to": "kilometer",
            "output_style": "compact",
        },
    }
    res = run_domain("units", spec, None)
    assert res["status"] == "ok", res
    assert res["helper"] == "convert_quantity"
    assert "output_style" not in captured
    assert captured["value"] == 100


def test_is_text_diagnostics():
    """Verify is_text_diagnostics recognizes diagnostics and check helpers, tolerating non-string values."""
    assert is_text_diagnostics("diagnostics") is True
    assert is_text_diagnostics("check") is True
    assert is_text_diagnostics("full") is False
    assert is_text_diagnostics("") is False
    assert is_text_diagnostics(None) is False
    assert is_text_diagnostics(["check"]) is False
