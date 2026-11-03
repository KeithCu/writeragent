from __future__ import annotations
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the shared @helper registry and per-domain parameter contracts."""


import ast
from pathlib import Path
from typing import Any

import pytest

from plugin.scripting.helper_registry import bound, get_spec, names, specs


def test_helper_registry_domains_match_expected_names():
    expected_analysis = {
        "describe_data",
        "kpi_summary",
        "detect_outliers",
        "quick_stats",
        "format_currency",
        "format_percent",
        "clean_and_prepare",
        "pivot_aggregate",
        "group_summary",
        "compare_periods",
        "correlation_matrix",
        "run_regression",
        "cluster_numeric",
        "monte_carlo",
    }
    assert names("analysis") == expected_analysis
    assert len(specs("analysis")) == 14

    expected_forecast = {
        "forecast_time_series",
        "decompose_time_series",
        "anomaly_detection_time_series",
    }
    assert names("forecast") == expected_forecast

    expected_optimize = {
        "optimize_portfolio",
        "linear_programming",
        "solve_scheduling_problem",
    }
    assert names("optimize") == expected_optimize

    expected_quant = {
        "fetch_historical_data",
        "technical_analysis",
        "portfolio_tearsheet",
        "efficient_frontier",
    }
    assert names("quant") == expected_quant

    expected_viz = {
        "quick_plot",
        "plot_data",
        "correlation_heatmap",
        "time_series_plot",
    }
    assert names("viz") == expected_viz

    expected_symbolic = {
        "solve_equation",
        "symbolic_simplify",
        "integrate",
        "differentiate",
        "latex_to_math_object",
    }
    assert names("symbolic") == expected_symbolic

    expected_units = {
        "convert_quantity",
        "parse_quantity",
        "format_quantity",
        "check_dimensionality",
    }
    assert names("units") == expected_units

    expected_text = {
        "full",
        "readability",
        "entities",
        "key_phrases",
        "topics",
        "sentiment",
        "diagnostics",
        "check",
    }
    assert names("text") == expected_text


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


def test_analysis_describe_data_none_and_extra_param():
    pytest.importorskip("pandas")
    from plugin.scripting.venv.trusted_dispatch import run_domain

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


def test_viz_plot_data_keeps_unknown_key_inside_spec():
    pytest.importorskip("matplotlib")
    pytest.importorskip("pandas")
    from plugin.scripting.venv.trusted_dispatch import run_domain

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


def test_symbolic_explicit_none_variable_uses_default():
    pytest.importorskip("sympy")
    from plugin.scripting.venv.trusted_dispatch import run_domain

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


def test_units_output_style_popped():
    pytest.importorskip("pint")
    from plugin.scripting.venv.trusted_dispatch import run_domain

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


def test_text_ner_alias_works_and_not_in_names():
    assert "ner" not in names("text")
    from plugin.scripting.venv.trusted_dispatch import run_domain

    spec = {"helper": "ner", "params": {}}
    try:
        res = run_domain("text", spec, "Keith Curtis lives in Seattle.")
    except RuntimeError as exc:
        assert "spaCy" in str(exc) or "spacy" in str(exc)
        return
    except ImportError:
        return
    assert res.get("helper") == "entities" or res.get("code") in ("MISSING_PACKAGE", "SPACY_NOT_INSTALLED", "SPACY_MODEL_MISSING", "TEXT_ANALYTICS_ERROR")
