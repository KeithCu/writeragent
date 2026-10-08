# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Process oracles for =PY dest and bulk read."""
from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest

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


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("J1", id="test_py_dest_j1_passes"),
        pytest.param("I1", id="test_py_dest_i1_passes"),
    ],
)
def test_py_dest_j1_passes(value) -> None:
    assert check_process("py_refuse_overlap", [_write(value)]) == []


@pytest.mark.parametrize(
    "value, value_2, value_3",
    [
        pytest.param("py_no_bulk_read", "J1", '=PY("result = 1"; A1:C8)', id="test_py_process_accepts_a1_c8"),
        pytest.param("py_no_bulk_read", "D1", '=PY("result = 1"; A1:C8)', id="test_py_d1_with_fixture_range_passes"),
        pytest.param("tax_column", "C1", "0.8", id="test_non_py_task_ignores_dest"),
    ],
)
def test_py_process_accepts_a1_c8(value, value_2, value_3) -> None:
    assert (
        check_process(
            value,
            [_write(value_2, value_3)],
        )
        == []
    )

def test_py_dest_h1_fails() -> None:
    fails = check_process("py_refuse_overlap", [_write("H1")])
    assert any("overlap" in f for f in fails)


def test_py_all_writes_earlier_inside_fails() -> None:
    fails = check_process("py_refuse_overlap", [_write("H1"), _write("J1")])
    assert any("overlap" in f for f in fails)

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


def test_flag_task_allows_domain_python() -> None:
    # =PY rows forbid domain=python. The flag requires that path, so the
    # same trace must not zero it before the flag oracle runs.
    trace = [
        {
            "name": "delegate_to_specialized_writer_toolset",
            "arguments": json.dumps({"domain": "python", "task": "flag"}),
            "result_status": "ok",
            "result_chars": 10,
            "error_code": "",
        }
    ]
    assert check_process("python_shapes_flag", trace) == []
    assert any("domain=python" in f for f in check_process("table_from_mess", trace))


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
    fails = check_process("py_refuse_overlap", trace)
    assert any("domain=python" in f for f in fails)


