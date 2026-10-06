# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO test for Calc =PY() data-arg DAG chaining (Issue #412 / Packet C2.4)."""

from __future__ import annotations

import time

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc

# Isolated testing_runner profiles do not unopkg the OXT, so sheet =PY() is #NAME?
# (504/525) and getValue() stays 0. Direct PythonFunction calls still run.
_PY_UNREGISTERED = frozenset({504, 525})


def _drain_calc(doc) -> None:
    doc.calculateAll()


def _wait_cell_value(doc, cell, expected: float, timeout: float = 2.0) -> bool:
    """Return True if *cell* reaches *expected*. False if =PY is #NAME? (no add-in)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _drain_calc(doc)
        if cell.getValue() == expected:
            return True
        if cell.getError() in _PY_UNREGISTERED:
            return False
        time.sleep(0.05)
    if cell.getValue() == expected:
        return True
    err = cell.getError()
    if err in _PY_UNREGISTERED:
        return False
    raise AssertionError(
        "A1 did not become %r: value=%r error=%r formula=%r"
        % (expected, cell.getValue(), err, cell.getFormula())
    )


@native_test
@with_native_doc("calc")
def test_py_data_arg_dag_chain_uno(ctx, doc):
    from plugin.calc.python.addin import PythonFunction
    from plugin.scripting.venv_worker import PythonWorkerManager

    # Ensure worker subprocess is fresh with current code
    PythonWorkerManager.shutdown_all()

    # 1. Direct PythonFunction add-in calls with single-cell values & tuples
    func = PythonFunction(ctx)

    res_a1 = func.py(None, "result = 2")
    assert res_a1 == 2.0

    res_b1 = func.py(None, "result = data + 3", ((res_a1,),))
    assert res_b1 == 5.0

    res_c1 = func.py(None, "result = data * 4", ((res_b1,),))
    assert res_c1 == 20.0

    # Fan-out direct calls
    res_fan_b = func.py(None, "result = data", ((res_a1,),))
    res_fan_c = func.py(None, "result = data", ((res_a1,),))
    assert res_fan_b == 2.0
    assert res_fan_c == 2.0

    # 3. Issue #413: Boolean return (does not need a registered sheet add-in)
    res_bool_true = func.py(None, "result = True")
    assert res_bool_true == 1.0
    assert isinstance(res_bool_true, float)

    res_bool_false = func.py(None, "result = False")
    assert res_bool_false == 0.0
    assert isinstance(res_bool_false, float)

    res_bool_chain = func.py(None, "result = 'YES' if data else 'NO'", ((res_bool_true,),))
    assert res_bool_chain == "YES"

    res_bool_chain_false = func.py(None, "result = 'YES' if data else 'NO'", ((res_bool_false,),))
    assert res_bool_chain_false == "NO"

    # 4. Live Calc Sheet DAG: C2.4.1 (Chain of three) & C2.4.3 (Fan-out)
    sheet = doc.getSheets().getByIndex(0)

    # C2.4.1: A1 -> B1 -> C1
    sheet.getCellByPosition(0, 0).setFormula('=PY("result = 2")')
    sheet.getCellByPosition(1, 0).setFormula('=PY("result = data + 3"; A1)')
    sheet.getCellByPosition(2, 0).setFormula('=PY("result = data * 4"; B1)')

    # C2.4.3: D1 producer; E1 and F1 fan-out consumers
    sheet.getCellByPosition(3, 0).setFormula('=PY("result = 10")')
    sheet.getCellByPosition(4, 0).setFormula('=PY("result = data"; D1)')
    sheet.getCellByPosition(5, 0).setFormula('=PY("result = data"; D1)')

    # Recalculate. testing_runner uses a blank user profile, so =PY() is often
    # #NAME? — skip the live sheet DAG (direct PythonFunction calls above still ran).
    _drain_calc(doc)
    a1 = sheet.getCellByPosition(0, 0)
    if not _wait_cell_value(doc, a1, 2.0):
        from plugin.framework.logging import log

        log.warning(
            "[test_py_data_arg_dag_chain_uno] skip live sheet DAG — "
            "A1 value=%r error=%r formula=%r (add-in not registered)",
            a1.getValue(),
            a1.getError(),
            a1.getFormula(),
        )
        return

    # Verify C2.4.1 values: A1=2, B1=5, C1=20
    assert a1.getValue() == 2.0
    assert sheet.getCellByPosition(1, 0).getValue() == 5.0
    assert sheet.getCellByPosition(2, 0).getValue() == 20.0

    # Verify C2.4.3 fan-out values: D1=10, E1=10, F1=10 (no MATRIX_SCALAR_SESSIONS collision)
    assert sheet.getCellByPosition(3, 0).getValue() == 10.0
    assert sheet.getCellByPosition(4, 0).getValue() == 10.0
    assert sheet.getCellByPosition(5, 0).getValue() == 10.0

    # 3. Hard recalc consistency (C2.6.3)
    doc.calculateAll()
    assert sheet.getCellByPosition(2, 0).getValue() == 20.0
    assert sheet.getCellByPosition(4, 0).getValue() == 10.0
    assert sheet.getCellByPosition(5, 0).getValue() == 10.0








