import pytest

from plugin.calc.array_formula import returns_array
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

    undo_mgr = doc.getUndoManager()
    undo_mgr.clear()
    assert not undo_mgr.isUndoPossible()

    manipulator.prepare_array_formula_if_needed("C1", "=FILTER({1;2}, {1;0})")

    assert not undo_mgr.isUndoPossible()

def test_refused_array_write_leaves_no_undo_step(calc_doc):
    """Writing a sequence over occupied cells fails, and leaves no undo step."""
    doc = calc_doc
    sheet = doc.getSheets().getByIndex(0)
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)

    # Occupy C2
    c2 = sheet.getCellByPosition(2, 1)
    c2.setValue(99)

    undo_mgr = doc.getUndoManager()
    undo_mgr.clear()

    with pytest.raises(CalcError, match="needs C1:D3, but 1 cell\\(s\\) there are not empty"):
        sz = manipulator.prepare_array_formula_if_needed("C1", "=SEQUENCE(3,2)")
        if sz:
            manipulator.write_formula_range("C1", "=SEQUENCE(3,2)", premeasured_array_size=sz)

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
