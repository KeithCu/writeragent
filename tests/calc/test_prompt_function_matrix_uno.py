# WriterAgent - matrix formula integration for =PYTHON()

import unittest.mock

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import libreoffice_version, with_native_doc


_MATRIX: dict[str, tuple[str, ...]] = {
    "A": ("10", "20", "30", "40"),
    "B": ("100", "200", "300", "400"),
    "C": ("4", "5", "6"),
    "D": ("4", "5", "6"),
    "E": ("7", "8", "Err:32767", "Err:32767", "Err:32767"),
    "F": ("1", "1", "Err:532"),
    "G": ("a", "", "Err:32767"),
    "H": ("North", "South"),
    "I": ("TRUE", "1", "FALSE"),
    "J": ("16",),
    "K": ("", "1"),
    "L": ("81",),
    "M": ("81",),
}

_TOP_LEFT: dict[str, tuple[str, ...]] = {
    "A": ("10", "10", "10", "10"),
    "B": ("100", "100", "100", "100"),
    "C": ("4", "4", "4"),
    "D": ("4", "4", "4"),
    "E": ("7", "7", "7", "7", "7"),
    "F": ("1", "1", "1"),
    "G": ("1", "1", "1"),
    "H": ("North", "North"),
    "I": ("TRUE", "2", "TRUE"),
    "J": ("1",),
    "K": ("", ""),
    "L": ("81",),
    "M": ("81",),
}


def _shown(sheet, col: int, row: int) -> str:
    cell = sheet.getCellByPosition(col, row)
    code = cell.getError()
    return f"Err:{code}" if code else cell.getString()


@native_test
@with_native_doc("calc")
def test_live_array_formula_block_and_recalc(ctx, doc):
    """Real {=PY} array blocks return every element on 26.8+ (top-left before), stay stable on recalc, and support Calc formulas."""
    from plugin.calc.python.workbook_state import WorkbookState
    from tests.calc.python.test_function_uno import _with_repo_venv_for_tests

    expected = _MATRIX if libreoffice_version(ctx) >= (26, 8) else _TOP_LEFT
    sheet = doc.getCurrentController().getActiveSheet()
    try:
        with _with_repo_venv_for_tests(ctx):
            # Unique code per block so on-main calls resolve each origin and is_matrix suppresses spill
            # (off-main build_cell_context sets no origin and returns the native matrix directly).
            sheet.getCellRangeByPosition(0, 0, 0, 3).setArrayFormula('=PYTHON("result = [10, 20, 30, 40]")')
            sheet.getCellRangeByPosition(1, 0, 1, 3).setArrayFormula('=PYTHON("result = [100, 200, 300, 400]"; ROW()-1)')

            # Identical-code blocks both receive the full matrix on 26.8+.
            sheet.getCellRangeByPosition(2, 0, 2, 2).setArrayFormula('=PYTHON("result = [4, 5, 6]")')
            sheet.getCellRangeByPosition(3, 0, 3, 2).setArrayFormula('=PYTHON("result = [4, 5, 6]")')

            # Block longer than result: extra cells show #N/A (32767) on 26.8+.
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

            # Empty first row: top-left is "" on every version.
            sheet.getCellRangeByPosition(10, 0, 10, 1).setArrayFormula('=PYTHON("result = [[], [1]]")')

            # Ordinary cells with identical code (no unique origin, so no spill): corner only.
            sheet.getCellByPosition(11, 0).setFormula('=PYTHON("result = [81, 82]")')
            sheet.getCellByPosition(12, 0).setFormula('=PYTHON("result = [81, 82]")')

            for _idx in range(3):
                doc.calculateAll()
                got = {
                    col: tuple(_shown(sheet, ord(col) - ord("A"), row) for row in range(len(rows)))
                    for col, rows in expected.items()
                }
                assert got == expected
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


@native_test(timeout=60)
@with_native_doc("calc")
def test_array_blocks_survive_save_and_reload(ctx, doc):
    """=PY array blocks survive save and reload in ODS and XLSX."""
    import os
    import tempfile

    import uno
    from com.sun.star.beans import PropertyValue

    from plugin.calc.python.workbook_state import WorkbookState
    from plugin.framework.uno_context import get_desktop
    from tests.calc.python.test_function_uno import _wait_spill_settled, _with_repo_venv_for_tests

    is_matrix = libreoffice_version(ctx) >= (26, 8)
    expected = (
        {
            "A": ("10", "20", "30", "40"),
            "B": ("North", "South"),
            "C": ("1", "1", "Err:532"),
            "D": ("a", "", "Err:32767"),
            "E": ("10", "20", "30"),
        }
        if is_matrix
        else {
            "A": ("10", "10", "10", "10"),
            "B": ("North", "North"),
            "C": ("1", "1", "1"),
            "D": ("1", "1", "1"),
            "E": ("10", "10", "10"),
        }
    )
    block_ranges = (
        (0, 0, 0, 3),
        (1, 0, 1, 1),
        (2, 0, 3, 2),
        (4, 0, 4, 2),
    )

    sheet = doc.getCurrentController().getActiveSheet()
    try:
        with _with_repo_venv_for_tests(ctx):
            sheet.getCellRangeByPosition(0, 0, 0, 3).setArrayFormula('=PYTHON("result = [10, 20, 30, 40]")')
            sheet.getCellRangeByPosition(1, 0, 1, 1).setArrayFormula('=PYTHON("result = [\'North\', \'South\']")')
            sheet.getCellRangeByPosition(2, 0, 3, 2).setArrayFormula(
                '=PYTHON("result = [[1, \'a\'], [True, None], [\'#DIV/0!\', \'#N/A\']]")'
            )
            sheet.getCellRangeByPosition(4, 0, 4, 2).setArrayFormula('=PYTHON("result = data"; A1:A3)')
            doc.calculateAll()

            got_live = {
                col: tuple(_shown(sheet, ord(col) - ord("A"), row) for row in range(len(rows)))
                for col, rows in expected.items()
            }
            assert got_live == expected, f"live mismatch: {got_live!r} != {expected!r}"

            desktop = get_desktop(ctx)
            # Before LibreOffice 26.8, XLSX round-trip leaves D3 of the 3x2 scalar block as "(empty)".
            formats = (("ods", "calc8"), ("xlsx", "Calc MS Excel 2007 XML")) if is_matrix else (("ods", "calc8"),)
            with tempfile.TemporaryDirectory() as tmpdir:
                for ext, filter_name in formats:
                    path = os.path.join(tmpdir, f"array_blocks.{ext}")
                    url = uno.systemPathToFileUrl(path)
                    doc.storeToURL(url, (PropertyValue(Name="FilterName", Value=filter_name),))
                    reloaded = desktop.loadComponentFromURL(
                        url,
                        "_blank",
                        0,
                        (PropertyValue(Name="Hidden", Value=True),),
                    )
                    try:
                        rsheet = reloaded.getSheets().getByIndex(0)
                        got_load = {
                            col: tuple(_shown(rsheet, ord(col) - ord("A"), row) for row in range(len(rows)))
                            for col, rows in expected.items()
                        }
                        assert got_load == expected, f"{ext} load mismatch: {got_load}"

                        reloaded.calculateAll()
                        got_recalc = {
                            col: tuple(_shown(rsheet, ord(col) - ord("A"), row) for row in range(len(rows)))
                            for col, rows in expected.items()
                        }
                        assert got_recalc == expected, f"{ext} recalc mismatch: {got_recalc}"

                        for c1, r1, c2, r2 in block_ranges:
                            af = rsheet.getCellRangeByPosition(c1, r1, c2, r2).getArrayFormula()
                            assert af.startswith("{=PY"), f"{ext} ({c1},{r1}) array formula={af!r}"
                    finally:
                        _wait_spill_settled(reloaded)
                        reloaded_st = WorkbookState.known(reloaded)
                        if reloaded_st is not None:
                            WorkbookState.forget_key(reloaded_st.key)
                        reloaded.close(True)
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)
