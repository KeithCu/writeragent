# WriterAgent - matrix formula integration for =PYTHON()

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
import unittest.mock


def _cell_value(sheet, col, row):
    cell = sheet.getCellByPosition(col, row)
    err = cell.getError()
    if err != 0:
        return None, err
    return cell.getValue(), 0


@native_test
def test_finalize_python_return_helpers():
    from plugin.calc.python.function import finalize_python_return, is_scalar_index_arg as _is_scalar_index_arg

    class _Ctx:
        pass

    ctx = _Ctx()
    assert _is_scalar_index_arg([2.0]) is True
    assert _is_scalar_index_arg([1, 2]) is False
    assert finalize_python_return(ctx, "c", [10, 20, 30], index_arg=1.0) == 20.0
    # Dummy ctx has no unique formula origin (XAddIn cannot name the calling cell).
    # Sharing next_index across duplicate =PY() cells would steal scalars, so both
    # calls return the first element. Session increment is tested with a unique origin.
    assert finalize_python_return(ctx, "x", [1, 2, 3]) == 1.0
    assert finalize_python_return(ctx, "x", [1, 2, 3]) == 1.0


@native_test
@with_native_doc("calc")
def test_python_matrix_via_index_argument(ctx, doc):
    """Simulate matrix formula: six calls with index 0..5 return six scalars."""
    from plugin.calc.python.addin import PythonFunction

    primes = [7919.0, 7927.0, 7933.0, 7937.0, 7949.0, 7951.0]
    func = PythonFunction(ctx)
    code = "result = [sp.prime(x) for x in range(1000, 1006)]"
    with unittest.mock.patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": [int(p) for p in primes]}
        for row, expected in enumerate(primes):
            res = func.python(None, code, row)
            assert res == expected, f"row {row}: expected {expected}, got {res}"
        assert mock_run.call_count == 6


@native_test
@with_native_doc("calc")
def test_python_matrix_via_session_counter(ctx, doc):
    """Without index arg, repeated evals of one unique origin emit successive list elements.

    Auto-spill is skipped when the current selection is a multi-cell range (matrix formula).
    """
    from plugin.calc.python.addin import PythonFunction
    from plugin.calc.python.function import clear_python_addin_cache

    code = "result = [2, 3, 5]"
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setFormula('=PYTHON("result = [2, 3, 5]")')
    doc.getCurrentController().select(sheet.getCellRangeByPosition(0, 0, 0, 2))
    clear_python_addin_cache()

    func = PythonFunction(ctx)
    with unittest.mock.patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": [2, 3, 5]}
        assert func.python(None, code) == 2.0
        assert func.python(None, code) == 3.0
        assert func.python(None, code) == 5.0


@native_test
@with_native_doc("calc")
def test_python_matrix_array_formula_block_with_single_cell_selected(ctx, doc):
    from plugin.calc.python.addin import PythonFunction
    from plugin.calc.python.function import clear_python_addin_cache

    code = "result = [2, 3, 5]"
    sheet = doc.getCurrentController().getActiveSheet()
    range_obj = sheet.getCellRangeByPosition(0, 0, 0, 2)
    range_obj.setArrayFormula('=PYTHON("result = [2, 3, 5]")')
    doc.getCurrentController().select(sheet.getCellByPosition(5, 5))
    clear_python_addin_cache()

    func = PythonFunction(ctx)
    with unittest.mock.patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": [2, 3, 5]}
        assert func.python(None, code) == 2.0
        assert func.python(None, code) == 3.0
        assert func.python(None, code) == 5.0
