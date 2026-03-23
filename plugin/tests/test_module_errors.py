import pytest
from unittest.mock import MagicMock

from plugin.modules.calc.manipulator import CellManipulator
from plugin.modules.calc.__init__ import CalcError

import sys
sys.modules['com.sun.star.table'] = MagicMock()


@pytest.fixture
def mock_bridge():
    return MagicMock()


@pytest.fixture
def manipulator(mock_bridge):
    return CellManipulator(mock_bridge)


def test_safe_get_cell_value_sheet_none(manipulator):
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(None, "A1")
    assert exc_info.value.code == "CALC_SHEET_NULL"
    assert "Sheet is None" in exc_info.value.message


def test_safe_get_cell_value_invalid_address(manipulator):
    sheet = MagicMock()
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, "1A")
    assert exc_info.value.code == "CALC_INVALID_ADDRESS"
    assert "Invalid cell address" in exc_info.value.message


def test_safe_get_cell_value_cell_not_found(manipulator):
    sheet = MagicMock()
    sheet.getCellRangeByName.side_effect = Exception("Not found")
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, "A1")
    assert exc_info.value.code == "CALC_CELL_NOT_FOUND"


def test_safe_get_cell_value_empty(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.EMPTY
    sheet.getCellRangeByName.return_value = cell

    assert manipulator.safe_get_cell_value(sheet, "A1") is None


def test_safe_get_cell_value_value(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.VALUE
    cell.getValue.return_value = 42.0
    sheet.getCellRangeByName.return_value = cell

    assert manipulator.safe_get_cell_value(sheet, "A1") == 42.0


def test_safe_get_cell_value_text(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.TEXT
    cell.getString.return_value = "Hello"
    sheet.getCellRangeByName.return_value = cell

    assert manipulator.safe_get_cell_value(sheet, "A1") == "Hello"


def test_safe_get_cell_value_formula_success(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.FORMULA
    cell.getError.return_value = 0
    cell.getValue.return_value = 100.0
    sheet.getCellRangeByName.return_value = cell

    assert manipulator.safe_get_cell_value(sheet, "A1") == 100.0


def test_safe_get_cell_value_formula_error(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.FORMULA
    cell.getError.return_value = 503  # #NUM!
    sheet.getCellRangeByName.return_value = cell

    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, "A1")
    assert exc_info.value.code == "CALC_FORMULA_ERROR"
    assert "Formula error in A1: #NUM!" in exc_info.value.message
    assert exc_info.value.details["error_code"] == 503
    assert exc_info.value.details["error_name"] == "#NUM!"


def test_safe_get_cell_value_unknown_type(manipulator):
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = 999  # Unknown
    sheet.getCellRangeByName.return_value = cell

    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, "A1")
    assert exc_info.value.code == "CALC_UNKNOWN_CELL_TYPE"


def test_safe_get_cell_value_unexpected_error(manipulator):
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.side_effect = RuntimeError("Something bad happened")
    sheet.getCellRangeByName.return_value = cell

    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, "A1")
    assert exc_info.value.code == "CALC_CELL_VALUE_ERROR"
    assert "Failed to get cell value" in exc_info.value.message
