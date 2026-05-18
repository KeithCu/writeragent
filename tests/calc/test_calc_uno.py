# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Calc debug menu entry: aggregates split ``*_uno`` suites (see commit 4bef9439).

``main.RunCalcTests`` / ``RunCalcIntegrationTests`` import this module and
``run_module_suite`` dispatches to ``run_calc_tests`` / ``run_integration_tests``
because there are no ``@native_test`` functions here.

``run_all_tests`` file-walk matches ``*_uno.py``; ``SKIP_NATIVE_RUN_ALL`` tells
``testing_runner`` not to execute this file as its own native suite (would be
wrong/empty without the menu-specific ``calc.tests`` name mapping).
"""
import importlib
from typing import Any

# Used by plugin.testing_runner.run_all_tests — skip this path as a standalone suite.
SKIP_NATIVE_RUN_ALL = True

# Curated split: smaller core smoke vs broader Calc API coverage (adjust here only).
CALC_MENU_UNIT_MODULES: tuple[str, ...] = (
    "plugin.tests.calc.test_base_uno",
    "plugin.tests.calc.test_cells_uno",
    "plugin.tests.calc.test_formulas_uno",
)

CALC_MENU_INTEGRATION_MODULES: tuple[str, ...] = (
    "plugin.tests.calc.test_analyzer_uno",
    "plugin.tests.calc.test_calc_analysis_uno",
    "plugin.tests.calc.test_charts_uno",
    "plugin.tests.calc.test_comments_uno",
    "plugin.tests.calc.test_conditional_uno",
    "plugin.tests.calc.test_enhanced_charts_uno",
    "plugin.tests.calc.test_editselection_uno",
    "plugin.tests.calc.test_pivot_uno",
    "plugin.tests.calc.test_rich_html_uno",
    "plugin.tests.calc.test_search_uno",
    "plugin.tests.calc.test_sheet_filter_uno",
    "plugin.tests.calc.test_sheets_uno",
    "plugin.tests.calc.test_tracking_uno",
)


def _run_calc_menu_aggregated(ctx: Any, doc_model: Any, module_names: tuple[str, ...]) -> tuple[int, int, list[str]]:
    from plugin.testing_runner import run_module_suite

    total_p = 0
    total_f = 0
    lines: list[str] = []
    for dotted in module_names:
        sub = importlib.import_module(dotted)
        short = dotted.rsplit(".", 1)[-1]
        p, f, log = run_module_suite(ctx, sub, f"calc.menu.{short}", doc_model)
        total_p += int(p or 0)
        total_f += int(f or 0)
        lines.append(f"--- {dotted} ({p} passed, {f} failed) ---")
        lines.extend(log or [])
    return total_p, total_f, lines


def run_calc_tests(ctx: Any, doc_model: Any) -> tuple[int, int, list[str]]:
    """Native runner fallback for ``calc.tests`` (debug menu)."""
    return _run_calc_menu_aggregated(ctx, doc_model, CALC_MENU_UNIT_MODULES)


def run_integration_tests(ctx: Any, doc_model: Any) -> tuple[int, int, list[str]]:
    """Native runner fallback for ``calc.integration_tests`` (debug menu)."""
    return _run_calc_menu_aggregated(ctx, doc_model, CALC_MENU_INTEGRATION_MODULES)
