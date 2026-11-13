"""Checks for the LibrePy OXT plugin bundle path list."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.librepy_bundle_paths import collect_librepy_plugin_paths  # noqa: E402


def test_librepy_bundle_excludes_unbundled_prototype_and_cli_modules() -> None:
    paths = set(collect_librepy_plugin_paths(_REPO_ROOT))
    excluded = {
        "plugin/calc/viz_auto_plot.py",
        "plugin/calc/forecast_auto_plot.py",
        "plugin/calc/error_detector.py",
        "plugin/calc/excel_py_convert/cli.py",
        "plugin/calc/excel_py_convert/__main__.py",
    }
    assert paths.isdisjoint(excluded)


def test_librepy_bundle_keeps_excel_convert_and_analyzer() -> None:
    paths = set(collect_librepy_plugin_paths(_REPO_ROOT))
    assert "plugin/calc/excel_py_convert/convert.py" in paths
    assert "plugin/calc/analyzer.py" in paths
