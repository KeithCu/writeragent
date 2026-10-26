# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting._lazy_venv."""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from plugin.scripting._lazy_venv import install_lazy_exports

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HOST_FACADE_PATHS = (
    "plugin/scripting/analysis.py",
    "plugin/scripting/duckdb_sql.py",
    "plugin/scripting/forecast.py",
    "plugin/scripting/optimize.py",
    "plugin/scripting/quant.py",
    "plugin/scripting/symbolic.py",
    "plugin/scripting/text_analytics.py",
    "plugin/scripting/units.py",
    "plugin/scripting/viz.py",
)

# facade module, its declared lazy export set, and the venv module those names
# must resolve from. Without this, a typo or a venv-side rename is silent until
# a caller first touches the name: _lazy_venv propagates the venv AttributeError.
_FACADE_EXPORTS = (
    ("plugin.scripting.analysis", "_ANALYSIS_VENV_EXPORTS", "plugin.scripting.venv.analysis"),
    ("plugin.scripting.duckdb_sql", "_SQL_VENV_EXPORTS", "plugin.scripting.venv.duckdb_sql"),
    ("plugin.scripting.forecast", "_FORECAST_VENV_EXPORTS", "plugin.scripting.venv.forecast"),
    ("plugin.scripting.optimize", "_OPTIMIZE_VENV_EXPORTS", "plugin.scripting.venv.optimize"),
    ("plugin.scripting.quant", "_QUANT_VENV_EXPORTS", "plugin.scripting.venv.quant"),
    ("plugin.scripting.symbolic", "_SYMBOLIC_VENV_EXPORTS", "plugin.scripting.venv.symbolic"),
    ("plugin.scripting.text_analytics", "_TEXT_VENV_EXPORTS", "plugin.scripting.venv.text_analytics"),
    ("plugin.scripting.units", "_UNITS_VENV_EXPORTS", "plugin.scripting.venv.units"),
    ("plugin.scripting.viz", "_VIZ_VENV_EXPORTS", "plugin.scripting.venv.viz"),
)

_DUCKDB_SNIPPET = """\
import sys
import plugin.scripting.duckdb_sql as host
assert "plugin.scripting.venv.duckdb_sql" not in sys.modules
assert "plugin.scripting.venv.coerce" not in sys.modules
assert "run_sql" in dir(host)
assert "plugin.scripting.venv.duckdb_sql" not in sys.modules
templates = host.get_sql_script_templates()
assert "plugin.scripting.venv.duckdb_sql" in sys.modules
rows = str(host.MAX_TABLE_ROWS)
assert rows in templates["query_folder_sql"]
assert rows in templates["query_sheet_sql"]
assert "MAX_TABLE_ROWS" in templates["query_folder_sql"]
"""

_ANALYSIS_SNIPPET = """\
import sys
import plugin.scripting.analysis as host
assert "plugin.scripting.venv.analysis" not in sys.modules
assert "plugin.scripting.venv.coerce" not in sys.modules
names = dir(host)
assert "run_analysis" in names
assert "CoerceResult" in names
assert "coerce_to_dataframe" in names
assert "grid_to_dataframe" in names
assert "get_analysis_script_templates" in names
assert "plugin.scripting.venv.analysis" not in sys.modules
assert "plugin.scripting.venv.coerce" not in sys.modules
host.CoerceResult
assert "plugin.scripting.venv.coerce" in sys.modules
assert "plugin.scripting.venv.analysis" not in sys.modules
host.run_analysis
assert "plugin.scripting.venv.analysis" in sys.modules
"""


def _top_level_import_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def test_dir_lists_lazy_names_without_import(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("plugin.scripting._lazy_probe")
    calls: list[str] = []

    def fake_import(name: str) -> Any:
        calls.append(name)
        raise AssertionError(f"unexpected import {name}")

    monkeypatch.setattr("plugin.scripting._lazy_venv.importlib.import_module", fake_import)
    install_lazy_exports(mod.__dict__, "example", frozenset({"ANSWER"}))

    names = dir(mod)
    assert "ANSWER" in names
    assert calls == []

    mod.LATER = 1
    names_after = dir(mod)
    assert "ANSWER" in names_after
    assert "LATER" in names_after
    assert calls == []


def test_resolve_caches_on_the_module(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("plugin.scripting._lazy_probe")
    calls: list[str] = []
    fake_venv = types.ModuleType("plugin.scripting.venv.example")
    fake_venv.ANSWER = 42

    def fake_import(name: str) -> Any:
        calls.append(name)
        if name == "plugin.scripting.venv.example":
            return fake_venv
        raise ImportError(name)

    monkeypatch.setattr("plugin.scripting._lazy_venv.importlib.import_module", fake_import)
    install_lazy_exports(mod.__dict__, "example", frozenset({"ANSWER"}))

    assert mod.ANSWER == 42
    assert calls == ["plugin.scripting.venv.example"]
    assert "ANSWER" in mod.__dict__

    assert mod.ANSWER == 42
    assert calls == ["plugin.scripting.venv.example"]


def test_unknown_name_uses_facade_module_name() -> None:
    mod = types.ModuleType("plugin.scripting.analysis")
    install_lazy_exports(mod.__dict__, "not_the_facade", frozenset({"something"}))

    with pytest.raises(AttributeError) as exc_info:
        _ = mod.missing

    assert str(exc_info.value) == "module 'plugin.scripting.analysis' has no attribute 'missing'"


def test_extra_loads_the_other_venv_module(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("plugin.scripting._lazy_probe")
    calls: list[str] = []
    fake_example = types.ModuleType("plugin.scripting.venv.example")
    fake_example.PRIMARY = "primary_val"
    fake_coerce = types.ModuleType("plugin.scripting.venv.coerce")
    fake_coerce.CoerceResult = "coerce_val"

    def fake_import(name: str) -> Any:
        calls.append(name)
        if name == "plugin.scripting.venv.example":
            return fake_example
        if name == "plugin.scripting.venv.coerce":
            return fake_coerce
        raise ImportError(name)

    monkeypatch.setattr("plugin.scripting._lazy_venv.importlib.import_module", fake_import)
    install_lazy_exports(
        mod.__dict__,
        "example",
        frozenset({"PRIMARY"}),
        extra={"coerce": frozenset({"CoerceResult"})},
    )

    assert mod.CoerceResult == "coerce_val"
    assert calls == ["plugin.scripting.venv.coerce"]
    assert mod.PRIMARY == "primary_val"
    assert calls == ["plugin.scripting.venv.coerce", "plugin.scripting.venv.example"]


def test_missing_venv_attribute_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("plugin.scripting._lazy_probe")
    fake_venv = types.ModuleType("plugin.scripting.venv.example")
    monkeypatch.setattr(
        "plugin.scripting._lazy_venv.importlib.import_module", lambda name: fake_venv
    )
    install_lazy_exports(mod.__dict__, "example", frozenset({"ANSWER"}))

    with pytest.raises(AttributeError):
        _ = mod.ANSWER

    assert "ANSWER" not in mod.__dict__


def test_overlapping_export_names_raise() -> None:
    mod = types.ModuleType("plugin.scripting._lazy_probe")
    with pytest.raises(ValueError, match="ANSWER"):
        install_lazy_exports(
            mod.__dict__,
            "example",
            frozenset({"ANSWER"}),
            extra={"coerce": frozenset({"ANSWER"})},
        )


def test_host_facades_have_no_toplevel_venv_import() -> None:
    for rel_path in _HOST_FACADE_PATHS:
        path = _REPO_ROOT / rel_path
        modules = _top_level_import_modules(path)
        hits = [m for m in modules if m.startswith("plugin.scripting.venv")]
        assert not hits, f"{rel_path} top-level imports venv module: {hits}"


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param(_DUCKDB_SNIPPET, id="duckdb_sql"),
        pytest.param(_ANALYSIS_SNIPPET, id="analysis"),
    ],
)
def test_host_facade_import_does_not_load_venv(snippet: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("facade_name, exports_attr, venv_name", _FACADE_EXPORTS)
def test_declared_exports_exist_in_venv_module(facade_name: str, exports_attr: str, venv_name: str) -> None:
    facade = importlib.import_module(facade_name)
    venv = importlib.import_module(venv_name)
    exports = getattr(facade, exports_attr)
    assert exports, f"{facade_name}.{exports_attr} is empty"
    missing = sorted(name for name in exports if not hasattr(venv, name))
    assert not missing, f"{facade_name}: {missing} not defined in {venv_name}"

