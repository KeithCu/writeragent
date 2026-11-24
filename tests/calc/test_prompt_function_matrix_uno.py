# WriterAgent - matrix formula integration for =PYTHON()

import unittest.mock

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_live_array_formula_block_and_recalc(ctx, doc):
    """Real {=PY} array blocks return every element, stay stable on recalc, and support Calc formulas."""
    from plugin.calc.python.workbook_state import WorkbookState
    from tests.calc.python.test_function_uno import _with_repo_venv_for_tests

    sheet = doc.getCurrentController().getActiveSheet()
    try:
        with _with_repo_venv_for_tests(ctx):
            # Unique code per block so locate_formula_cell_in_doc resolves each origin and is_matrix suppresses spill.
            sheet.getCellRangeByPosition(0, 0, 0, 3).setArrayFormula('=PYTHON("result = [10, 20, 30, 40]")')
            sheet.getCellRangeByPosition(1, 0, 1, 3).setArrayFormula('=PYTHON("result = [100, 200, 300, 400]"; ROW()-1)')

            # Identical-code blocks both receive the full matrix.
            sheet.getCellRangeByPosition(2, 0, 2, 2).setArrayFormula('=PYTHON("result = [4, 5, 6]")')
            sheet.getCellRangeByPosition(3, 0, 3, 2).setArrayFormula('=PYTHON("result = [4, 5, 6]")')

            # Block longer than result: extra cells show #N/A (32767).
            sheet.getCellRangeByPosition(4, 0, 4, 4).setArrayFormula('=PYTHON("result = [7, 8]")')

            # 3x2 mixed block: number, text, bool, None, and error tokens.
            sheet.getCellRangeByPosition(5, 0, 6, 2).setArrayFormula(
                '=PYTHON("result = [[1, \'a\'], [True, None], [\'#DIV/0!\', \'#N/A\']]")'
            )

            # All-text block: strings are interned via [][]string so Calc equality and COUNTIF match.
            sheet.getCellRangeByPosition(7, 0, 7, 1).setArrayFormula('=PYTHON("result = [\'North\', \'South\']")')
            sheet.getCellByPosition(8, 0).setFormula('=H1="North"')
            sheet.getCellByPosition(8, 1).setFormula('=COUNTIF(H1:H2;"North")')
            sheet.getCellByPosition(8, 2).setFormula('=H1=H2')

            # Scalar aggregation over a Python list in an ordinary cell.
            sheet.getCellByPosition(9, 0).setFormula('=SUM(PYTHON("result = [1, 2, 3, 10]"))')

            for _ in range(3):
                doc.calculateAll()
                for row, expected in enumerate((10.0, 20.0, 30.0, 40.0)):
                    c0 = sheet.getCellByPosition(0, row)
                    c1 = sheet.getCellByPosition(1, row)
                    assert c0.getValue() == expected, (row, c0.getError(), c0.getString(), c0.getValue())
                    assert c1.getValue() == expected * 10, (row, c1.getError(), c1.getString(), c1.getValue())

            for row, expected in enumerate((4.0, 5.0, 6.0)):
                assert sheet.getCellByPosition(2, row).getValue() == expected
                assert sheet.getCellByPosition(3, row).getValue() == expected

            assert sheet.getCellByPosition(4, 0).getValue() == 7.0
            assert sheet.getCellByPosition(4, 1).getValue() == 8.0
            for extra_row in (2, 3, 4):
                assert sheet.getCellByPosition(4, extra_row).getError() == 32767

            assert sheet.getCellByPosition(5, 0).getValue() == 1.0
            assert sheet.getCellByPosition(6, 0).getString() == "a"
            assert sheet.getCellByPosition(5, 1).getValue() == 1.0
            assert sheet.getCellByPosition(6, 1).getString() == ""
            assert sheet.getCellByPosition(5, 2).getError() == 532
            assert sheet.getCellByPosition(6, 2).getError() == 32767

            assert sheet.getCellByPosition(7, 0).getString() == "North"
            assert sheet.getCellByPosition(7, 1).getString() == "South"
            assert sheet.getCellByPosition(8, 0).getValue() == 1.0
            assert sheet.getCellByPosition(8, 1).getValue() == 1.0
            assert sheet.getCellByPosition(8, 2).getValue() == 0.0

            assert sheet.getCellByPosition(9, 0).getValue() == 16.0
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)


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
