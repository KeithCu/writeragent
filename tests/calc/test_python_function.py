# WriterAgent - =PYTHON() return coercion tests

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import plugin.calc.python_function as python_function
from plugin.calc.python_function import finalize_python_return, to_calc_compatible


def test_to_calc_compatible_none_becomes_empty_nan_becomes_error() -> None:
    """None (from text/mixed or explicit) becomes empty cell; NaN is returned raw (Calc shows cascading error)."""
    import math
    assert to_calc_compatible(None) == ""
    assert math.isnan(to_calc_compatible(float("nan")))


def test_to_calc_compatible_finite_float_unchanged() -> None:
    assert to_calc_compatible(3.5) == 3.5


def test_to_calc_compatible_nan_in_nested_matrix() -> None:
    """NaN slots in a matrix result stay as NaN (Calc error cells); only None becomes empty."""
    import math
    matrix = ((1.0, float("nan")), (3.0, 4.0))
    out = to_calc_compatible(matrix)
    assert out[0][0] == 1.0
    assert math.isnan(out[0][1])
    assert out[1] == (3.0, 4.0)


def test_finalize_python_return_scalar_nan_becomes_error() -> None:
    """Scalar NaN from worker becomes a Calc error (not silent empty)."""
    import math
    class _Ctx:
        pass

    val = finalize_python_return(_Ctx(), "c", float("nan"))
    assert math.isnan(val)


def test_finalize_python_return_list_nan_becomes_error() -> None:
    """NaN inside a list result becomes nan via to_calc_compatible (Calc error). The matrix session path uses the same coercion."""
    import math
    # Direct coercion for the element (the session path in finalize calls to_calc_compatible on each)
    assert math.isnan(to_calc_compatible(float("nan")))
    # Also exercise finalize with a fresh context (no prior session) for a single nan scalar
    class _Ctx:
        pass
    val = finalize_python_return(_Ctx(), "c2", float("nan"))
    assert math.isnan(val)


@pytest.mark.parametrize("nan_val", [math.nan, float("nan")])
def test_to_calc_compatible_various_nan_literals(nan_val: float) -> None:
    """Any spelling of NaN is returned raw (Calc error), not coerced to empty."""
    import math
    assert math.isnan(to_calc_compatible(nan_val))


def test_insert_image_result_uses_merged_safe_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TmpFile:
        name = "/tmp/fake.png"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def write(self, _data):
            return None

    import plugin.scripting.image_payload as image_payload

    monkeypatch.setattr(image_payload.tempfile, "NamedTemporaryFile", lambda **kwargs: _TmpFile())

    class _UnoModule:
        @staticmethod
        def systemPathToFileUrl(path: str) -> str:
            return f"file://{path}"

    import sys

    monkeypatch.setitem(sys.modules, "uno", _UnoModule())
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h))
    monkeypatch.setitem(sys.modules, "com.sun.star.awt", awt_mod)

    shape = MagicMock()
    draw_page = MagicMock()
    sheet = MagicMock()
    sheet.DrawPage = draw_page
    cell = MagicMock()
    sheet.getCellByPosition.return_value = cell

    selection = MagicMock()
    selection.getRangeAddress.return_value = SimpleNamespace(StartColumn=2, StartRow=3)

    controller = MagicMock()
    controller.getActiveSheet.return_value = sheet
    controller.getSelection.return_value = selection

    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    doc.createInstance.return_value = shape

    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    ctx = SimpleNamespace(ServiceManager=smgr)

    pos = SimpleNamespace(X=111, Y=222)
    size = SimpleNamespace(Width=333, Height=444)
    import plugin.calc.calc_utils as calc_utils

    monkeypatch.setattr(calc_utils, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    python_function._insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"})

    shape.setPosition.assert_called_once_with(pos)
    shape.setSize.assert_any_call(size)
    shape.setPropertyValue.assert_any_call("Anchor", cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", True)


def test_finalize_python_return_triggers_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a list result triggers deferred spilling when not in a matrix selection."""
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    selection = MagicMock()
    # Mock a single cell selection (not matrix selection)
    selection.getRangeAddress.return_value = SimpleNamespace(StartColumn=1, EndColumn=1, StartRow=1, EndRow=1)
    controller = MagicMock()
    controller.getActiveSheet.return_value = sheet
    controller.getSelection.return_value = selection
    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    doc.getURL.return_value = "file:///fake.ods"
    doc.getSheets().getByName.return_value = sheet
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    ctx = SimpleNamespace(ServiceManager=smgr)

    # Clean the spill registry
    python_function.SPILL_REGISTRY.clear()

    # Stub threading.Timer so it runs synchronously for testing
    class DummyTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function
            self.args = args
            self.kwargs = kwargs
        def start(self):
            self.function(*self.args, **self.kwargs)

    monkeypatch.setattr(python_function.threading, "Timer", DummyTimer)

    # Mock the cell value setting
    cell_B2 = MagicMock() # B2 (formula cell at col=1, row=1)
    cell_B2.getFormula.return_value = '=PYTHON("test_code")'
    cell_B3 = MagicMock() # B3 (spilled cell at col=1, row=2)
    cell_B3.getFormula.return_value = ''
    
    # cell.getType() = 0 means empty (no collision)
    cell_B3.getType.return_value = 0

    def get_cell(c, r):
        if r == 1 and c == 1:
            return cell_B2
        if r == 2 and c == 1:
            return cell_B3
        return MagicMock()

    sheet.getCellByPosition.side_effect = get_cell

    result = [10.0, 20.0]  # 1D list, will be treated as shape (2, 1)
    val = finalize_python_return(ctx, "test_code", result)

    assert val == 10.0
    # Verify B3 was written to (B2 is formula cell, we leave it alone so Calc shows the returned 10.0)
    cell_B3.setValue.assert_called_once_with(20.0)

    # Check that spill registry has recorded B3
    key = ("file:///fake.ods", sheet.getName(), 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[key] == [(2, 1)]


def test_finalize_python_return_matrix_formula_does_not_spill() -> None:
    """Test that a matrix selection (e.g. B2:C3) does not trigger spilling, but returns standard scalar instead."""
    sheet = MagicMock()
    selection = MagicMock()
    # EndColumn > StartColumn means it is a matrix selection
    selection.getRangeAddress.return_value = SimpleNamespace(StartColumn=1, EndColumn=2, StartRow=1, EndRow=1)
    controller = MagicMock()
    controller.getActiveSheet.return_value = sheet
    controller.getSelection.return_value = selection
    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    ctx = SimpleNamespace(ServiceManager=smgr)

    result = [[1.0, 2.0], [3.0, 4.0]]
    val = finalize_python_return(ctx, "test_code_matrix", result)

    # Should fall back to standard scalar/session returns for matrix formula
    assert val == 1.0



def test_spill_collision_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that finalize_python_return returns #SPILL! when a cell in the spill target is occupied."""
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    formula_cell = MagicMock()
    formula_cell.getFormula.return_value = '=PYTHON("test_code_spill_blocked")'
    blocked_cell = MagicMock()
    blocked_cell.getFormula.return_value = ''
    
    # cell.getType() != 0 means it contains data/formula (collision!)
    blocked_cell.getType.return_value = 1 

    # Mock selection (single cell at B2 -> StartColumn=1, StartRow=1)
    selection = MagicMock()
    selection.getRangeAddress.return_value = SimpleNamespace(StartColumn=1, EndColumn=1, StartRow=1, EndRow=1)

    controller = MagicMock()
    controller.getActiveSheet.return_value = sheet
    controller.getSelection.return_value = selection

    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    doc.getSheets().getByName.return_value = sheet
    doc.getURL.return_value = "file:///fake.ods"
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    ctx = SimpleNamespace(ServiceManager=smgr)

    # Empty spill registry
    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    # The grid wants to spill to B2 (formula) and B3 (blocked)
    def get_cell(c, r):
        if r == 1 and c == 1:
            return formula_cell
        return blocked_cell

    sheet.getCellByPosition.side_effect = get_cell

    # Synchronous evaluation should return "#SPILL!"
    val = finalize_python_return(ctx, "test_code_spill_blocked", [[100], [200]])

    assert val == "#SPILL!"
    # The registry should be empty for this key
    key = ("file:///fake.ods", "Sheet1", 1, 1)
    assert python_function.SPILL_REGISTRY.get(key) is None


def test_load_and_save_spill_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that spill registry loads from and saves to document properties correctly."""
    import json
    
    saved_payload = None
    
    def mock_get_prop(model, name, default=None):
        if name == "WriterAgentSpillRegistry":
            return json.dumps({
                "Sheet1:1,1": [[2, 1], [3, 1]]
            })
        return default
        
    def mock_set_prop(model, name, value):
        nonlocal saved_payload
        if name == "WriterAgentSpillRegistry":
            saved_payload = value

    monkeypatch.setattr("plugin.doc.document_helpers.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.doc.document_helpers.set_document_property", mock_set_prop)

    doc = MagicMock()
    doc.getURL.return_value = "file:///fake_doc.ods"

    # Reset registry and loaded set
    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    # 1. Test load
    python_function.load_spill_registry_for_doc(doc)
    key = ("file:///fake_doc.ods", "Sheet1", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[key] == [(2, 1), (3, 1)]

    # 2. Test save
    python_function.SPILL_REGISTRY[key] = [(2, 1), (3, 1), (4, 1)]
    python_function.save_spill_registry_for_doc(doc)
    
    assert saved_payload is not None
    data = json.loads(saved_payload)
    assert data["Sheet1:1,1"] == [[2, 1], [3, 1], [4, 1]]


