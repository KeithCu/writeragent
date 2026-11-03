# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI guards for run_optimize (no MIPRO / no paid API)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

pytest.importorskip("dspy")

import run_optimize  # noqa: E402


def test_parse_args_defaults_to_live_student_and_calc_core() -> None:
    args = run_optimize.parse_args([])
    assert args.student == "llm"
    assert args.slice == "calc_core"
    assert args.auto == "light"
    assert args.backend == "string"
    assert args.tools == "full"
    assert args.schema_density == "full"


def test_parse_args_tools_and_schema_density() -> None:
    args = run_optimize.parse_args(
        ["--tools", "calc_minimal", "--schema-density", "skinny", "--slice", "calc_core"]
    )
    assert args.tools == "calc_minimal"
    assert args.schema_density == "skinny"
    assert args.slice == "calc_core"


def test_parse_args_accepts_apply_html_writer_smoke() -> None:
    args = run_optimize.parse_args(
        ["--slice", "apply_html", "-e", "table_from_mess,table_engineering", "-j", "1"]
    )
    assert args.slice == "apply_html"
    assert args.example == "table_from_mess,table_engineering"
    assert args.jobs == 1


def test_parse_args_react_mock_and_task_filter() -> None:
    args = run_optimize.parse_args(
        ["--student", "react-mock", "-e", "data_sorting,tax_column", "-j", "1"]
    )
    assert args.student == "react-mock"
    assert args.example == "data_sorting,tax_column"
    assert args.jobs == 1


def test_winning_instruction_from_live_module() -> None:
    class _Fake:
        def get_slice_text(self) -> str:
            return "WINNING_SLICE"

    assert run_optimize._winning_instruction(_Fake()) == "WINNING_SLICE"
