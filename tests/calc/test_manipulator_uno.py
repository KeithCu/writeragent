import pytest

from plugin.calc.manipulator import CellManipulator
from plugin.calc.bridge import CalcBridge
from plugin.framework.errors import CalcError

pytestmark = pytest.mark.uno

def test_manipulator_undo_clears_empty_cells(calc_doc):
    """Test that writing into an empty range and undoing restores the cells to EMPTY."""
    doc = calc_doc
    sheet = doc.getSheets().getByIndex(0)

    c1 = sheet.getCellByPosition(0, 0)
    c2 = sheet.getCellByPosition(1, 0)
    assert c1.getType().value == 0

    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)

    doc.getUndoManager().enterUndoContext("test_write")
    manipulator.write_formula_range("A1:B1", [["A", "B"]])
    doc.getUndoManager().leaveUndoContext()

    assert c1.getString() == "A"

    doc.getUndoManager().undo()
    assert c1.getType().value == 0

def test_manipulator_array_probe_undo(calc_doc):
    doc = calc_doc
    sheet = doc.getSheets().getByIndex(0)
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)

    manipulator.prepare_array_formula_if_needed("C1", "=FILTER({1;2}, {1;0})")

    undo_mgr = doc.getUndoManager()
    assert not undo_mgr.isUndoPossible()

def test_manipulator_clear_whole_array_works(calc_doc):
    doc = calc_doc
    sheet = doc.getSheets().getByIndex(0)
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)

    sz = manipulator.prepare_array_formula_if_needed("A5", "=FILTER({1;2}, {1;1})")
    manipulator.write_formula_range("A5", "=FILTER({1;2}, {1;1})", premeasured_array_size=sz)

    assert sheet.getCellByPosition(0, 4).getValue() == 1

    with pytest.raises(CalcError, match="is part of array"):
        manipulator.clear_range("A5:A5")

    manipulator.clear_range("A5:A6")
    assert sheet.getCellByPosition(0, 4).getType().value == 0
