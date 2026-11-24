# WriterAgent - matrix formula integration for =PYTHON()

import unittest.mock

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_live_array_formula_block_and_recalc(ctx, doc):
    """Real {=PY} array blocks return every element and stay stable across recalculations."""
    from tests.calc.python.test_function_uno import _with_repo_venv_for_tests

    sheet = doc.getCurrentController().getActiveSheet()
    with _with_repo_venv_for_tests(ctx):
        sheet.getCellRangeByPosition(0, 0, 0, 3).setArrayFormula('=PYTHON("result = [10, 20, 30, 40]")')
        sheet.getCellRangeByPosition(1, 0, 1, 3).setArrayFormula('=PYTHON("result = [10, 20, 30, 40]"; ROW()-1)')

        for _ in range(3):
            doc.calculateAll()
            for row, expected in enumerate((10.0, 20.0, 30.0, 40.0)):
                c0 = sheet.getCellByPosition(0, row)
                c1 = sheet.getCellByPosition(1, row)
                assert c0.getValue() == expected, (row, c0.getError(), c0.getString(), c0.getValue())
                assert c1.getValue() == expected, (row, c1.getError(), c1.getString(), c1.getValue())


@native_test
@with_native_doc("calc")
def test_ordinary_formula_multi_cell_selection_returns_corner(ctx, doc):
    """A range selection does not suppress spill when the formula cell is a normal formula.

    setFormula writes only A1. Selecting A1:A3 is not an array formula, so
    Decision 7 spills and every call returns the corner. Requires
    scripting.python_auto_spill at its default true. The 0.1s spill timer is
    cancelled so it does not write after the document closes.
    """
    from plugin.calc.python.addin import PythonFunction
    from plugin.calc.python.workbook_state import WorkbookState

    code = "result = [2, 3, 5]"
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setFormula('=PYTHON("result = [2, 3, 5]")')
    doc.getCurrentController().select(sheet.getCellRangeByPosition(0, 0, 0, 2))

    func = PythonFunction(ctx)
    try:
        with unittest.mock.patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": [2, 3, 5]}
            assert func.python(doc, code) == 2.0
            assert func.python(doc, code) == 2.0
            assert func.python(doc, code) == 2.0
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)
