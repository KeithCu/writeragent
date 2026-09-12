# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI parse for run_eval.py (no paid API)."""
from __future__ import annotations

import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import run_eval  # noqa: E402


def test_parse_args_defaults_keep_full_catalog() -> None:
    args = run_eval.parse_args([])
    assert args.tools == "full"
    assert args.schema_density == "full"
    assert args.student == "llm"
    assert args.backend == "string"


def test_parse_args_tools_and_schema_density() -> None:
    args = run_eval.parse_args(
        ["--tools", "calc_minimal", "--schema-density", "skinny", "-e", "data_sorting"]
    )
    assert args.tools == "calc_minimal"
    assert args.schema_density == "skinny"
    assert args.example == "data_sorting"
