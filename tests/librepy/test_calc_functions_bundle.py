"""LibrePy bundle excludes spreadsheet-import xl helpers; keeps calc_functions_common."""

from __future__ import annotations

import os

from scripts.librepy_bundle_paths import (
    LIBREPY_CALC_FUNCTIONS_EXCLUDES,
    collect_librepy_plugin_paths,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_LIBREPY_CALC_FUNCTIONS_PATHS = tuple(
    "plugin/scripting/" + rel for rel in LIBREPY_CALC_FUNCTIONS_EXCLUDES
)


def test_librepy_bundle_excludes_calc_functions():
    paths = collect_librepy_plugin_paths(_REPO_ROOT)
    for excluded in _LIBREPY_CALC_FUNCTIONS_PATHS:
        assert excluded not in paths, f"LibrePy bundle must not include {excluded}"


def test_librepy_bundle_includes_calc_functions_common():
    paths = collect_librepy_plugin_paths(_REPO_ROOT)
    assert "plugin/scripting/calc_functions_common.py" in paths


def test_librepy_bundle_includes_bug_report():
    paths = collect_librepy_plugin_paths(_REPO_ROOT)
    assert "plugin/framework/bug_report.py" in paths
