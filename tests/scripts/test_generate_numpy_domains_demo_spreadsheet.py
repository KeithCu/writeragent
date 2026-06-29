# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Smoke tests for numpy_domains_demo spreadsheet generator."""

from __future__ import annotations

import importlib.util
import re
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_GEN_PATH = REPO_ROOT / "scripts" / "generate_numpy_domains_demo_spreadsheet.py"

from plugin.scripting.analysis import HELPER_NAMES as ANALYSIS_HELPERS
from plugin.scripting.optimize import HELPER_NAMES as OPTIMIZE_HELPERS
from plugin.scripting.quant import HELPER_NAMES as QUANT_HELPERS
from plugin.scripting.symbolic import HELPER_NAMES as MATH_HELPERS
from plugin.scripting.units import HELPER_NAMES as UNITS_HELPERS
from plugin.scripting.viz import HELPER_NAMES as VIZ_HELPERS
from tests.calc.numpy_domains_demo_cases import (
    DOMAIN_SHEET_ORDER,
    all_domain_demo_cases,
    analysis_demo_cases,
    goal_seek_solver_layout,
    math_demo_cases,
    optimize_demo_cases,
    quant_demo_cases,
    units_demo_cases,
    viz_demo_cases,
)

_LOWERCASE_PYTHON_FN_RE = re.compile(r"of:=python\(")


def test_case_counts_per_domain():
    assert len(analysis_demo_cases()) == 14
    assert len(viz_demo_cases()) == 3
    assert len(math_demo_cases()) == 4
    assert len(quant_demo_cases()) == 4
    assert len(optimize_demo_cases()) == 3
    assert len(units_demo_cases()) == 4
    assert len(all_domain_demo_cases()) == 32


def test_helpers_subset_of_module_names():
    domain_helpers = {
        "analysis": ANALYSIS_HELPERS,
        "viz": VIZ_HELPERS,
        "math": MATH_HELPERS,
        "quant": frozenset(QUANT_HELPERS),
        "optimize": OPTIMIZE_HELPERS,
        "units": UNITS_HELPERS,
    }
    for case in all_domain_demo_cases():
        allowed = domain_helpers[case.domain]
        assert case.helper in allowed, f"{case.helper} not in {case.domain} HELPER_NAMES"


def test_unique_case_ids():
    cases = all_domain_demo_cases()
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))


def test_goal_seek_solver_blocks():
    blocks = goal_seek_solver_layout()
    assert len(blocks) == 2
    assert blocks[0].id == "goal_seek_square"
    assert blocks[1].id == "solver_lp"


def test_generator_writes_ods(tmp_path: Path):
    if importlib.util.find_spec("odf") is None:
        pytest.skip("odfpy not installed")

    spec = importlib.util.spec_from_file_location("generate_numpy_domains_demo_spreadsheet", _GEN_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    mod.generate_all(tmp_path)
    ods = tmp_path / "numpy_domains_demo.ods"
    readme = tmp_path / "numpy_domains_demo.README.md"
    assert ods.is_file()
    assert readme.is_file()

    with zipfile.ZipFile(ods) as zf:
        content = zf.read("content.xml").decode()

    expected_sheets = {"readme", *DOMAIN_SHEET_ORDER, "goal_seek_solver"}
    for sheet in expected_sheets:
        assert f'table:name="{sheet}"' in content

    assert "PYTHON(" in content
    assert _LOWERCASE_PYTHON_FN_RE.search(content) is None
