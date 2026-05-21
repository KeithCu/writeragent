# WriterAgent - matrix formula integration for =PYTHON()

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test
import unittest.mock

_test_doc = None
_test_ctx = None


@setup
def setup_test(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    import uno

    desktop = get_desktop(ctx)
    hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    global _test_doc
    _test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden,))


@teardown
def teardown_test(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


def _cell_value(sheet, col, row):
    cell = sheet.getCellByPosition(col, row)
    err = cell.getError()
    if err != 0:
        return None, err
    return cell.getValue(), 0


@native_test
def test_finalize_python_return_helpers():
    from plugin.calc.python_function import finalize_python_return, is_scalar_index_arg as _is_scalar_index_arg

    class _Ctx:
        pass

    ctx = _Ctx()
    assert _is_scalar_index_arg([2.0]) is True
    assert _is_scalar_index_arg([1, 2]) is False
    assert finalize_python_return(ctx, "c", [10, 20, 30], index_arg=1.0) == 20.0
    assert finalize_python_return(ctx, "x", [1, 2, 3]) == 1.0
    assert finalize_python_return(ctx, "x", [1, 2, 3]) == 2.0


@native_test
def test_python_matrix_via_index_argument():
    """Simulate matrix formula: six calls with index 0..5 return six scalars."""
    from plugin.calc.python_addin import PythonFunction

    sheet = _test_doc.getSheets().getByIndex(0)
    primes = [7919.0, 7927.0, 7933.0, 7937.0, 7949.0, 7951.0]
    func = PythonFunction(_test_ctx)
    code = "result = [sp.prime(x) for x in range(1000, 1006)]"
    with unittest.mock.patch("plugin.calc.python_function.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": [int(p) for p in primes]}
        for row, expected in enumerate(primes):
            res = func.python(code, row)
            assert res == expected, f"row {row}: expected {expected}, got {res}"
        assert mock_run.call_count == 6


@native_test
def test_python_matrix_via_session_counter():
    """Without index arg, repeated calls emit successive list elements."""
    from plugin.calc.python_addin import PythonFunction

    func = PythonFunction(_test_ctx)
    code = "result = [2, 3, 5]"
    with unittest.mock.patch("plugin.calc.python_function.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": [2, 3, 5]}
        assert func.python(code) == 2.0
        assert func.python(code) == 3.0
        assert func.python(code) == 5.0
