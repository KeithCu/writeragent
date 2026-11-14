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


def test_parse_args_defaults_and_overrides() -> None:
    defaults = run_eval.parse_args([])
    assert defaults.backend == "string"
    assert defaults.tools == "full"
    args = run_eval.parse_args(
        ["--tools", "calc_minimal", "--schema-density", "skinny", "-e", "data_sorting"]
    )
    assert args.tools == "calc_minimal"
    assert args.schema_density == "skinny"
    assert args.example == "data_sorting"
    mixed = run_eval.parse_args(["--backend", "auto", "-j", "4", "--lo-workers", "2", "-e", "a,b"])
    assert mixed.backend == "auto"
    assert mixed.jobs == 4
    assert mixed.lo_workers == 2
    assert mixed.example == "a,b"
