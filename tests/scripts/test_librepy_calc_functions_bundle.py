"""LibrePy bundle excludes spreadsheet-import xl helpers; keeps calc_functions_common."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.librepy_bundle_paths import (  # noqa: E402
    LIBREPY_CALC_FUNCTIONS_EXCLUDES,
    collect_librepy_plugin_paths,
)

_LIBREPY_CALC_FUNCTIONS_PATHS = tuple(
    "plugin/scripting/" + rel for rel in LIBREPY_CALC_FUNCTIONS_EXCLUDES
)


def test_librepy_bundle_excludes_calc_functions():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    for excluded in _LIBREPY_CALC_FUNCTIONS_PATHS:
        assert excluded not in paths, f"LibrePy bundle must not include {excluded}"


def test_librepy_bundle_includes_calc_functions_common():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/scripting/calc_functions_common.py" in paths


def test_librepy_bundle_includes_bug_report():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/framework/bug_report.py" in paths


def test_librepy_bundle_includes_udprops():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/doc/udprops.py" in paths


def test_librepy_bundle_includes_calc_utils():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/calc/calc_utils.py" in paths

