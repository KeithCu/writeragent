# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Process oracles for =PY dest and bulk read."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from process_oracles import check_process


def _write(dest: str, formula: str = '=PY("result = 1"; A1:H500)') -> dict:
    return {
        "name": "write_formula_range",
        "arguments": json.dumps({"range": [dest], "values": formula}),
        "result_status": "ok",
        "result_chars": 20,
        "error_code": "",
    }


def test_py_dest_j1_passes() -> None:
    assert check_process("py_unique_beside", [_write("J1")]) == []


def test_py_dest_h1_fails() -> None:
    fails = check_process("py_refuse_overlap", [_write("H1")])
    assert any("inside" in f for f in fails)


def test_bulk_read_fails_no_bulk_task() -> None:
    trace = [
        {
            "name": "read_cell_range",
            "arguments": json.dumps({"range": ["A1:H500"]}),
            "result_status": "ok",
            "result_chars": 4000,
            "error_code": "",
        },
        _write("J1"),
    ]
    fails = check_process("py_no_bulk_read", trace)
    assert any("bulk" in f for f in fails)


def test_domain_python_fails() -> None:
    trace = [
        {
            "name": "delegate_to_specialized_calc_toolset",
            "arguments": json.dumps({"domain": "python", "task": "unique"}),
            "result_status": "error",
            "result_chars": 10,
            "error_code": "unsupported_in_eval",
        },
        _write("J1"),
    ]
    fails = check_process("py_unique_beside", trace)
    assert any("domain=python" in f for f in fails)


def test_non_py_task_ignores_dest() -> None:
    assert check_process("tax_column", [_write("C1", "0.8")]) == []
