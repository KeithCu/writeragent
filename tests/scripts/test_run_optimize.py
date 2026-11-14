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


def test_parse_args_defaults_and_overrides() -> None:
    defaults = run_optimize.parse_args([])
    assert defaults.student == "llm"
    assert defaults.slice == "calc_core"
    assert defaults.auto == "light"
    assert defaults.backend == "string"
    assert defaults.tools == "full"
    assert defaults.schema_density == "full"

    args = run_optimize.parse_args(
        [
            "--student",
            "react-mock",
            "--tools",
            "calc_minimal",
            "--schema-density",
            "skinny",
            "--slice",
            "apply_html",
            "-e",
            "table_from_mess,table_engineering",
            "-j",
            "1",
        ]
    )
    assert args.student == "react-mock"
    assert args.tools == "calc_minimal"
    assert args.schema_density == "skinny"
    assert args.slice == "apply_html"
    assert args.example == "table_from_mess,table_engineering"
    assert args.jobs == 1
