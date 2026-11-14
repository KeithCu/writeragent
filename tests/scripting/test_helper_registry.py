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
    import plugin.scripting.venv.analysis  # noqa: F401
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
    import plugin.scripting.venv.viz  # noqa: F401
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
    import plugin.scripting.venv.symbolic  # noqa: F401
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


def test_text_dispatch_reaches_one_branch_per_registry_name(monkeypatch: pytest.MonkeyPatch):
    """Agreement check between helper_registry text rows and child execution.

    Text helpers are dispatched through an if-ladder rather than bound directly.
    We mock the heavy NLP models to verify each registry helper reaches its dedicated branch.
    """
    from plugin.scripting.venv import text_analytics
    from plugin.scripting.venv.trusted_dispatch import run_domain

    monkeypatch.setattr(text_analytics, "check_diagnostics", lambda lang=None: {"stub": "diag"})
    monkeypatch.setattr(text_analytics, "analyze_text", lambda text, lang=None, context=None: {"status": "ok", "stub": "full"})
    monkeypatch.setattr(
        text_analytics,
        "_nlp_result",
        lambda text, lang, context, extract: {"status": "ok", "result": extract(None)},
    )
    monkeypatch.setattr(text_analytics, "_extract_readability", lambda doc: ({"r": 1}, {}))
    monkeypatch.setattr(text_analytics, "_extract_entities", lambda doc: [])
    monkeypatch.setattr(text_analytics, "_extract_key_phrases", lambda doc: [])
    monkeypatch.setattr(text_analytics, "_extract_topics", lambda text, n_topics=4: {"topics": []})
    monkeypatch.setattr(text_analytics, "_extract_sentiment", lambda text, params=None: {"overall": {}})

    expected = {
        "full": lambda res: res.get("stub") == "full",
        "readability": lambda res: "readability" in res.get("result", {}),
        "entities": lambda res: "entities" in res.get("result", {}),
        "key_phrases": lambda res: "key_phrases" in res.get("result", {}),
        "topics": lambda res: "topics" in res.get("result", {}),
        "sentiment": lambda res: "sentiment" in res.get("result", {}),
        "diagnostics": lambda res: res.get("result") == {"stub": "diag"},
        "check": lambda res: res.get("result") == {"stub": "diag"},
    }

    assert set(expected) == names("text")
    for name, predicate in expected.items():
        res = run_domain("text", name, "Some text.")
        assert res.get("status") == "ok", f"{name}: {res}"
        assert predicate(res), f"{name} predicate failed on {res}"


@pytest.mark.parametrize(
    "alias",
    [
        "analyze",
        "all",
        "stats",
        "descriptive",
        "ner",
        "keyphrases",
        "chunks",
        "topic",
        "topic_model",
        "polarity",
    ],
)
def test_text_dropped_aliases_are_unknown_helper(alias: str):
    """Only names registered in helper_registry are valid text analytics helpers."""
    from plugin.scripting.venv.trusted_dispatch import run_domain

    res = run_domain("text", alias, "Keith lives in Seattle.")
    assert res == {
        "status": "error",
        "code": "UNKNOWN_HELPER",
        "helper": alias,
        "message": f"Unknown text analytics helper '{alias}'",
    }


def test_text_unknown_helper_with_empty_text_is_unknown_helper():
    """Helper validation runs before text length checks, returning UNKNOWN_HELPER even with empty text."""
    from plugin.scripting.venv.trusted_dispatch import run_domain

    res = run_domain("text", "bogus", "")
    assert res == {
        "status": "error",
        "code": "UNKNOWN_HELPER",
        "helper": "bogus",
        "message": "Unknown text analytics helper 'bogus'",
    }


def test_is_text_diagnostics():
    """Verify is_text_diagnostics recognizes diagnostics and check helpers, tolerating non-string values."""
    assert is_text_diagnostics("diagnostics") is True
    assert is_text_diagnostics("check") is True
    assert is_text_diagnostics("full") is False
    assert is_text_diagnostics("") is False
    assert is_text_diagnostics(None) is False
    assert is_text_diagnostics(["check"]) is False
