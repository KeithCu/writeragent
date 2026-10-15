# WriterAgent - =PYTHON() return coercion tests

from __future__ import annotations

import logging
import math
import struct
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import plugin.calc.python.function as python_function
from plugin.calc.python.function import finalize_python_return, to_calc_compatible
from plugin.tests.testing_utils import CalcDocStub


def _ctx_with_doc(doc: CalcDocStub | None = None):
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    desktop.getComponents.return_value = None
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    return SimpleNamespace(ServiceManager=smgr, getServiceManager=lambda: smgr)


def test_to_calc_compatible_none_becomes_empty_nan_becomes_error() -> None:
    """None (from text/mixed or explicit) becomes empty cell; NaN is returned raw (Calc shows cascading error)."""
    import math
    assert to_calc_compatible(None) == ""
    assert math.isnan(to_calc_compatible(float("nan")))


def test_to_calc_compatible_finite_float_unchanged() -> None:
    assert to_calc_compatible(3.5) == 3.5


def test_to_calc_compatible_bool_coercion() -> None:
    # Bugfix (#413): Python bool maps to float 1.0/0.0 for Calc compatibility
    res_true = to_calc_compatible(True)
    assert res_true == 1.0
    assert isinstance(res_true, float)

    res_false = to_calc_compatible(False)
    assert res_false == 0.0
    assert isinstance(res_false, float)



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

    import plugin.scripting.payload_codec as payload_codec

    monkeypatch.setattr(payload_codec.tempfile, "NamedTemporaryFile", lambda **kwargs: _TmpFile())

    class _UnoModule:
        @staticmethod
        def systemPathToFileUrl(path: str) -> str:
            return f"file://{path}"

    import sys

    monkeypatch.setitem(sys.modules, "uno", _UnoModule())
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h), Point=lambda x, y: ("Point", x, y))
    monkeypatch.setitem(sys.modules, "com.sun.star.awt", awt_mod)

    doc = CalcDocStub(selection="C4")
    shape = MagicMock()
    doc._created["com.sun.star.drawing.GraphicObjectShape"] = shape
    sheet = doc.getSheets().getByName("Sheet1")
    cell = sheet.getCellByPosition(2, 3)
    cell.IsMerged = True
    ctx = _ctx_with_doc(doc)

    pos = SimpleNamespace(X=111, Y=222)
    size = SimpleNamespace(Width=8000, Height=5000)
    import plugin.calc.calc_utils as calc_utils

    monkeypatch.setattr(calc_utils, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    python_function.insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"}, doc=doc)

    shape.setPosition.assert_called_once_with(pos)
    shape.setSize.assert_any_call(size)
    shape.setPropertyValue.assert_any_call("Anchor", cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", True)


def test_insert_image_result_thin_merged_cell_preserves_default_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1-row thin merged block (e.g. A1:H1 banner) uses DEFAULT_CHART_SIZE and ResizeWithCell=False."""
    class _TmpFile:
        name = "/tmp/fake.png"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def write(self, _data):
            return None

    import plugin.scripting.payload_codec as payload_codec

    monkeypatch.setattr(payload_codec.tempfile, "NamedTemporaryFile", lambda **kwargs: _TmpFile())

    class _UnoModule:
        @staticmethod
        def systemPathToFileUrl(path: str) -> str:
            return f"file://{path}"

    import sys

    monkeypatch.setitem(sys.modules, "uno", _UnoModule())
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h), Point=lambda x, y: ("Point", x, y))
    monkeypatch.setitem(sys.modules, "com.sun.star.awt", awt_mod)

    doc = CalcDocStub(selection="A1")
    shape = MagicMock()
    doc._created["com.sun.star.drawing.GraphicObjectShape"] = shape
    sheet = doc.getSheets().getByName("Sheet1")
    cell = sheet.getCellByPosition(0, 0)
    cell.IsMerged = True
    ctx = _ctx_with_doc(doc)

    pos = SimpleNamespace(X=0, Y=0)
    # Thin 1-row banner: wide (20000) but short (1270 HMM / ~12.7 mm)
    size = SimpleNamespace(Width=20000, Height=1270)
    import plugin.calc.calc_utils as calc_utils

    monkeypatch.setattr(calc_utils, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    python_function.insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"}, doc=doc)

    shape.setPosition.assert_called_once_with(pos)
    shape.setSize.assert_any_call(("Size", 10000, 6000))
    shape.setPropertyValue.assert_any_call("Anchor", cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", False)


def test_insert_image_result_targets_formula_cell_sheet_when_another_sheet_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #385: When Overview is active, plots for formulas on Viz_Gallery land on Viz_Gallery."""
    from plugin.tests.testing_utils import CalcSheetStub

    class _TmpFile:
        name = "/tmp/fake.png"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def write(self, _data):
            return None

    import plugin.scripting.payload_codec as payload_codec

    monkeypatch.setattr(payload_codec.tempfile, "NamedTemporaryFile", lambda **kwargs: _TmpFile())

    class _UnoModule:
        @staticmethod
        def systemPathToFileUrl(path: str) -> str:
            return f"file://{path}"

    import sys

    monkeypatch.setitem(sys.modules, "uno", _UnoModule())
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h), Point=lambda x, y: ("Point", x, y))
    monkeypatch.setitem(sys.modules, "com.sun.star.awt", awt_mod)

    sheet_overview = CalcSheetStub("Overview")
    sheet_viz = CalcSheetStub("Viz_Gallery")

    doc = CalcDocStub(
        sheets=[sheet_overview, sheet_viz],
        active_sheet="Overview",
        selection="A1:H1",
    )
    shape = MagicMock()
    doc._created["com.sun.star.drawing.GraphicObjectShape"] = shape

    # Formula cell is on Viz_Gallery!D7 (col 3, row 6)
    viz_cell = sheet_viz.getCellByPosition(3, 6)
    code_str = "plt.figure(); plt.plot([1, 2, 3]); plt.title('Sales Trend')"
    viz_cell.setFormula(f'=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("{code_str}")')
    viz_cell.IsMerged = True

    ctx = _ctx_with_doc(doc)

    pos = SimpleNamespace(X=5000, Y=3000)
    size = SimpleNamespace(Width=9000, Height=6000)
    import plugin.calc.calc_utils as calc_utils

    monkeypatch.setattr(calc_utils, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    python_function.insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"}, code=code_str, doc=doc)

    # Must be added to Viz_Gallery's DrawPage, NOT Overview's DrawPage
    sheet_viz.DrawPage.add.assert_called_once_with(shape)
    sheet_overview.DrawPage.add.assert_not_called()
    shape.setPropertyValue.assert_any_call("Anchor", viz_cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", True)
    shape.setSize.assert_any_call(size)


def test_locate_formula_cell_in_doc_finds_on_secondary_sheet() -> None:
    """locate_formula_cell_in_doc locates formula cell on non-active sheet."""
    from plugin.tests.testing_utils import CalcSheetStub

    sheet1 = CalcSheetStub("Overview")
    sheet2 = CalcSheetStub("Viz_Gallery")
    doc = CalcDocStub(sheets=[sheet1, sheet2], active_sheet="Overview")
    ctx = _ctx_with_doc(doc)

    code = "plt.plot([10, 20])"
    formula_cell = sheet2.getCellByPosition(3, 6)
    formula_cell.setFormula(f'=PY("{code}")')

    located = python_function.locate_formula_cell_in_doc(ctx, doc, code)
    assert located is not None
    found_sheet, found_cell, coords = located
    assert found_sheet.getName() == "Viz_Gallery"
    assert found_cell == formula_cell
    assert coords == (6, 3)


def test_insert_image_result_unmerged_single_cell_default_size(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TmpFile:
        name = "/tmp/fake.png"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def write(self, _data):
            return None

    import plugin.scripting.payload_codec as payload_codec

    monkeypatch.setattr(payload_codec.tempfile, "NamedTemporaryFile", lambda **kwargs: _TmpFile())

    class _UnoModule:
        @staticmethod
        def systemPathToFileUrl(path: str) -> str:
            return f"file://{path}"

    import sys

    monkeypatch.setitem(sys.modules, "uno", _UnoModule())
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h), Point=lambda x, y: ("Point", x, y))
    monkeypatch.setitem(sys.modules, "com.sun.star.awt", awt_mod)

    doc = CalcDocStub(selection="C4")
    shape = MagicMock()
    doc._created["com.sun.star.drawing.GraphicObjectShape"] = shape
    sheet = doc.getSheets().getByName("Sheet1")
    cell = sheet.getCellByPosition(2, 3)
    cell.IsMerged = False
    ctx = _ctx_with_doc(doc)

    pos = SimpleNamespace(X=111, Y=222)
    size = SimpleNamespace(Width=333, Height=444)
    import plugin.calc.calc_utils as calc_utils

    monkeypatch.setattr(calc_utils, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    python_function.insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"}, doc=doc)

    shape.setPosition.assert_called_once_with(pos)
    # For single unmerged cells, default size (10000, 6000) should be applied and not overwritten by cell size
    shape.setSize.assert_any_call(("Size", 10000, 6000))
    shape.setPropertyValue.assert_any_call("Anchor", cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", False)


def test_finalize_python_return_triggers_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a list result triggers deferred spilling when not in a matrix selection."""
    doc = CalcDocStub(url="file:///fake.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("test_code")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()

    class DummyTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function
            self.args = args
            self.kwargs = kwargs
        def start(self):
            self.function(*self.args, **self.kwargs)

    monkeypatch.setattr(python_function.threading, "Timer", DummyTimer)
    # Deferred spill posts to the main-thread queue; run immediately in unit tests.
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: fn(*a, **k),
    )
    python_function.LOADED_DOCUMENTS.clear()

    result = [10.0, 20.0]  # 1D list, will be treated as shape (2, 1)
    val = finalize_python_return(ctx, "test_code", result, doc=doc)

    assert val == 10.0
    # B2 is the formula cell (left alone); spill writes B3 via setDataArray.
    assert sheet.getCellByPosition(1, 2).getValue() == 20.0

    key = ("file:///fake.ods", sheet.getName(), 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[key].spills == [(2, 1)]


def test_finalize_python_return_spills_on_secondary_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-spill locates the formula cell and spills correctly even when a different sheet is active."""
    from plugin.tests.testing_utils import CalcSheetStub

    sheet1 = CalcSheetStub("Overview")
    sheet2 = CalcSheetStub("Viz_Gallery")
    doc = CalcDocStub(sheets=[sheet1, sheet2], url="file:///multi.ods", active_sheet="Overview", selection="A1")
    sheet2.getCellByPosition(1, 1).setFormula('=PYTHON("secondary_code")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()

    class DummyTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function
            self.args = args
            self.kwargs = kwargs
        def start(self):
            self.function(*self.args, **self.kwargs)

    monkeypatch.setattr(python_function.threading, "Timer", DummyTimer)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: fn(*a, **k),
    )
    python_function.LOADED_DOCUMENTS.clear()

    result = [100.0, 200.0]
    val = finalize_python_return(ctx, "secondary_code", result, doc=doc)

    assert val == 100.0
    assert sheet2.getCellByPosition(1, 2).getValue() == 200.0
    key = ("file:///multi.ods", "Viz_Gallery", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[key].spills == [(2, 1)]


def test_finalize_python_return_matrix_formula_does_not_spill() -> None:
    """No calling document, so there is nothing to spill and the first element is returned.

    The multi-cell selection on the stub is not what suppresses spill. A known
    non-matrix formula cell still spills; see test_selection_does_not_suppress_spill.
    """
    doc = CalcDocStub()
    sheet = doc.getSheets().getByName("Sheet1")
    # EndColumn > StartColumn means it is a matrix selection
    doc.CurrentController.Selection = sheet.getCellRangeByPosition(1, 1, 2, 1)
    ctx = _ctx_with_doc(doc)

    result = [[1.0, 2.0], [3.0, 4.0]]
    val = finalize_python_return(ctx, "test_code_matrix", result)

    # Should fall back to standard scalar/session returns for matrix formula
    assert val == 1.0


def test_spill_collision_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that finalize_python_return returns #SPILL! when a cell in the spill target is occupied."""
    doc = CalcDocStub(url="file:///fake.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("test_code_spill_blocked")')
    # Occupied spill target (getType() != EMPTY)
    sheet.getCellByPosition(1, 2).setValue(1.0)
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    val = finalize_python_return(ctx, "test_code_spill_blocked", [[100], [200]], doc=doc)

    assert val == "#SPILL!"
    key = ("file:///fake.ods", "Sheet1", 1, 1)
    assert python_function.SPILL_REGISTRY.get(key) is None


def test_load_and_save_spill_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that spill registry loads from and saves to document properties correctly."""
    import json
    from plugin.calc.python.function import SpillCell, SpillRecord

    saved_payload = None

    def mock_get_prop(model, name, default=None):
        if name == "WriterAgentSpillRegistry":
            return saved_payload if saved_payload is not None else json.dumps({
                "Sheet1:1,1": [[2, 1], [3, 1]]
            })
        return default

    def mock_set_prop(model, name, value):
        nonlocal saved_payload
        if name == "WriterAgentSpillRegistry":
            saved_payload = value

    monkeypatch.setattr("plugin.doc.udprops.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", mock_set_prop)

    doc = CalcDocStub(url="file:///fake_doc.ods")

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    python_function.load_spill_registry_for_doc(doc)
    key = ("file:///fake_doc.ods", "Sheet1", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    rec = python_function.SPILL_REGISTRY[key]
    assert rec.spills == [(2, 1), (3, 1)]
    assert rec.code == ""
    assert all(c.kind == "u" for c in rec.cells)

    python_function.SPILL_REGISTRY[key] = SpillRecord(
        code="some_code",
        cells=[SpillCell(2, 1, "n", 20.0), SpillCell(3, 1, "n", 30.0)],
    )
    python_function.save_spill_registry_for_doc(doc)

    assert saved_payload is not None
    data = json.loads(saved_payload)
    assert data["Sheet1:1,1"] == {
        "code": "some_code",
        "cells": [
            {"r": 2, "c": 1, "t": "n", "v": 20.0},
            {"r": 3, "c": 1, "t": "n", "v": 30.0},
        ],
    }

    # Add a second load of that object JSON
    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()
    python_function.load_spill_registry_for_doc(doc)
    rec2 = python_function.SPILL_REGISTRY[key]
    assert rec2.spills == [(2, 1), (3, 1)]
    assert rec2.code == "some_code"
    assert rec2.cells[0].kind == "n" and rec2.cells[0].value == 20.0


def test_untitled_docs_do_not_share_spill_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two unsaved books must not share spill rows or LOADED_DOCUMENTS.

    getURL() is "" for both. Registry identity is each book's RuntimeUID.
    """
    import json
    from plugin.calc.python.function import PreparedSpill, SpillCell, SpillRecord

    stored: dict[int, str] = {}

    def mock_get_prop(model, name, default=None):
        if name == "WriterAgentSpillRegistry":
            return stored.get(id(model), default)
        return default

    def mock_set_prop(model, name, value):
        if name == "WriterAgentSpillRegistry":
            stored[id(model)] = value

    monkeypatch.setattr("plugin.doc.udprops.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", mock_set_prop)

    doc1 = CalcDocStub(url="", props={"RuntimeUID": "uid-spill-1"}, selection="B2")
    doc2 = CalcDocStub(url="", props={"RuntimeUID": "uid-spill-2"}, selection="B2")
    sheet1 = doc1.getSheets().getByName("Sheet1")
    sheet2 = doc2.getSheets().getByName("Sheet1")
    sheet1.getCellByPosition(1, 1).setFormula('=PYTHON("untitled_spill_a")')
    sheet2.getCellByPosition(1, 1).setFormula('=PYTHON("untitled_spill_b")')
    stored[id(doc1)] = json.dumps({"Sheet1:1,1": [[2, 1]]})
    stored[id(doc2)] = json.dumps({"Sheet1:1,1": [[9, 9]]})

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()
    python_function.SHEET_MODIFY_LISTENERS.clear()
    ctx1 = _ctx_with_doc(doc1)
    ctx2 = _ctx_with_doc(doc2)
    key1 = ("uid-spill-1", "Sheet1", 1, 1)
    key2 = ("uid-spill-2", "Sheet1", 1, 1)
    try:
        prepared1 = python_function._prepare_auto_spill(ctx1, "untitled_spill_a", [[1], [2]], doc1)
        prepared2 = python_function._prepare_auto_spill(ctx2, "untitled_spill_b", [[3], [4]], doc2)
        assert isinstance(prepared1, PreparedSpill)
        assert isinstance(prepared2, PreparedSpill)
        assert prepared1.doc_key == "uid-spill-1"
        assert prepared1.sheet_name == "Sheet1"
        assert prepared1.formula_row == 1
        assert prepared1.formula_col == 1

        assert prepared2.doc_key == "uid-spill-2"
        assert prepared2.sheet_name == "Sheet1"
        assert prepared2.formula_row == 1
        assert prepared2.formula_col == 1

        assert python_function.SPILL_REGISTRY[key1].spills == [(2, 1)]
        assert python_function.SPILL_REGISTRY[key2].spills == [(9, 9)]
        assert ("", "Sheet1", 1, 1) not in python_function.SPILL_REGISTRY
        assert python_function.LOADED_DOCUMENTS == {"uid-spill-1", "uid-spill-2"}
        assert "" not in python_function.LOADED_DOCUMENTS

        python_function.SPILL_REGISTRY[key1] = SpillRecord(
            code="untitled_spill_a",
            cells=[SpillCell(2, 1, "n", 2.0), SpillCell(3, 1, "n", 3.0)],
        )
        python_function.save_spill_registry_for_doc(doc1)
        assert json.loads(stored[id(doc1)]) == {
            "Sheet1:1,1": {
                "code": "untitled_spill_a",
                "cells": [
                    {"r": 2, "c": 1, "t": "n", "v": 2.0},
                    {"r": 3, "c": 1, "t": "n", "v": 3.0},
                ],
            }
        }
        assert json.loads(stored[id(doc2)]) == {"Sheet1:1,1": [[9, 9]]}

        # Blank listener identity must not clear either untitled book.
        blank = python_function.CalcSpillModifyListener(ctx1, "", "Sheet1")
        blank.modified(SimpleNamespace(Source=sheet1))
        assert key1 in python_function.SPILL_REGISTRY
        assert key2 in python_function.SPILL_REGISTRY

        # Book 1's modify clears only book 1, even though both sheets are Sheet1.
        sheet1.getCellByPosition(1, 1).setFormula("")
        python_function.CalcSpillModifyListener(ctx1, "uid-spill-1", "Sheet1").modified(SimpleNamespace(Source=sheet1))
        assert key1 not in python_function.SPILL_REGISTRY
        assert python_function.SPILL_REGISTRY[key2].spills == [(9, 9)]

        python_function.SPILL_REGISTRY[key1] = SpillRecord(
            code="untitled_spill_a",
            cells=[SpillCell(2, 1, "n", 2.0)],
        )
        python_function.LOADED_DOCUMENTS.add("uid-spill-1")
        python_function.clear_in_memory_spill_state(doc_url="", lifecycle_key="uid-spill-1")
        assert key1 not in python_function.SPILL_REGISTRY
        assert "uid-spill-1" not in python_function.LOADED_DOCUMENTS
        assert python_function.SPILL_REGISTRY[key2].spills == [(9, 9)]
        assert "uid-spill-2" in python_function.LOADED_DOCUMENTS

        # A scheduled identity from book 1 must not write book 2.
        python_function.perform_deferred_spill(
            ctx2, "uid-spill-1", "Sheet1", 1, 1, [[8], [9]], doc=doc2, code="untitled_spill_b"
        )
        assert sheet2.getCellByPosition(1, 2).getValue() in (0, 0.0)
        assert ("", "Sheet1", 1, 1) not in python_function.SPILL_REGISTRY

        # Legacy blank URL still spills this book under its lifecycle id.
        python_function.perform_deferred_spill(
            ctx2, "", "Sheet1", 1, 1, [[3], [4]], doc=doc2, code="untitled_spill_b"
        )
        assert sheet2.getCellByPosition(1, 2).getValue() == 4.0
        assert ("", "Sheet1", 1, 1) not in python_function.SPILL_REGISTRY
        assert ("uid-spill-2", "Sheet1", 1, 1) in python_function.SPILL_REGISTRY
    finally:
        python_function.SPILL_REGISTRY.clear()
        python_function.LOADED_DOCUMENTS.clear()
        python_function.SHEET_MODIFY_LISTENERS.clear()


def test_session_key_and_init_kwargs_recursion_off_main_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set WRITERAGENT_TESTING to 1 to force inline execution in queue_executor
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")

    # When off main thread, session_key and get_python_init_kwargs must not touch UNO
    # or cause thread safety violations / recursion (Issue #402).
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)

    ctx = MagicMock()
    key = python_function.session_key(ctx, "print('hello')")
    assert key == ("", "", "", "print('hello')", "")

    kwargs = python_function.get_python_init_kwargs(ctx)
    assert kwargs == {}


def test_finalize_python_return_triggers_spill_2d(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a 2D result triggers block spills via setDataArray on appropriate ranges."""
    doc = CalcDocStub(url="file:///fake2d.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("test_code_2d")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()

    class DummyTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function
            self.args = args
            self.kwargs = kwargs
        def start(self):
            self.function(*self.args, **self.kwargs)

    monkeypatch.setattr(python_function.threading, "Timer", DummyTimer)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: fn(*a, **k),
    )
    python_function.LOADED_DOCUMENTS.clear()

    result = [[10.0, 20.0], [30.0, 40.0]]
    val = finalize_python_return(ctx, "test_code_2d", result, doc=doc)

    assert val == 10.0
    assert sheet.getCellByPosition(2, 1).getValue() == 20.0  # C2
    assert sheet.getCellByPosition(1, 2).getValue() == 30.0  # B3
    assert sheet.getCellByPosition(2, 2).getValue() == 40.0  # C3

    key = ("file:///fake2d.ods", "Sheet1", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert set(python_function.SPILL_REGISTRY[key].spills) == {(1, 2), (2, 1), (2, 2)}


def _install_immediate_spill_timer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_main_after_start: dict[str, bool] | None = None,
) -> None:
    """Run the 0.1s spill Timer inline; optionally flip on_main when it fires."""

    class DummyTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function
            self.args = args
            self.kwargs = kwargs

        def start(self):
            if on_main_after_start is not None:
                on_main_after_start["value"] = True
            self.function(*self.args, **self.kwargs)

        def cancel(self):
            return None

    monkeypatch.setattr(python_function.threading, "Timer", DummyTimer)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: fn(*a, **k),
    )


def test_spilltest_a1_off_main_paints_full_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """SpillTest!A1 ``=PY("result=[[11,22],[33,44]]")`` must paint 11,22 / 33,44.

    Headed repro (Scrolly): worker returns the full grid, then
    ``PYTHON eval: target_doc=False on_main=False`` / ``returning scalar: 11``
    with zero ``Spill:`` lines. Off-main finalize passes the caller document
    through and deferred-spills the neighbors on the UI thread.
    """
    from plugin.tests.testing_utils import CalcSheetStub

    spilltest_code = "result=[[11,22],[33,44]]"
    sheet = CalcSheetStub("SpillTest")
    doc = CalcDocStub(
        sheets=[sheet],
        url="file:///SpillTest.ods",
        active_sheet="SpillTest",
        selection="A1",
    )
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{spilltest_code}")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    on_main = {"value": False}
    monkeypatch.setattr(
        "plugin.framework.thread_guard.on_main_thread",
        lambda: on_main["value"],
    )
    # The front window is never consulted; the caller doc is the only source.
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: None)
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    val = finalize_python_return(ctx, spilltest_code, [[11, 22], [33, 44]], doc=doc)

    assert val == 11
    assert sheet.getCellByPosition(1, 0).getValue() == 22  # B1
    assert sheet.getCellByPosition(0, 1).getValue() == 33  # A2
    assert sheet.getCellByPosition(1, 1).getValue() == 44  # B2
    key = ("file:///SpillTest.ods", "SpillTest", 0, 0)
    assert key in python_function.SPILL_REGISTRY
    assert set(python_function.SPILL_REGISTRY[key].spills) == {(0, 1), (1, 0), (1, 1)}


def test_finalize_python_return_spills_off_main_into_caller_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off-main finalize with the caller doc defers locate+write to the UI thread.

    Treating it as a matrix formula returned only grid[0][0] (SpillTest
    [[11,22],[33,44]] painted 11) with zero Spill: logs.
    """
    doc = CalcDocStub(url="file:///offmain-spill.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("off_main_spill")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    on_main = {"value": False}
    monkeypatch.setattr(
        "plugin.framework.thread_guard.on_main_thread",
        lambda: on_main["value"],
    )
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    val = finalize_python_return(ctx, "off_main_spill", [[11, 22], [33, 44]], doc=doc)

    assert val == 11
    assert sheet.getCellByPosition(2, 1).getValue() == 22  # C2
    assert sheet.getCellByPosition(1, 2).getValue() == 33  # B3
    assert sheet.getCellByPosition(2, 2).getValue() == 44  # C3
    key = ("file:///offmain-spill.ods", "Sheet1", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert set(python_function.SPILL_REGISTRY[key].spills) == {(1, 2), (2, 1), (2, 2)}


def test_finalize_python_return_spills_dataframe_payload_off_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DataFrame envelopes must spill header+body off-main, not only the first header."""
    doc = CalcDocStub(url="file:///offmain-df.ods", selection="A1")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PYTHON("df_spill")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    on_main = {"value": False}
    monkeypatch.setattr(
        "plugin.framework.thread_guard.on_main_thread",
        lambda: on_main["value"],
    )
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    payload = {
        "__wa_payload__": "dataframe",
        "columns": ["Region", "Channel"],
        "data": [["North", "Search"], ["South", "Email"]],
    }
    val = finalize_python_return(ctx, "df_spill", payload, doc=doc)

    assert val == "Region"
    assert sheet.getCellByPosition(1, 0).getString() == "Channel"
    assert sheet.getCellByPosition(0, 1).getString() == "North"
    assert sheet.getCellByPosition(1, 1).getString() == "Search"
    assert sheet.getCellByPosition(0, 2).getString() == "South"
    assert sheet.getCellByPosition(1, 2).getString() == "Email"


def test_finalize_python_return_off_main_no_spill_without_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No caller doc: do not guess which book to spill into (front window ignored)."""
    doc = CalcDocStub(url="file:///ambig.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("ambig_spill")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: doc)
    spill_started = {"n": 0}

    class _NoStartTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function

        def start(self):
            spill_started["n"] += 1

        def cancel(self):
            return None

    monkeypatch.setattr(python_function.threading, "Timer", _NoStartTimer)

    val = finalize_python_return(ctx, "ambig_spill", [[11, 22], [33, 44]], doc=None)

    assert val == 11
    assert spill_started["n"] == 0
    assert sheet.getCellByPosition(2, 1).getValue() in (0, 0.0, None, "")
    assert python_function.SPILL_REGISTRY.get(("file:///ambig.ods", "Sheet1", 1, 1)) is None


def test_calc_spill_modify_listener_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that CalcSpillModifyListener cleans up spilled cells when formula is removed."""
    # Listener cleanup asserts clearContents call args; keep a MagicMock sheet for that.
    sheet = MagicMock()
    aEvent = SimpleNamespace(Source=sheet)
    doc = CalcDocStub(url="file:///fake_cleanup.ods")

    monkeypatch.setattr(python_function, "_get_calc_doc", lambda ctx: doc)

    saved = []
    monkeypatch.setattr(python_function, "save_spill_registry_for_doc", lambda d: saved.append(d))

    ctx = MagicMock()
    listener = python_function.CalcSpillModifyListener(ctx, "file:///fake_cleanup.ods", "Sheet1")

    key = ("file:///fake_cleanup.ods", "Sheet1", 1, 1)
    python_function.SPILL_REGISTRY[key] = python_function.SpillRecord(
        code="some_code",
        cells=[python_function.SpillCell(2, 1, "n", 20.0)],
    )

    cell_B2 = MagicMock()
    cell_B3 = MagicMock()
    cell_B3.getType.return_value = 1
    cell_B3.getValue.return_value = 20.0

    def get_cell(c, r):
        if r == 1 and c == 1:
            return cell_B2
        if r == 2 and c == 1:
            return cell_B3
        return MagicMock()

    sheet.getCellByPosition.side_effect = get_cell

    cell_B2.getFormula.return_value = '=PYTHON("some_code")'
    listener.modified(aEvent)

    assert key in python_function.SPILL_REGISTRY
    cell_B3.clearContents.assert_not_called()

    cell_B2.getFormula.return_value = ''
    listener.modified(aEvent)

    assert key not in python_function.SPILL_REGISTRY
    cell_B3.clearContents.assert_called_once_with(55)
    assert len(saved) == 1


def test_spill_orphan_cleanup_saves_sheet_owner_not_active_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orphan cleanup must undo/save the workbook that owns the sheet."""
    owner = CalcDocStub(url="file:///owner.ods", props={"RuntimeUID": "uid-owner"})
    active = CalcDocStub(url="file:///active.ods", props={"RuntimeUID": "uid-active"})
    sheet = owner.getSheets().getByName("Sheet1")
    sheet.getParent = lambda: owner  # type: ignore[method-assign]
    origin = sheet.getCellByPosition(1, 1)
    spilled = sheet.getCellByPosition(1, 2)
    origin.setFormula("")
    spilled.setValue(99)

    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: active)
    saved: list[Any] = []
    monkeypatch.setattr(python_function, "save_spill_registry_for_doc", lambda doc: saved.append(doc))

    key = ("file:///owner.ods", "Sheet1", 1, 1)
    python_function.SPILL_REGISTRY[key] = python_function.SpillRecord(
        code='=PY("test")',
        cells=[python_function.SpillCell(2, 1, "n", 99.0)],
    )
    try:
        listener = python_function.CalcSpillModifyListener(MagicMock(), "file:///owner.ods", "Sheet1")
        listener.modified(SimpleNamespace(Source=sheet))
        assert key not in python_function.SPILL_REGISTRY
        assert spilled.getValue() == 0
        assert saved == [owner]
    finally:
        python_function.SPILL_REGISTRY.pop(key, None)


def test_calc_spill_modify_listener_clears_when_formula_only_contains_py(monkeypatch: pytest.MonkeyPatch) -> None:
    """=PYMT and other formulas that merely contain PY/PYTHON must drop stale spills."""
    sheet = MagicMock()
    aEvent = SimpleNamespace(Source=sheet)
    doc = CalcDocStub(url="file:///fake_pymt.ods")
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda ctx: doc)
    monkeypatch.setattr(python_function, "save_spill_registry_for_doc", lambda d: None)

    listener = python_function.CalcSpillModifyListener(MagicMock(), "file:///fake_pymt.ods", "Sheet1")
    key = ("file:///fake_pymt.ods", "Sheet1", 1, 1)
    python_function.SPILL_REGISTRY[key] = python_function.SpillRecord(
        code="keep",
        cells=[python_function.SpillCell(2, 1, "n", 20.0)],
    )

    origin = MagicMock()
    spilled = MagicMock()
    spilled.getType.return_value = 1
    spilled.getValue.return_value = 20.0

    def get_cell(c, r):
        if r == 1 and c == 1:
            return origin
        if r == 2 and c == 1:
            return spilled
        return MagicMock()

    sheet.getCellByPosition.side_effect = get_cell
    try:
        origin.getFormula.return_value = '=PY("keep")'
        listener.modified(aEvent)
        assert key in python_function.SPILL_REGISTRY
        spilled.clearContents.assert_not_called()

        origin.getFormula.return_value = "=PYMT(0.05, 12, 1000)"
        listener.modified(aEvent)
        assert key not in python_function.SPILL_REGISTRY
        spilled.clearContents.assert_called_once_with(55)
    finally:
        python_function.SPILL_REGISTRY.pop(key, None)


def _clear_spill_timer_registry() -> None:
    import plugin.calc.python.workbook_lifecycle as lifecycle

    python_function._PENDING_SPILL_TIMERS.clear()
    lifecycle._LIFECYCLE_KEYS.clear()
    lifecycle._LIFECYCLE_REFS_BY_KEY.clear()
    lifecycle._LIFECYCLE_KEY_BY_DOC_ID.clear()
    lifecycle._DOC_IDS_BY_LIFECYCLE_KEY.clear()


class _HoldSpillTimer:
    """Timer double that does not run the callback until asked."""

    def __init__(self, interval, function, args=(), kwargs=None):
        self.function = function
        self.args = args
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.finished = threading.Event()

    def start(self) -> None:
        return None

    def cancel(self) -> None:
        self.cancelled = True
        self.finished.set()


class _NowSpillTimer(_HoldSpillTimer):
    def start(self) -> None:
        self.function(*self.args, **self.kwargs)
        self.finished.set()


def test_spill_timers_prune_on_fire_register_and_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finished timers must not keep ctx/grid closures in the registry."""
    _clear_spill_timer_registry()
    monkeypatch.setattr(python_function.threading, "Timer", _NowSpillTimer)
    ran = {"n": 0}
    try:
        python_function.start_deferred_sheet_timer(0.1, lambda: ran.__setitem__("n", ran["n"] + 1), lifecycle_key="fired")
        assert ran["n"] == 1
        assert python_function._PENDING_SPILL_TIMERS == []

        dead = threading.Timer(60, lambda: None)
        dead.cancel()
        python_function._PENDING_SPILL_TIMERS.append(("stale", dead))
        live = _HoldSpillTimer(0.1, lambda: None)
        python_function._register_spill_timer("live", live)
        assert [key for key, _timer in python_function._PENDING_SPILL_TIMERS] == ["live"]

        other = _HoldSpillTimer(0.1, lambda: None)
        other.finished.set()
        python_function._PENDING_SPILL_TIMERS.append(("other", other))
        python_function.cancel_pending_spill_timers("live")
        assert live.cancelled
        assert python_function._PENDING_SPILL_TIMERS == []
    finally:
        _clear_spill_timer_registry()


def test_off_main_spill_timer_registers_cached_lifecycle_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unload cancel must see off-main timers; registering them must not call UNO."""
    from plugin.calc.python.workbook_lifecycle import _lifecycle_key

    _clear_spill_timer_registry()
    doc = CalcDocStub(url="file:///spill-life.ods", props={"RuntimeUID": "uid-spill"})
    calls = {"n": 0}
    original = doc.getPropertyValue

    def _count(name):
        calls["n"] += 1
        return original(name)

    doc.getPropertyValue = _count
    assert _lifecycle_key(doc) == "uid-spill"
    seen = calls["n"]
    assert seen >= 1

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr(python_function.threading, "Timer", _HoldSpillTimer)
    try:
        python_function._queue_off_main_auto_spill(MagicMock(), "code", [[1, 2]], doc)
        assert calls["n"] == seen
        assert [key for key, _timer in python_function._PENDING_SPILL_TIMERS] == ["uid-spill"]
        timer = python_function._PENDING_SPILL_TIMERS[0][1]
        python_function.cancel_pending_spill_timers("uid-spill")
        assert timer.cancelled
        assert python_function._PENDING_SPILL_TIMERS == []

        # No document object: do not guess a workbook key.
        python_function._queue_off_main_auto_spill(MagicMock(), "code", [[1]], None)
        assert [key for key, _timer in python_function._PENDING_SPILL_TIMERS] == []
    finally:
        _clear_spill_timer_registry()


def test_off_main_spill_timer_key_follows_each_workbook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each cached model keeps its own unload key; a missing model stays blank."""
    from plugin.calc.python.workbook_lifecycle import _lifecycle_key

    _clear_spill_timer_registry()
    doc_a = CalcDocStub(props={"RuntimeUID": "uid-a"})
    doc_b = CalcDocStub(props={"RuntimeUID": "uid-b"})
    assert _lifecycle_key(doc_a) == "uid-a"
    assert _lifecycle_key(doc_b) == "uid-b"
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr(python_function.threading, "Timer", _HoldSpillTimer)
    try:
        python_function._queue_off_main_auto_spill(MagicMock(), "a", [[1]], doc_a)
        python_function._queue_off_main_auto_spill(MagicMock(), "b", [[1]], doc_b)
        python_function._queue_off_main_auto_spill(MagicMock(), "none", [[1]], None)
        assert [key for key, _timer in python_function._PENDING_SPILL_TIMERS] == ["uid-a", "uid-b"]
        python_function.cancel_pending_spill_timers("uid-a")
        assert [key for key, _timer in python_function._PENDING_SPILL_TIMERS] == ["uid-b"]
    finally:
        _clear_spill_timer_registry()


def test_session_key_off_main_ignores_cached_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    doc = MagicMock()
    doc.getURL.side_effect = AssertionError("getURL off main")
    key = python_function.session_key(MagicMock(), "print('hello')", doc=doc)
    assert key == ("", "", "", "print('hello')", "")
    doc.getURL.assert_not_called()


def test_finalize_python_return_off_main_skips_session_key_on_cached_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spill-disabled list results must not locate the cached model off-main."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr("plugin.framework.config.get_config_bool", lambda _key: False)
    doc = MagicMock()
    doc.getURL.side_effect = AssertionError("getURL off main")
    val = finalize_python_return(MagicMock(), "c", [10, 20, 30], doc=doc)
    assert val == 10.0
    doc.getURL.assert_not_called()


def test_to_calc_compatible_datetime_types() -> None:
    """Datetime, date, time, and timedelta are converted to ISO strings or fractional day floats."""
    import datetime

    dt = datetime.datetime(2026, 8, 13, 14, 30, 0)
    d = datetime.date(2026, 8, 13)
    t = datetime.time(14, 30, 0)
    td = datetime.timedelta(days=1, hours=12)

    assert to_calc_compatible(dt) == "2026-08-13T14:30:00"
    assert to_calc_compatible(d) == "2026-08-13"
    assert to_calc_compatible(t) == "14:30:00"
    assert to_calc_compatible(td) == 1.5


def test_to_calc_compatible_inf_and_decimal() -> None:
    """±inf pass through as floats; Decimal becomes float. Neither is a new wire type."""
    from decimal import Decimal

    assert to_calc_compatible(float("inf")) == float("inf")
    assert to_calc_compatible(float("-inf")) == float("-inf")
    assert to_calc_compatible(Decimal("1.25")) == 1.25


def test_to_calc_compatible_tz_aware_datetime_strips_offset() -> None:
    """Calc cannot parse +HH:MM / Z; wall time is emitted as naive ISO."""
    import datetime

    dt = datetime.datetime(2026, 8, 13, 14, 30, 0, tzinfo=datetime.timezone.utc)
    assert to_calc_compatible(dt) == "2026-08-13T14:30:00"


def test_to_calc_compatible_pandas_nat_timestamp_datetime64() -> None:
    """NaT → empty; Timestamp → naive ISO; datetime64 → ISO-like text. No pandas import in function.py."""
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")

    assert to_calc_compatible(pd.NaT) == ""
    ts = pd.Timestamp("2026-08-13 14:30:00")
    assert to_calc_compatible(ts) == "2026-08-13T14:30:00"
    ts_tz = pd.Timestamp("2026-08-13 14:30:00", tz="UTC")
    assert to_calc_compatible(ts_tz) == "2026-08-13T14:30:00"
    assert "+" not in str(to_calc_compatible(ts_tz))
    dt64 = np.datetime64("2026-06-25")
    out = to_calc_compatible(dt64)
    assert out == "2026-06-25"
    assert out != 20629.0


def test_to_calc_compatible_jagged_2d_rectangularization() -> None:
    """Jagged 2D lists are padded to a rectangular 2D matrix with empty strings."""
    jagged = [[1, 2, 3], [4, 5], [6]]
    out = to_calc_compatible(jagged)
    assert out == (
        (1.0, 2.0, 3.0),
        (4.0, 5.0, ""),
        (6.0, "", ""),
    )


def test_to_calc_compatible_duck_typed_numeric() -> None:
    """Custom objects with __float__ are coerced to float."""
    class CustomNumber:
        def __init__(self, val: float):
            self.val = val

        def __float__(self) -> float:
            return self.val

    assert to_calc_compatible(CustomNumber(42.5)) == 42.5


def test_calc_python_function_zero_event_pumping_invariant() -> None:
    """Static invariant: function.py must not import event loop pumping or UI draining functions."""
    import inspect
    import plugin.calc.python.function as fn_mod

    source = inspect.getsource(fn_mod)
    assert "processEventsToIdle" not in source
    assert "async_stream" not in source
    assert "run_async_worker_with_drain" not in source


def test_function_module_avoids_document_helpers_import() -> None:
    """First =PY() must not load document_helpers → SheetAnalyzer or the dialog stack."""
    import ast

    tree = ast.parse(Path(python_function.__file__).read_text(encoding="utf-8"))
    mods: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
    assert "plugin.doc.document_helpers" not in mods
    assert "plugin.calc.analyzer" not in mods
    assert "plugin.scripting.document_scripts" not in mods
    assert "plugin.chatbot.dialogs" not in mods


def test_execute_python_addin_maps_missing_venv_error(monkeypatch) -> None:
    python_function.clear_python_addin_cache()
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {
            "status": "error",
            "message": "No python executable found under configured venv: '/missing'",
        },
    )
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda *_a, **_k: None)
    out = python_function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "1+1")
    assert "Settings" in out
    assert "Test" in out
    assert "venv" in out.lower()


def test_execute_python_addin_maps_timeout_error(monkeypatch) -> None:
    python_function.clear_python_addin_cache()
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {
            "status": "error",
            "message": "Python worker failed: timed out after 10 seconds",
        },
    )
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda *_a, **_k: None)
    out = python_function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "1+1")
    assert out.startswith("Error:")
    assert "timed out" in out.lower()
    assert "Settings" in out


def _reset_py_pass_stats() -> None:
    python_function._PY_PASS_STATS.last_end = None
    python_function._PY_PASS_STATS.n = 0
    python_function._PY_PASS_STATS.sum_ms = 0
    python_function._PY_PASS_STATS.pass_start = None


def test_py_timing_off_by_default(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    python_function.clear_python_addin_cache()
    monkeypatch.setattr(python_function, "PYTHON_TIMINGS_LOG", False)
    _reset_py_pass_stats()
    caplog.set_level(logging.DEBUG, logger="plugin.calc.python.function")
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "ok", "result": 2.0},
    )
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda *_a, **_k: None)
    python_function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "1+1")
    assert not any(r.message.startswith("py_timing ") for r in caplog.records)


def test_py_timing_logs_ipc_ms_and_pass_totals(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Each =PY() logs its own calculation time; pass_* is wall vs sum, not asctime deltas."""
    python_function.clear_python_addin_cache()
    monkeypatch.setattr(python_function, "PYTHON_TIMINGS_LOG", True)
    _reset_py_pass_stats()
    caplog.set_level(logging.DEBUG, logger="plugin.calc.python.function")
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "ok", "result": 2.0},
    )
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda *_a, **_k: None)
    ctx = _ctx_with_doc(CalcDocStub())
    code = (
        'from writeragent.scripting.analysis import run_analysis; '
        'run_analysis({"helper":"describe_data","params":{}}, data, {})["status"]'
    )
    assert python_function.execute_python_addin(ctx, code) == 2.0
    assert python_function.execute_python_addin(ctx, code) == 2.0
    from tests.harness.strip_bundle import module_source_contains

    if not module_source_contains(python_function, "py_timing "):
        return

    lines = [r.message for r in caplog.records if r.message.startswith("py_timing ")]
    assert len(lines) >= 2
    assert "code=describe_data" in lines[0]
    assert "ipc_ms=" in lines[0]
    assert "total_ms=" in lines[0]
    assert "pass_outside_ms=" in lines[0]
    assert "n=1" in lines[0]
    assert "n=2" in lines[1]
    assert "pass_sum_ms=" in lines[1]


def test_py_timing_cached_matrix_skips_ipc(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    python_function.clear_python_addin_cache()
    monkeypatch.setattr(python_function, "PYTHON_TIMINGS_LOG", True)
    _reset_py_pass_stats()
    caplog.set_level(logging.DEBUG, logger="plugin.calc.python.function")
    doc = CalcDocStub()
    sheet = doc.getSheets().getByIndex(0)
    code = "result = [1, 2, 3]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    ctx = _ctx_with_doc(doc)
    worker_data = None
    tid = threading.get_ident()
    key = (tid, python_function.session_key(ctx, code, doc=doc), python_function._worker_data_token(worker_data))
    python_function._MATRIX_SCALAR_SESSIONS[key] = python_function.WorkerResultSession(
        [1.0, 2.0, 3.0], [1.0, 2.0, 3.0]
    )
    called: list[int] = []

    def _should_not_run(*_a, **_k):
        called.append(1)
        return {"status": "ok", "result": 99}

    monkeypatch.setattr(python_function, "run_code_in_user_venv", _should_not_run)
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    out = python_function.execute_python_addin(ctx, code, doc=doc)
    assert called == []
    assert out == 1.0
    from tests.harness.strip_bundle import module_source_contains

    if not module_source_contains(python_function, "py_timing "):
        return
    lines = [r.message for r in caplog.records if r.message.startswith("py_timing ")]
    assert lines
    assert "cached=1" in lines[-1]
    assert "ipc_ms=0" in lines[-1]


def test_perform_deferred_spill_aborts_when_formula_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///spill.ods")
    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula("=SUM(A2)")
    ctx = _ctx_with_doc(doc)
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    python_function.perform_deferred_spill(
        ctx, "file:///spill.ods", "Sheet1", 0, 0, [["x", "y"]], doc=doc, code="result = 1"
    )
    assert sheet.getCellByPosition(1, 0).getString() in ("", None) or not sheet.getCellByPosition(1, 0).getFormula()


def test_scalar_for_list_result_no_share_without_unique_origin() -> None:
    ctx = _ctx_with_doc(CalcDocStub())
    python_function.clear_python_addin_cache()
    a = python_function.scalar_for_list_result(ctx, "dup", [10, 20, 30])
    b = python_function.scalar_for_list_result(ctx, "dup", [10, 20, 30])
    assert a == 10
    assert b == 10


def test_scalar_for_list_result_increments_with_unique_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx_with_doc(CalcDocStub())
    python_function.clear_python_addin_cache()
    monkeypatch.setattr(
        python_function,
        "session_key",
        lambda *_a, **_k: ("file:///u.ods", "Sheet1", "sid", "dup", "0,0"),
    )
    a = python_function.scalar_for_list_result(ctx, "dup", [10, 20, 30])
    b = python_function.scalar_for_list_result(ctx, "dup", [10, 20, 30])
    c = python_function.scalar_for_list_result(ctx, "dup", [10, 20, 30])
    assert a == 10
    assert b == 20
    assert c == 30


def test_format_error_for_display_distinguishes_timeout_error() -> None:
    """Issue #402: host marshal TimeoutError must not format as user venv settings guidance."""
    exc = TimeoutError("Main-thread execution of _workbook_session_id_impl timed out after 30.0s")
    formatted = python_function._format_error_for_display(exc)
    assert "Main-thread execution timed out" in formatted
    assert "Settings" not in formatted
    assert "Test the venv" not in formatted


def _skip_if_release_bundle() -> None:
    from plugin.framework import thread_guard as tg
    # In release bundles, thread_guard is replaced by a no-op stub that lacks internal guard machinery
    if not hasattr(tg, "_designated_main_thread") or not hasattr(tg, "_violation_ui_lock"):
        pytest.skip("Thread-guard background tests require active dev thread_guard (stripped in release bundles)")


def test_execute_python_addin_from_background_thread_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #402: external PyUNO setFormula runs on a bridge thread (Dummy-N).

    Must run without triggering Layer A UNO thread violations or deadlocking against main thread.
    """
    _skip_if_release_bundle()
    from plugin.framework import thread_guard as tg

    monkeypatch.setattr(tg, "GUARD_ON", True)
    monkeypatch.setattr("plugin.scripting.session_manager.python_session_mode", lambda _ctx: "isolated")

    ctx = _ctx_with_doc(None)

    def fake_run(ctx, code, **kwargs):
        return {"status": "ok", "result": 2.0}

    monkeypatch.setattr(python_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)

    result_holder: list[Any] = []
    exc_holder: list[BaseException] = []

    def worker():
        try:
            res = python_function.execute_python_addin(ctx, "1+1")
            result_holder.append(res)
        except BaseException as e:
            exc_holder.append(e)

    t = threading.Thread(target=worker, name="Dummy-2")
    t.start()
    t.join(timeout=3.0)

    assert not exc_holder
    assert result_holder == [2.0]


def test_execute_python_addin_from_background_thread_shared_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #402: shared mode off-main falls back safely if main-thread marshal fails."""
    _skip_if_release_bundle()
    from plugin.framework import thread_guard as tg

    monkeypatch.setattr(tg, "GUARD_ON", True)
    monkeypatch.setattr("plugin.scripting.session_manager.python_session_mode", lambda _ctx: "shared")

    # Simulate execute_on_main_thread timing out because main thread is blocked in UNO dispatch
    def fake_execute_on_main(fn, *args, **kwargs):
        raise TimeoutError("Main-thread execution timed out")

    monkeypatch.setattr("plugin.framework.queue_executor.execute_on_main_thread", fake_execute_on_main)

    ctx = _ctx_with_doc(None)

    def fake_run(ctx, code, session_id=None, **kwargs):
        # Even with timeout in shared mode, it falls back to isolated (session_id=None) and returns result
        return {"status": "ok", "result": 42.0}

    monkeypatch.setattr(python_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)

    result_holder: list[Any] = []
    exc_holder: list[BaseException] = []

    def worker():
        try:
            res = python_function.execute_python_addin(ctx, "6*7")
            result_holder.append(res)
        except BaseException as e:
            exc_holder.append(e)

    t = threading.Thread(target=worker, name="Dummy-3")
    t.start()
    t.join(timeout=3.0)

    assert not exc_holder
    assert result_holder == [42.0]




def test_off_main_spill_timer_does_not_register_empty_lkey(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = None
    t_start_called = [False]
    class MockTimer:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            t_start_called[0] = True

    monkeypatch.setattr(python_function, "_new_spill_timer", MockTimer)

    reg_called = [False]
    def mock_register(*args, **kwargs):
        reg_called[0] = True
    monkeypatch.setattr(python_function, "_register_spill_timer", mock_register)

    python_function._queue_off_main_auto_spill(None, "code", [[1, 2]], doc)

    assert t_start_called[0], "Timer should still be started"
    assert not reg_called[0], "Timer should not be registered when lkey is empty"


def test_matrix_cache_staleness_after_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 1: [1,2,3] gives 1; after the gap, the next call gives 1 again and the worker is called."""
    import time

    ctx = _ctx_with_doc(CalcDocStub())
    python_function.clear_python_addin_cache()

    curr_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: curr_time[0])
    monkeypatch.setattr(
        python_function,
        "session_key",
        lambda *_a, **_k: ("file:///doc.ods", "Sheet1", "sid", "code_gap", "0,0"),
    )

    worker_calls: list[int] = []

    def mock_run(*_a, **_k):
        worker_calls.append(1)
        return {"status": "ok", "result": [1, 2, 3]}

    monkeypatch.setattr(python_function, "run_code_in_user_venv", mock_run)

    # 1. First call: worker is called, returns 1st element
    res1 = python_function._execute_python_addin_impl(ctx, "code_gap")
    assert res1 == 1.0
    assert len(worker_calls) == 1

    # 2. Second call within gap: worker is NOT called, returns 2nd element
    curr_time[0] += 0.5
    res2 = python_function._execute_python_addin_impl(ctx, "code_gap")
    assert res2 == 2.0
    assert len(worker_calls) == 1

    # 3. Third call after gap (> _PY_PASS_GAP_SEC = 2.0s):
    # Cache is treated as stale, worker is called again, returns 1st element
    curr_time[0] += 3.0
    res3 = python_function._execute_python_addin_impl(ctx, "code_gap")
    assert res3 == 1.0
    assert len(worker_calls) == 2


def test_scalar_for_list_result_stale_after_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 1: scalar_for_list_result resets next_index to 0 after gap."""
    import time

    ctx = _ctx_with_doc(CalcDocStub())
    python_function.clear_python_addin_cache()

    curr_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: curr_time[0])
    monkeypatch.setattr(
        python_function,
        "session_key",
        lambda *_a, **_k: ("file:///doc.ods", "Sheet1", "sid", "code_gap2", "0,0"),
    )

    a = python_function.scalar_for_list_result(ctx, "code_gap2", [1, 2, 3])
    assert a == 1.0
    curr_time[0] += 0.5
    b = python_function.scalar_for_list_result(ctx, "code_gap2", [1, 2, 3])
    assert b == 2.0

    curr_time[0] += 3.0  # > _PY_PASS_GAP_SEC
    c = python_function.scalar_for_list_result(ctx, "code_gap2", [1, 2, 3])
    assert c == 1.0


def test_text_single_cell_arg_spills_instead_of_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 2: Single cell text arg spills normally; numeric arg stays an index."""
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    python_function.clear_python_addin_cache()

    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "ok", "result": ["hello", "world"]},
    )
    spill_calls: list[int] = []
    monkeypatch.setattr(
        python_function,
        "_prepare_auto_spill",
        lambda *_a, **_k: python_function.PreparedSpill("file:///u.ods", "Sheet1", 0, 0, sheet=doc.getSheets().getByName("Sheet1")),
    )
    monkeypatch.setattr(
        python_function,
        "_queue_deferred_spill_write",
        lambda *_a, **_k: spill_calls.append(1),
    )

    # Text arg: A1 contains "text"
    res_text = python_function._execute_python_addin_impl(ctx, "[data]*3", data=[["text"]], doc=doc)
    # Spills normally (returns corner "hello" and queued deferred spill write, does NOT error)
    assert res_text == "hello"
    assert len(spill_calls) == 1

    # Numeric arg: e.g. ROW()-1 (value 1.0) -> acts as index, returns 2nd element
    res_num = python_function._execute_python_addin_impl(ctx, "[data]*3", data=[[1.0]], doc=doc)
    assert res_num == "world"


def test_prepare_auto_spill_with_16384_columns() -> None:
    """Item 3: A mock sheet with 16384 columns and a spill past column 1024 is not #SPILL!."""
    import unittest.mock
    from unittest.mock import MagicMock

    ctx = MagicMock()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    cols = MagicMock()
    cols.getCount.return_value = 16384
    rows = MagicMock()
    rows.getCount.return_value = 1048576
    sheet.getColumns.return_value = cols
    sheet.getRows.return_value = rows

    cell = MagicMock()
    cell.getType.return_value = 0  # EMPTY
    sheet.getCellByPosition.return_value = cell

    doc = MagicMock()
    doc.getURL.return_value = "file:///wide.ods"
    doc.getPropertyValue.side_effect = lambda prop: "uid-wide" if prop == "RuntimeUID" else None
    doc.getSheets().getByName.return_value = sheet

    # Locate formula at (row 0, col 1020)
    with unittest.mock.patch(
        "plugin.calc.python.function.locate_formula_cell_in_doc",
        return_value=(sheet, cell, (0, 1020)),
    ):
        # 1 row by 10 columns grid -> targets col 1020..1029 (crosses past 1024)
        grid = [[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]]
        prepared = python_function._prepare_auto_spill(ctx, "py_code", grid, doc)
        assert prepared != "#SPILL!"
        assert isinstance(prepared, python_function.PreparedSpill)
        assert prepared.doc_key == "uid-wide"
        assert prepared.sheet_name == "Sheet1"
        assert prepared.formula_row == 0
        assert prepared.formula_col == 1020


def test_prepare_auto_spill_fallback_columns_are_16384() -> None:
    from unittest.mock import MagicMock, patch

    class FallbackCellStub:
        def getType(self) -> int:
            return 0  # EMPTY

    class FallbackSheetStub:
        def __init__(self, cell: FallbackCellStub) -> None:
            self._cell = cell

        def getName(self) -> str:
            return "Sheet1"

        def getCellByPosition(self, col: int, row: int) -> FallbackCellStub:
            return self._cell

    cell = FallbackCellStub()
    sheet = FallbackSheetStub(cell)
    doc = CalcDocStub(url="file:///wide-fallback.ods", props={"RuntimeUID": "uid-wide-fallback"})
    ctx = MagicMock()
    grid = [[1, 2]]

    doc_id = "uid-wide-fallback"
    try:
        with patch(
            "plugin.calc.python.function.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 16300)),
        ):
            prepared = python_function._prepare_auto_spill(ctx, "py_code", grid, doc)
            assert prepared != "#SPILL!"
            assert isinstance(prepared, python_function.PreparedSpill)
            assert prepared.doc_key == doc_id
            assert prepared.sheet_name == "Sheet1"
            assert prepared.formula_row == 0
            assert prepared.formula_col == 16300

        with patch(
            "plugin.calc.python.function.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 16383)),
        ):
            prepared_spill = python_function._prepare_auto_spill(ctx, "py_code", grid, doc)
            assert prepared_spill == "#SPILL!"
    finally:
        python_function.LOADED_DOCUMENTS.discard(doc_id)
        for key in [k for k in python_function.SPILL_REGISTRY if k[0] == doc_id]:
            python_function.SPILL_REGISTRY.pop(key, None)


def test_ragged_results_handling() -> None:
    """Item 4: Ragged results in flatten_result_values and _result_as_spill_grid."""
    # flatten_result_values:
    # [[1, 2], 3] -> [1, 2, 3] (no TypeError)
    assert python_function.flatten_result_values([[1, 2], 3]) == [1, 2, 3]
    assert python_function.flatten_result_values([1, [2, 3]]) == [1, 2, 3]
    assert python_function.flatten_result_values([1, 2, 3]) == [1, 2, 3]
    assert python_function.flatten_result_values(42) == [42]
    assert python_function.flatten_result_values([]) == []

    # _result_as_spill_grid:
    # [[1, 2], 3] -> [[1, 2], [3]]
    assert python_function._result_as_spill_grid([[1, 2], 3]) == [[1, 2], [3]]
    # [[1, 2], "hello"] -> [[1, 2], ["hello"]] (does not split strings into chars)
    assert python_function._result_as_spill_grid([[1, 2], "hello"]) == [[1, 2], ["hello"]]
    assert python_function._result_as_spill_grid([1, 2, 3]) == [[1], [2], [3]]
    assert python_function._result_as_spill_grid([]) == []



def test_get_python_init_kwargs_off_main_returns_empty_without_touching_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off-main the caller doc is only passed through: its init script is UNO."""

    def boom(_doc: object) -> dict[str, Any]:
        raise AssertionError("must not read document scripts off-main")

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", boom)
    assert python_function.get_python_init_kwargs(MagicMock(), doc=CalcDocStub()) == {}


def test_get_python_init_kwargs_without_doc_does_not_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """No caller document means no init kwargs; the front window is not consulted."""

    def boom(_ctx: object) -> object:
        raise AssertionError("must not look up the front window")

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr("plugin.scripting.document_scripts.get_calc_document_from_ctx", boom)
    assert python_function.get_python_init_kwargs(MagicMock()) == {}


def test_get_python_init_kwargs_registers_unload_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///fake_lifecycle.ods")
    calls: list[tuple] = []

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda _doc: {"dummy": True})
    monkeypatch.setattr(
        "plugin.calc.python.workbook_lifecycle.ensure_calc_workbook_unload_resets_python",
        lambda ctx, workbook: calls.append((ctx, workbook)),
    )

    ctx = MagicMock()
    kwargs = python_function.get_python_init_kwargs(ctx, doc=doc)
    assert kwargs == {"dummy": True}
    assert calls == [(ctx, doc)]


def test_get_python_init_kwargs_survives_listener_install_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///fake_lifecycle_fail.ods")

    def _boom(_ctx, _doc):
        raise RuntimeError("listener failed")

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda _doc: {"dummy": True})
    monkeypatch.setattr(
        "plugin.calc.python.workbook_lifecycle.ensure_calc_workbook_unload_resets_python",
        _boom,
    )
    assert python_function.get_python_init_kwargs(MagicMock(), doc=doc) == {"dummy": True}


def test_py_scoped_dir_bindings_none_doc() -> None:
    assert python_function._py_scoped_dir_bindings(None) == {"scoped_dir": None}


def test_py_scoped_dir_bindings_off_main_skips_document_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yellow / #402: off-main recalc must not call getURL() on the caller doc."""
    called: list[int] = []

    def boom(doc: object) -> str:
        called.append(1)
        raise AssertionError("must not touch UNO off-main")

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr("plugin.doc.document_research.get_document_directory", boom)
    assert python_function._py_scoped_dir_bindings(object()) == {"scoped_dir": None}
    assert called == []


def test_py_scoped_dir_bindings_on_main_uses_document_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(
        "plugin.doc.document_research.get_document_directory",
        lambda doc: "/tmp/workbook-dir",
    )
    assert python_function._py_scoped_dir_bindings(object()) == {"scoped_dir": "/tmp/workbook-dir"}


def test_execute_python_addin_binds_scoped_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    python_function.clear_python_addin_cache()
    captured: dict[str, Any] = {}

    def fake_run(*_a: object, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "ok", "result": 1.0}

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(
        "plugin.doc.document_research.get_document_directory",
        lambda doc: "/folder",
    )
    monkeypatch.setattr(python_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda *_a, **_k: None)
    doc = CalcDocStub()
    out = python_function.execute_python_addin(_ctx_with_doc(doc), "1+1", doc=doc)
    assert out == 1.0
    assert captured.get("bindings") == {"scoped_dir": "/folder"}


def test_execute_python_addin_reuses_session_key_sid(monkeypatch: pytest.MonkeyPatch) -> None:
    python_function.clear_python_addin_cache()
    captured: dict[str, Any] = {}

    def fake_run(*_a: object, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "ok", "result": 1.0}

    session_calls = 0
    current_sid: str | None = "calc:file:///once.ods"

    def mock_session_id(*_a: object, **_k: object) -> str | None:
        nonlocal session_calls
        session_calls += 1
        return current_sid

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(python_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(python_function, "workbook_session_id", mock_session_id)
    monkeypatch.setattr("plugin.scripting.session_manager.workbook_session_id", mock_session_id)

    doc = CalcDocStub()
    out = python_function.execute_python_addin(_ctx_with_doc(doc), "1+1", doc=doc)
    assert out == 1.0
    assert session_calls == 1
    assert captured.get("session_id") == "calc:file:///once.ods"

    python_function.clear_python_addin_cache()
    captured.clear()
    session_calls = 0
    current_sid = None
    out2 = python_function.execute_python_addin(_ctx_with_doc(doc), "2+2", doc=doc)
    assert out2 == 1.0
    assert session_calls == 1
    assert captured.get("session_id") is None


def test_execute_python_addin_off_main_passes_doc_through_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off-main the caller doc yields no session id, init kwargs, or folder (all UNO)."""
    python_function.clear_python_addin_cache()
    captured: dict[str, Any] = {}

    def fake_run(*_a: object, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "ok", "result": 1.0}

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not derive a session off-main")

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr(python_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "workbook_session_id", boom)
    out = python_function.execute_python_addin(_ctx_with_doc(None), "result=1", doc=CalcDocStub())
    assert out == 1.0
    assert captured.get("bindings") == {"scoped_dir": None}
    assert captured.get("session_id") is None
    assert "init_script" not in captured


def test_perform_deferred_spill_fallback_to_passed_sheet_when_getbyname_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When doc.getSheets().getByName(sheet_name) raises, the passed sheet object receives the spill."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    doc = CalcDocStub(url="file:///spill_rename.ods")

    def boom_get_by_name(name: str) -> None:
        raise KeyError(f"Sheet {name} not found")

    monkeypatch.setattr(doc.getSheets(), "getByName", boom_get_by_name)

    from plugin.tests.testing_utils import CalcSheetStub

    passed_sheet = CalcSheetStub(name="SheetRenamed")
    origin_cell = passed_sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("result = [[1, 2], [3, 4]]")')

    grid = [[1, 2], [3, 4]]
    ctx = _ctx_with_doc(doc)

    python_function.perform_deferred_spill(
        ctx,
        "file:///spill_rename.ods",
        "Sheet1",
        0,
        0,
        grid,
        doc=doc,
        sheet=passed_sheet,
        code="result = [[1, 2], [3, 4]]",
    )

    assert passed_sheet.getCellByPosition(1, 0).getValue() == 2.0
    assert passed_sheet.getCellByPosition(0, 1).getValue() == 3.0
    assert passed_sheet.getCellByPosition(1, 1).getValue() == 4.0


def test_spill_remap_when_block_moves_with_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-row-insert state: formula at row 5, spilled values at rows 6-8, registry at row 4. modified() remaps key without clearing."""
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///remap.ods")
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: doc)

    code = "result = [1, 2, 3]"
    formula = f'=PY("{code}")'
    # Formula is now at (col 0, row 5)
    sheet.getCellByPosition(0, 5).setFormula(formula)
    # Values 1, 2, 3 at rows 6, 7, 8
    sheet.getCellByPosition(0, 6).setValue(1.0)
    sheet.getCellByPosition(0, 7).setValue(2.0)
    sheet.getCellByPosition(0, 8).setValue(3.0)

    # Old registry entry at row 4
    old_key = ("file:///remap.ods", "Sheet1", 4, 0)
    python_function.SPILL_REGISTRY[old_key] = python_function.SpillRecord(
        code=code,
        cells=[
            python_function.SpillCell(5, 0, "n", 1.0),
            python_function.SpillCell(6, 0, "n", 2.0),
            python_function.SpillCell(7, 0, "n", 3.0),
        ],
    )

    listener = python_function.CalcSpillModifyListener(MagicMock(), "file:///remap.ods", "Sheet1")
    listener.modified(SimpleNamespace(Source=sheet))

    new_key = ("file:///remap.ods", "Sheet1", 5, 0)
    assert old_key not in python_function.SPILL_REGISTRY
    assert new_key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[new_key].spills == [(6, 0), (7, 0), (8, 0)]
    assert sheet.getCellByPosition(0, 6).getValue() == 1.0
    assert sheet.getCellByPosition(0, 7).getValue() == 2.0
    assert sheet.getCellByPosition(0, 8).getValue() == 3.0


def test_spill_clear_does_not_follow_cut_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    """Formula moved to another column (col 3), spill values still at old col (col 0). Listener clears old coordinates and does not write destination col into registry."""
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///cutpaste.ods")
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: doc)

    code = "result = [1, 2]"
    # Formula cut-pasted to col 3 row 4
    sheet.getCellByPosition(3, 4).setFormula(f'=PY("{code}")')
    # Old formula cell at col 0 row 4 is now empty
    sheet.getCellByPosition(0, 4).setFormula("")
    # Old spill values still at col 0 row 5
    sheet.getCellByPosition(0, 5).setValue(2.0)

    old_key = ("file:///cutpaste.ods", "Sheet1", 4, 0)
    python_function.SPILL_REGISTRY[old_key] = python_function.SpillRecord(
        code=code,
        cells=[python_function.SpillCell(5, 0, "n", 2.0)],
    )

    listener = python_function.CalcSpillModifyListener(MagicMock(), "file:///cutpaste.ods", "Sheet1")
    listener.modified(SimpleNamespace(Source=sheet))

    assert old_key not in python_function.SPILL_REGISTRY
    assert ("file:///cutpaste.ods", "Sheet1", 4, 3) not in python_function.SPILL_REGISTRY
    # Spilled cell at (col 0, row 5) was cleared
    assert sheet.getCellByPosition(0, 5).getValue() in (0, 0.0)


def test_spill_clear_skips_formula_cell_and_edited_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """One spill cell is now =SUM(1) (not cleared), one value was edited (not cleared), one still matches (cleared)."""
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///skip.ods")
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: doc)

    # Formula at (0, 0) is deleted
    sheet.getCellByPosition(0, 0).setFormula("")

    # Cell (0, 1): now has =SUM(1)
    sheet.getCellByPosition(0, 1).setFormula("=SUM(1)")

    # Cell (0, 2): edited from 2.0 to 999.0
    sheet.getCellByPosition(0, 2).setValue(999.0)

    # Cell (0, 3): still matches 3.0
    sheet.getCellByPosition(0, 3).setValue(3.0)

    key = ("file:///skip.ods", "Sheet1", 0, 0)
    python_function.SPILL_REGISTRY[key] = python_function.SpillRecord(
        code="result = [1, 2, 3]",
        cells=[
            python_function.SpillCell(1, 0, "n", 1.0),
            python_function.SpillCell(2, 0, "n", 2.0),
            python_function.SpillCell(3, 0, "n", 3.0),
        ],
    )

    listener = python_function.CalcSpillModifyListener(MagicMock(), "file:///skip.ods", "Sheet1")
    listener.modified(SimpleNamespace(Source=sheet))

    assert key not in python_function.SPILL_REGISTRY
    # Formula cell preserved
    assert sheet.getCellByPosition(0, 1).getFormula() == "=SUM(1)"
    # Edited cell preserved
    assert sheet.getCellByPosition(0, 2).getValue() == 999.0
    # Matching cell cleared
    assert sheet.getCellByPosition(0, 3).getValue() in (0, 0.0)


def test_spill_clear_skips_user_text_matching_numeric_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spilled 0.0 does not clear neighbor setString('note'), while neighbor setValue(0.0) is cleared."""
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///text_zero.ods")
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: doc)

    # Formula cell cleared
    sheet.getCellByPosition(0, 0).setFormula("")

    # Neighbor 1 has user text "note"
    sheet.getCellByPosition(0, 1).setString("note")

    # Neighbor 2 has numeric 0.0
    sheet.getCellByPosition(0, 2).setValue(0.0)

    key = ("file:///text_zero.ods", "Sheet1", 0, 0)
    python_function.SPILL_REGISTRY[key] = python_function.SpillRecord(
        code="result = [0.0, 0.0]",
        cells=[
            python_function.SpillCell(1, 0, "n", 0.0),
            python_function.SpillCell(2, 0, "n", 0.0),
        ],
    )

    listener = python_function.CalcSpillModifyListener(MagicMock(), "file:///text_zero.ods", "Sheet1")
    listener.modified(SimpleNamespace(Source=sheet))

    assert key not in python_function.SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 1).getString() == "note"
    assert sheet.getCellByPosition(0, 2).getType() == 0
    assert sheet.getCellByPosition(0, 2).getString() == ""


def test_cell_matches_stored_value_type_gate() -> None:
    """Direct _cell_matches_stored_value cases for VALUE/TEXT gate and PyUNO enum compatibility."""
    from plugin.calc.python.function import SpillCell, _cell_matches_stored_value
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    sheet.getCellByPosition(0, 0).setValue(0.0)
    assert _cell_matches_stored_value(sheet, 0, 0, SpillCell(0, 0, "n", 0.0)) is True

    sheet.getCellByPosition(0, 1).setString("0.0")
    assert _cell_matches_stored_value(sheet, 1, 0, SpillCell(1, 0, "n", 0.0)) is False

    class _PyUnoEnum:
        def __init__(self, value: str) -> None:
            self.value = value

        def __eq__(self, other: object) -> bool:
            return isinstance(other, _PyUnoEnum) and self.value == other.value

    class _StandInCell:
        def __init__(self, enum_val: str, value: float = 0.0, text: str = "") -> None:
            self._type = _PyUnoEnum(enum_val)
            self._value = value
            self._text = text

        def getType(self) -> _PyUnoEnum:
            return self._type

        def getValue(self) -> float:
            return self._value

        def getString(self) -> str:
            return self._text

    class _StandInSheet:
        def __init__(self, cell: _StandInCell) -> None:
            self._cell = cell

        def getCellByPosition(self, _col: int, _row: int) -> _StandInCell:
            return self._cell

    # Stand-in whose getType returns object with .value == "VALUE" and __eq__ false for ints
    val_cell = _StandInCell("VALUE", value=123.0, text="123.0")
    val_sheet = _StandInSheet(val_cell)
    assert _cell_matches_stored_value(val_sheet, 0, 0, SpillCell(0, 0, "n", 123.0)) is True
    assert _cell_matches_stored_value(val_sheet, 0, 0, SpillCell(0, 0, "s", "123.0")) is False

    # The same stand-in with .value == "TEXT" and getString() equal to the stored string matches kind "s"
    text_cell = _StandInCell("TEXT", value=0.0, text="hello")
    text_sheet = _StandInSheet(text_cell)
    assert _cell_matches_stored_value(text_sheet, 0, 0, SpillCell(0, 0, "s", "hello")) is True

    # A VALUE stand-in whose getString() equals a stored string does not match kind "s"
    val_with_str = _StandInCell("VALUE", value=0.0, text="target")
    val_str_sheet = _StandInSheet(val_with_str)
    assert _cell_matches_stored_value(val_str_sheet, 0, 0, SpillCell(0, 0, "s", "target")) is False


def test_overflow_int_becomes_decimal_text() -> None:
    """to_calc_compatible(10**400) == str(10**400) and the same through _coerce_spill_value. to_calc_compatible(float('nan')) stays NaN."""
    huge = 10**400
    huge_str = str(huge)
    assert python_function.to_calc_compatible(huge) == huge_str

    val, meta = python_function._coerce_spill_value(huge)
    assert val == huge_str
    assert meta == {"is_temporal": False, "is_empty": False}

    nan_val = python_function.to_calc_compatible(float("nan"))
    assert math.isnan(nan_val)


def test_selection_does_not_suppress_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """CalcDocStub selection is a multi-cell range, the located formula cell is a single cell, finalize_python_return still spills. Fallback does not spill."""
    from unittest.mock import patch

    doc = CalcDocStub(url="file:///sel_spill.ods", selection="A1:D10")
    sheet = doc.getSheets().getByName("Sheet1")
    formula_cell = sheet.getCellByPosition(0, 0)
    formula_cell.setFormula('=PY("code_single")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    _install_immediate_spill_timer(monkeypatch)

    with patch(
        "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
        return_value=(sheet, formula_cell, (0, 0)),
    ):
        val = python_function.finalize_python_return(ctx, "code_single", [10, 20, 30], doc=doc)

    assert val == 10
    assert sheet.getCellByPosition(0, 1).getValue() == 20
    key = ("file:///sel_spill.ods", "Sheet1", 0, 0)
    assert key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[key].spills == [(1, 0), (2, 0)]

    # Second case: locate returns None, multi-cell selection falls back to no spill (matrix mode)
    with patch(
        "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
        return_value=None,
    ):
        val_fallback = python_function.finalize_python_return(ctx, "code_fallback", [10, 20, 30], doc=doc)
    # When is_matrix is true, returns scalar without queuing spill
    assert val_fallback == 10


def test_indexed_multi_data_ignores_my_data() -> None:
    """_code_uses_indexed_multi_data('my_data[0]') is false; 'data[0]' and 'ranges[1]' and 'note = \"data[0]\"' are true."""
    assert not python_function._code_uses_indexed_multi_data("my_data[0]")
    assert not python_function._code_uses_indexed_multi_data("_data[0]")
    assert not python_function._code_uses_indexed_multi_data("obj.data[0]")
    assert not python_function._code_uses_indexed_multi_data("self.ranges[0]")
    assert python_function._code_uses_indexed_multi_data("data[0]")
    assert python_function._code_uses_indexed_multi_data("ranges[1]")
    assert python_function._code_uses_indexed_multi_data('note = "data[0]"')
    assert python_function._code_uses_indexed_multi_data("(data[0])")


def test_matrix_sessions_expire() -> None:
    """Plant an entry with timestamp older than _PY_PASS_GAP_SEC, call scalar_for_list_result for a different key, assert old key is gone."""
    import time
    from unittest.mock import patch

    python_function.clear_python_addin_cache()
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    old_time = time.monotonic() - (python_function._PY_PASS_GAP_SEC + 5.0)

    stale_key = ("tid_old", ("file:///d.ods", "S", "sid", "c", "1,1"), "token_old")
    with python_function._MATRIX_SCALAR_SESSIONS_LOCK:
        python_function._MATRIX_SCALAR_SESSIONS[stale_key] = python_function.WorkerResultSession(
            [1], [1], timestamp=old_time
        )

    # Call scalar_for_list_result for a different formula with a unique origin
    with patch(
        "plugin.calc.python.function.session_key",
        return_value=("file:///d.ods", "S", "sid", "c_new", "2,2"),
    ):
        python_function.scalar_for_list_result(ctx, "c_new", [10, 20], doc=doc)

    with python_function._MATRIX_SCALAR_SESSIONS_LOCK:
        assert stale_key not in python_function._MATRIX_SCALAR_SESSIONS


def test_second_spill_timer_cancels_the_first() -> None:
    """Two _register_spill_timer calls with the same lifecycle and token; the first timer's cancel was called and only the second remains. A different token does not cancel the first."""
    _clear_spill_timer_registry()

    t1 = MagicMock()
    t1.finished.is_set.return_value = False
    t2 = MagicMock()
    t2.finished.is_set.return_value = False
    t3 = MagicMock()
    t3.finished.is_set.return_value = False

    python_function._register_spill_timer("life1", t1, token="Sheet1:0:0")
    # Same lifecycle and token -> t1 cancelled, t2 stored
    python_function._register_spill_timer("life1", t2, token="Sheet1:0:0")
    t1.cancel.assert_called_once()
    t2.cancel.assert_not_called()

    # Different token -> does not cancel t2
    python_function._register_spill_timer("life1", t3, token="Sheet1:1:0")
    t2.cancel.assert_not_called()
    t3.cancel.assert_not_called()

    active_timers = [t for _k, t in python_function._PENDING_SPILL_TIMERS]
    assert t1 not in active_timers
    assert t2 in active_timers
    assert t3 in active_timers


def test_calc_formula_error_double_and_tokens() -> None:
    tokens = [
        ("#VALUE!", 519),
        ("  #VALUE!  ", 519),
        ("#NUM!", 503),
        ("#DIV/0!", 532),
        ("#N/A", 32767),
        ("#N/A N/A", 32767),
        ("#REF!", 524),
        ("#NAME?", 525),
        ("#NULL!", 521),
    ]
    for token, expected_code in tokens:
        val = python_function.py_addin_cell_value(token)
        assert isinstance(val, float)
        assert not math.isfinite(val)
        raw = struct.unpack("Q", struct.pack("d", val))[0]
        assert raw & 0xFFFF == expected_code
        assert raw == (0x7FF8000000000000 | expected_code)
        assert python_function.calc_error_code_for_token(token) == expected_code

    # Direct calc_formula_error_double
    for _token, code in tokens:
        d = python_function.calc_formula_error_double(code)
        raw = struct.unpack("Q", struct.pack("d", d))[0]
        assert raw & 0xFFFF == code
        assert raw == (0x7FF8000000000000 | code)

    # Strings that stay str
    for non_err in ["hello", "#value!", "#VALUE! extra", "#SPILL!", "Error: Code execution failed"]:
        val = python_function.py_addin_cell_value(non_err)
        assert val == non_err
        assert isinstance(val, str)
        assert python_function.calc_error_code_for_token(non_err) is None

    # None, nan, inf
    assert python_function.py_addin_cell_value(None) == ""
    nan_val = python_function.py_addin_cell_value(float("nan"))
    assert isinstance(nan_val, float) and math.isnan(nan_val)
    assert struct.unpack("Q", struct.pack("d", nan_val))[0] & 0xFFFF == 0
    inf_val = python_function.py_addin_cell_value(float("inf"))
    assert inf_val == float("inf")
    ninf_val = python_function.py_addin_cell_value(float("-inf"))
    assert ninf_val == float("-inf")

    # Tuple maps only first element when it has error token
    tup = python_function.py_addin_cell_value(("#DIV/0!", 1.0, "ok"))
    assert isinstance(tup, tuple)
    first_raw = struct.unpack("Q", struct.pack("d", tup[0]))[0]
    assert first_raw & 0xFFFF == 532
    assert tup[1] == 1.0
    assert tup[2] == "ok"


def test_worker_exception_error_code_and_stays_text() -> None:
    cases = [
        ("Code execution failed at line '1/0' due to: ZeroDivisionError: division by zero", 532),
        ("Code execution failed at line 'x' due to: OverflowError: math range error", 503),
        ("Code execution failed at line 'x' due to: FloatingPointError: underflow", 503),
        ("Code execution failed at line 'x' due to: NameError: name 'nope' is not defined", 525),
        ("Code execution failed at line 'x' due to: UnboundLocalError: local variable referenced before assignment", 525),
        ("Code execution failed at line 'x' due to: ValueError: invalid literal", 519),
        ("Code execution failed at line 'x' due to: IndexError: list index out of range", 519),
        ("Code execution failed at line 'x' due to: NotImplementedError: bahttext", 519),
        ("Code execution failed at line 'x' due to: ValueError: connection timeout while reading", 519),
        ("Code execution failed at line 'x' due to: NameError: nope due to: ZeroDivisionError: div zero", 532),
        ("Unknown worker error", 519),
    ]
    for msg, expected_code in cases:
        assert python_function.worker_exception_error_code(msg) == expected_code

    # _worker_failure_stays_text
    assert python_function._worker_failure_stays_text({"code": "USER_STOPPED", "message": "Stopped by user."}) is True
    assert python_function._worker_failure_stays_text({"status": "error", "message": "No python executable found under configured venv: '/missing'"}) is True
    assert python_function._worker_failure_stays_text({"status": "error", "message": "Python worker failed: timed out after 10 seconds"}) is True
    assert python_function._worker_failure_stays_text({"status": "error", "message": "Code execution failed at line 'x' due to: ValueError: connection timeout"}) is False


def test_execute_python_addin_worker_errors(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    python_function.clear_python_addin_cache()
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda *_a, **_k: None)

    # 1. ValueError-shaped worker dict -> the 519 double, not a string
    rec_diag = MagicMock()
    monkeypatch.setattr(python_function, "_record_py_diagnostic", rec_diag)
    err_msg = "Code execution failed at line 'bad' due to: ValueError: invalid literal"
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "error", "message": err_msg},
    )
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.function"):
        out = python_function.execute_python_addin(ctx, "bad_code", doc=doc)
    assert isinstance(out, float)
    raw = struct.unpack("Q", struct.pack("d", out))[0]
    assert raw & 0xFFFF == 519
    assert "Code execution failed" in caplog.text
    assert "ValueError" in caplog.text
    rec_diag.assert_called_with(ctx, "bad_code", {"status": "error", "message": err_msg}, status="error", message=err_msg, doc=doc)

    # 2. ZeroDivisionError-shaped dict -> 532
    div_msg = "Code execution failed at line '1/0' due to: ZeroDivisionError: division by zero"
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "error", "message": div_msg},
    )
    out_div = python_function.execute_python_addin(ctx, "1/0", doc=doc)
    assert isinstance(out_div, float)
    raw_div = struct.unpack("Q", struct.pack("d", out_div))[0]
    assert raw_div & 0xFFFF == 532

    # 3. Existing timeout and venv-not-found tests still expect a string containing Settings
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "error", "message": "No python executable found under configured venv: '/missing'"},
    )
    out_venv = python_function.execute_python_addin(ctx, "venv_code", doc=doc)
    assert isinstance(out_venv, str) and "Settings" in out_venv

    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "error", "message": "Python worker failed: timed out after 10 seconds"},
    )
    out_to = python_function.execute_python_addin(ctx, "to_code", doc=doc)
    assert isinstance(out_to, str) and "timed out" in out_to.lower()

    # 4. code="USER_STOPPED", message Stopped by user. -> a string, no ERROR log
    caplog.clear()
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user."},
    )
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.function"):
        out_stop = python_function.execute_python_addin(ctx, "stop_code", doc=doc)
    assert isinstance(out_stop, str)
    assert "Stopped" in out_stop or "USER_STOPPED" in out_stop
    assert caplog.text == ""

    # 5. Monkeypatch run_code_in_user_venv to raise RuntimeError("host blew up") -> still a string from _format_error_for_display
    def _raise_runtime(*_a, **_k):
        raise RuntimeError("host blew up")
    monkeypatch.setattr(python_function, "run_code_in_user_venv", _raise_runtime)
    out_exc = python_function.execute_python_addin(ctx, "exc_code", doc=doc)
    assert isinstance(out_exc, str)
    assert "host blew up" in out_exc

    # 6. Successful result="#N/A" -> the 32767 double
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "ok", "result": "#N/A"},
    )
    out_na = python_function.execute_python_addin(ctx, "na_code", doc=doc)
    assert isinstance(out_na, float)
    raw_na = struct.unpack("Q", struct.pack("d", out_na))[0]
    assert raw_na & 0xFFFF == 32767

    # 7. Successful result=["#NUM!", 2] is a column spill; worker status: error does not write SPILL_REGISTRY
    python_function.SPILL_REGISTRY.clear()
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "error", "message": err_msg},
    )
    python_function.execute_python_addin(ctx, "spill_fail_code", doc=doc)
    assert len(python_function.SPILL_REGISTRY) == 0

    class DummyTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function
            self.args = args
            self.kwargs = kwargs
        def start(self):
            self.function(*self.args, **self.kwargs)

    monkeypatch.setattr(python_function.threading, "Timer", DummyTimer)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: fn(*a, **k),
    )
    doc_spill = CalcDocStub(url="file:///fake_col_addin.ods", selection="A1")
    sheet_spill = doc_spill.getSheets().getByName("Sheet1")
    sheet_spill.getCellByPosition(0, 0).setFormula('=PYTHON("num_spill_code")')
    ctx_spill = _ctx_with_doc(doc_spill)
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "ok", "result": ["#NUM!", 2]},
    )
    out_spill = python_function.execute_python_addin(ctx_spill, "num_spill_code", doc=doc_spill)
    assert isinstance(out_spill, float)
    assert struct.unpack("Q", struct.pack("d", out_spill))[0] & 0xFFFF == 503
    key_spill = ("file:///fake_col_addin.ods", "Sheet1", 0, 0)
    assert key_spill in python_function.SPILL_REGISTRY


def test_py_addin_cell_value_helper_error_dict(caplog: pytest.LogCaptureFixture) -> None:
    from plugin.calc.analysis_egress import format_analysis_for_calc
    from plugin.calc.quant_egress import format_quant_for_calc

    quick = {
        "status": "error",
        "code": "UNKNOWN_COLUMN",
        "message": "Unknown columns: Region",
        "helper": "quick_stats",
    }
    # Egress formatters keep the dict as text rows. to_calc_compatible still stringifies it.
    assert format_analysis_for_calc(quick) == [["Analysis error (UNKNOWN_COLUMN)"], ["Unknown columns: Region"]]
    assert format_quant_for_calc(quick) == [["Quant error (UNKNOWN_COLUMN)"], ["Unknown columns: Region"]]
    assert to_calc_compatible(quick) == str(quick)
    assert isinstance(to_calc_compatible(quick), str)

    mapped = [
        (quick, 519, "quick_stats: UNKNOWN_COLUMN: Unknown columns: Region"),
        ({"status": "error", "code": "INVALID_PARAM", "message": "bad"}, 519, "INVALID_PARAM: bad"),
        (
            {
                "status": "error",
                "code": "INVALID_PARAM",
                "message": "bad",
                "helper": "describe_data",
                "details": {"col": "A"},
            },
            519,
            "describe_data: INVALID_PARAM: bad",
        ),
        (
            {
                "status": "error",
                "code": "MISSING_PACKAGE",
                "message": "scipy is required for detect_outliers.",
                "helper": "detect_outliers",
            },
            519,
            "detect_outliers: MISSING_PACKAGE: scipy is required for detect_outliers.",
        ),
        ({"status": "error", "code": "#VALUE!", "message": "x"}, 519, "#VALUE!: x"),
        ({"status": "error", "code": "#NUM!", "message": "x"}, 503, "#NUM!: x"),
        ({"status": "error", "code": "#DIV/0!", "message": "x"}, 532, "#DIV/0!: x"),
        ({"status": "error", "code": "  #DIV/0!  ", "message": "x"}, 532, "#DIV/0!: x"),
        ({"status": "error", "code": "#N/A", "message": "x"}, 32767, "#N/A: x"),
        ({"status": "error", "code": "#N/A N/A", "message": "x"}, 32767, "#N/A N/A: x"),
        ({"status": "error", "code": "#REF!", "message": "x"}, 524, "#REF!: x"),
        ({"status": "error", "code": "#NAME?", "message": "x"}, 525, "#NAME?: x"),
        ({"status": "error", "code": "#NULL!", "message": "x"}, 521, "#NULL!: x"),
        ({"status": "error", "code": "ZeroDivisionError", "message": "x"}, 532, "ZeroDivisionError: x"),
        ({"status": "error", "code": "OverflowError", "message": "x"}, 503, "OverflowError: x"),
        ({"status": "error", "code": "FloatingPointError", "message": "x"}, 503, "FloatingPointError: x"),
        ({"status": "error", "code": "NameError", "message": "x"}, 525, "NameError: x"),
        ({"status": "error", "code": "UnboundLocalError", "message": "x"}, 525, "UnboundLocalError: x"),
        ({"status": "error", "code": "ValueError", "message": "x"}, 519, "ValueError: x"),
        (
            {
                "status": "error",
                "code": "INVALID_PARAM",
                "message": "failed due to: ZeroDivisionError: div",
                "helper": "describe_data",
            },
            519,
            "describe_data: INVALID_PARAM: failed due to: ZeroDivisionError: div",
        ),
    ]
    for err, bits, logged in mapped:
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="plugin.calc.python.function"):
            val = python_function.py_addin_cell_value(err)
        assert isinstance(val, float)
        assert not math.isfinite(val)
        assert struct.unpack("Q", struct.pack("d", val))[0] & 0xFFFF == bits
        assert logged in caplog.text
        assert "=PY() worker failure" in caplog.text

    stay_text = [
        {"a": 1, "b": 2},
        {"status": "ok", "helper": "describe_data", "metrics": {"n": 1}},
        {"status": "error", "message": "nope"},
        {"status": "error", "code": "UNKNOWN_COLUMN"},
        {"status": "error", "code": "", "message": "x"},
        {"status": "error", "code": "   ", "message": "x"},
        {"status": "error", "code": "UNKNOWN_COLUMN", "message": "Unknown columns: Region", "rows": [1]},
        {
            "status": "error",
            "code": "TEXT_ANALYTICS_ERROR",
            "helper": "topics",
            "message": "install scikit-learn for topics",
            "result": {"error": "MISSING_PACKAGE"},
        },
        {"status": "Error", "code": "X", "message": "Y"},
        {"status": "error", "code": 519, "message": "Y"},
        {"status": "error", "code": "X", "message": None},
        {"status": "error", "code": "X", "message": "Y", "helper": ""},
        {"status": "error", "code": "X", "message": "Y", "helper": "   "},
        {"status": "error", "code": "X", "message": "Y", "details": "nope"},
        {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user.", "stdout": ""},
    ]
    for err in stay_text:
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="plugin.calc.python.function"):
            val = python_function.py_addin_cell_value(err)
        assert val == str(err)
        assert isinstance(val, str)
        assert caplog.text == ""


def test_execute_python_addin_helper_error_dict(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    python_function.clear_python_addin_cache()
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda *_a, **_k: None)
    rec_diag = MagicMock()
    monkeypatch.setattr(python_function, "_record_py_diagnostic", rec_diag)

    err = {
        "status": "error",
        "code": "UNKNOWN_COLUMN",
        "message": "Unknown columns: Region",
        "helper": "quick_stats",
    }
    worker = {"status": "ok", "result": err}
    monkeypatch.setattr(python_function, "run_code_in_user_venv", lambda *_a, **_k: worker)
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.function"):
        out = python_function.execute_python_addin(ctx, "helper_err", doc=doc)
    assert isinstance(out, float)
    assert struct.unpack("Q", struct.pack("d", out))[0] & 0xFFFF == 519
    assert "quick_stats: UNKNOWN_COLUMN: Unknown columns: Region" in caplog.text
    assert caplog.text.count("=PY() worker failure") == 1
    rec_diag.assert_called_once_with(
        ctx,
        "helper_err",
        worker,
        status="error",
        message="quick_stats: UNKNOWN_COLUMN: Unknown columns: Region",
        doc=doc,
    )

    # Message text that would stay a Settings string on the worker-status path is still #VALUE! here.
    caplog.clear()
    rec_diag.reset_mock()
    timed = {
        "status": "error",
        "code": "INVALID_PARAM",
        "message": "connection timed out",
        "helper": "describe_data",
    }
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "ok", "result": timed},
    )
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.function"):
        out_timed = python_function.execute_python_addin(ctx, "helper_timeout_word", doc=doc)
    assert isinstance(out_timed, float)
    assert struct.unpack("Q", struct.pack("d", out_timed))[0] & 0xFFFF == 519
    assert "describe_data: INVALID_PARAM: connection timed out" in caplog.text

    # Ordinary dict stays text. Diagnostic is the ok record, not an error record.
    caplog.clear()
    rec_diag.reset_mock()
    plain = {"hello": "world"}
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "ok", "result": plain},
    )
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.function"):
        out_plain = python_function.execute_python_addin(ctx, "plain_dict", doc=doc)
    assert isinstance(out_plain, str)
    assert "hello" in out_plain
    assert caplog.text == ""
    rec_diag.assert_called_once_with(ctx, "plain_dict", {"status": "ok", "result": plain}, status="ok", doc=doc)

    # Worker USER_STOPPED is still the text path (status on the envelope, not a helper result).
    caplog.clear()
    rec_diag.reset_mock()
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user."},
    )
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.function"):
        out_stop = python_function.execute_python_addin(ctx, "stop_still_text", doc=doc)
    assert isinstance(out_stop, str)
    assert "Stopped" in out_stop
    assert caplog.text == ""


def test_flat_result_equal_and_scalar_matrix_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two payload NaNs with the same bits compare equal
    nan519_a = python_function.calc_formula_error_double(519)
    nan519_b = python_function.calc_formula_error_double(519)
    nan532 = python_function.calc_formula_error_double(532)
    assert python_function._flat_result_equal((nan519_a,), (nan519_b,)) is True
    assert python_function._flat_result_equal((nan519_a,), (nan532,)) is False

    # float("nan") compares equal to float("nan")
    assert python_function._flat_result_equal((float("nan"),), (float("nan"),)) is True
    # 1.0 equals 1.0
    assert python_function._flat_result_equal((1.0,), (1.0,)) is True
    # #VALUE! mapped values of 519 equal each other
    val519_a = python_function.py_addin_cell_value("#VALUE!")
    val519_b = python_function.py_addin_cell_value("#VALUE!")
    assert python_function._flat_result_equal((val519_a,), (val519_b,)) is True
    val503 = python_function.py_addin_cell_value("#NUM!")
    assert python_function._flat_result_equal((val519_a,), (val503,)) is False

    # Monkeypatch session_key to ("u", "Sheet1", "sid", code, "0,0")
    # Call scalar_for_list_result twice on ["#VALUE!", "#DIV/0!"]
    code = "test_matrix_session_err"
    monkeypatch.setattr(
        python_function,
        "session_key",
        lambda _ctx, _c, doc=None: ("u", "Sheet1", "sid", code, "0,0"),
    )
    ctx = MagicMock()
    try:
        ret1 = python_function.scalar_for_list_result(ctx, code, ["#VALUE!", "#DIV/0!"])
        ret2 = python_function.scalar_for_list_result(ctx, code, ["#VALUE!", "#DIV/0!"])
        assert isinstance(ret1, float)
        assert struct.unpack("Q", struct.pack("d", ret1))[0] & 0xFFFF == 519
        assert isinstance(ret2, float)
        assert struct.unpack("Q", struct.pack("d", ret2))[0] & 0xFFFF == 532
    finally:
        with python_function._MATRIX_SCALAR_SESSIONS_LOCK:
            python_function._MATRIX_SCALAR_SESSIONS.clear()


def test_spill_with_calc_error_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.doc_stubs import CalcRangeStub

    class DummyTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function
            self.args = args
            self.kwargs = kwargs
        def start(self):
            self.function(*self.args, **self.kwargs)

    monkeypatch.setattr(python_function.threading, "Timer", DummyTimer)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: fn(*a, **k),
    )
    python_function.LOADED_DOCUMENTS.clear()
    python_function.SPILL_REGISTRY.clear()

    recorded_set_data: list[tuple[Any, ...]] = []
    orig_set_data = CalcRangeStub.setDataArray

    def recording_set_data(self, data):
        recorded_set_data.append(data)
        return orig_set_data(self, data)

    monkeypatch.setattr(CalcRangeStub, "setDataArray", recording_set_data)

    # 1. Column [1, "#VALUE!", "#DIV/0!", "#N/A"]
    doc = CalcDocStub(url="file:///fake_col.ods", selection="A1")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PYTHON("col_err_test")')
    ctx = _ctx_with_doc(doc)

    col_result = [1, "#VALUE!", "#DIV/0!", "#N/A"]
    ret_corner = python_function.finalize_python_return(ctx, "col_err_test", col_result, doc=doc)
    assert ret_corner == 1.0
    assert sheet.getCellByPosition(0, 1).getFormula() == "=#ERR519!"
    assert sheet.getCellByPosition(0, 2).getFormula() == "=#ERR532!"
    # The #N/A slot in the recorded setDataArray data is None, and that cell is not given a formula
    assert sheet.getCellByPosition(0, 3).getFormula() == ""
    all_recorded_cells = [cell for call in recorded_set_data for row in call for cell in row]
    assert None in all_recorded_cells
    key_col = ("file:///fake_col.ods", sheet.getName(), 0, 0)
    assert key_col in python_function.SPILL_REGISTRY
    col_cells = python_function.SPILL_REGISTRY[key_col].cells
    assert len(col_cells) == 3
    assert col_cells[0].kind == "err" and col_cells[0].value == 519
    assert col_cells[1].kind == "err" and col_cells[1].value == 532
    assert col_cells[2].kind == "err" and col_cells[2].value == 32767

    # 2. 2x2 [[1, "#NUM!"], ["#REF!", "#NAME?"]]
    recorded_set_data.clear()
    python_function.SPILL_REGISTRY.clear()
    doc2 = CalcDocStub(url="file:///fake_2x2.ods", selection="A1")
    sheet2 = doc2.getSheets().getByName("Sheet1")
    sheet2.getCellByPosition(0, 0).setFormula('=PYTHON("grid_2x2_test")')
    ctx2 = _ctx_with_doc(doc2)

    grid_result = [[1, "#NUM!"], ["#REF!", "#NAME?"]]
    ret_corner2 = python_function.finalize_python_return(ctx2, "grid_2x2_test", grid_result, doc=doc2)
    assert ret_corner2 == 1.0
    assert sheet2.getCellByPosition(1, 0).getFormula() == "=#ERR503!"
    assert sheet2.getCellByPosition(0, 1).getFormula() == "=#ERR524!"
    assert sheet2.getCellByPosition(1, 1).getFormula() == "=#ERR525!"
    recorded_cells_2x2 = [cell for call in recorded_set_data for row in call for cell in row]
    assert all(c == "" for c in recorded_cells_2x2)

    # 3. [[ "#NULL!" ]] (one cell): return value is the 521 double. No neighbor formula.
    python_function.SPILL_REGISTRY.clear()
    doc3 = CalcDocStub(url="file:///fake_1x1.ods", selection="A1")
    sheet3 = doc3.getSheets().getByName("Sheet1")
    sheet3.getCellByPosition(0, 0).setFormula('=PYTHON("one_cell_test")')
    ctx3 = _ctx_with_doc(doc3)
    ret_corner3 = python_function.finalize_python_return(ctx3, "one_cell_test", [["#NULL!"]], doc=doc3)
    assert isinstance(ret_corner3, float)
    assert struct.unpack("Q", struct.pack("d", ret_corner3))[0] & 0xFFFF == 521
    assert sheet3.getCellByPosition(1, 0).getFormula() == ""
    assert sheet3.getCellByPosition(0, 1).getFormula() == ""

    # 4. Index path: finalize_python_return(..., ["a", "#NUM!"], index_arg=1) is the 503 double.
    # index_arg=5 on a 2-element list is still the Error: index … string.
    val_idx = python_function.finalize_python_return(ctx3, "idx_code", ["a", "#NUM!"], index_arg=1)
    assert isinstance(val_idx, float)
    assert struct.unpack("Q", struct.pack("d", val_idx))[0] & 0xFFFF == 503
    val_idx_err = python_function.finalize_python_return(ctx3, "idx_code", ["a", "#NUM!"], index_arg=5)
    assert isinstance(val_idx_err, str)
    assert "Error: index 5 out of range" in val_idx_err


def test_cell_matches_stored_value_err() -> None:
    fake_cell_519 = SimpleNamespace(getError=lambda: 519)
    fake_cell_no_err = SimpleNamespace()
    rec_519 = python_function.SpillCell(row=0, col=0, kind="err", value=519)

    class FakeSheet:
        def __init__(self, cell):
            self._cell = cell
        def getCellByPosition(self, col, row):
            return self._cell

    sheet_519 = FakeSheet(fake_cell_519)
    assert python_function._cell_matches_stored_value(sheet_519, 0, 0, rec_519) is True

    rec_532 = python_function.SpillCell(row=0, col=0, kind="err", value=532)
    assert python_function._cell_matches_stored_value(sheet_519, 0, 0, rec_532) is False

    sheet_no_err = FakeSheet(fake_cell_no_err)
    assert python_function._cell_matches_stored_value(sheet_no_err, 0, 0, rec_519) is False


def test_load_spill_registry_skips_malformed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    payload = json.dumps({
        "Sheet1:1,1": [[2, 1]],
        "Sheet1:x,1": [[0, 0]],
        "Sheet1:4,4": {"code": "later", "cells": [{"r": 5, "c": 4, "t": "n", "v": 1.0}]},
    })
    monkeypatch.setattr(
        "plugin.doc.udprops.get_document_property",
        lambda model, name, default=None: payload if name == "WriterAgentSpillRegistry" else default,
    )
    doc = CalcDocStub(url="file:///fake_malformed.ods")

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    python_function.load_spill_registry_for_doc(doc)
    doc_key = "file:///fake_malformed.ods"
    assert (doc_key, "Sheet1", 1, 1) in python_function.SPILL_REGISTRY
    assert (doc_key, "Sheet1", 4, 4) in python_function.SPILL_REGISTRY
    assert len([k for k in python_function.SPILL_REGISTRY if k[0] == doc_key]) == 2


def test_finalize_python_return_empty_row_does_not_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///empty-row.ods", selection="A1")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PYTHON("empty_row")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()
    with python_function._PENDING_SPILL_LOCK:
        python_function._PENDING_SPILL_TIMERS.clear()

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    val = finalize_python_return(ctx, "empty_row", [[]], doc=doc)
    assert val == ""
    assert ("file:///empty-row.ods", "Sheet1", 0, 0) not in python_function.SPILL_REGISTRY
    with python_function._PENDING_SPILL_LOCK:
        assert len(python_function._PENDING_SPILL_TIMERS) == 0

    # Repeat off the main thread
    queue_mock = MagicMock()
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr("plugin.calc.python.function._queue_off_main_auto_spill", queue_mock)
    val_off = finalize_python_return(ctx, "empty_row", [[]], doc=doc)
    assert val_off == ""
    queue_mock.assert_not_called()


def test_scalar_for_list_result_uses_supplied_data_token(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    python_function.clear_python_addin_cache()

    monkeypatch.setattr(
        "plugin.calc.python.function.session_key",
        lambda *args, **kwargs: ("file:///doc.ods", "Sheet1", "sid", "code_token", "0,0"),
    )
    token_calls = []

    def mock_token(data):
        token_calls.append(data)
        return "computed"

    monkeypatch.setattr("plugin.calc.python.function._worker_data_token", mock_token)

    res = python_function.scalar_for_list_result(ctx, "code_token", [10, 20], worker_data={"x": 1}, doc=doc, data_token="preset")
    assert res == 10
    assert len(token_calls) == 0
    with python_function._MATRIX_SCALAR_SESSIONS_LOCK:
        matching_keys = [k for k in python_function._MATRIX_SCALAR_SESSIONS if k[1][3] == "code_token"]
        assert len(matching_keys) == 1
        assert matching_keys[0][2] == "preset"


def test_execute_python_addin_hashes_worker_data_once(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    python_function.clear_python_addin_cache()

    monkeypatch.setattr(
        "plugin.calc.python.function.session_key",
        lambda *args, **kwargs: ("file:///doc.ods", "Sheet1", "sid", "code_once", "1,1"),
    )
    monkeypatch.setattr(
        "plugin.framework.config.get_config_bool",
        lambda key, default=False: False if key == "scripting.python_auto_spill" else default,
    )
    monkeypatch.setattr(
        "plugin.calc.python.function.run_code_in_user_venv",
        lambda *args, **kwargs: {"status": "ok", "result": [1, 2, 3]},
    )
    call_count = 0
    real_token = python_function._worker_data_token

    def counting_token(data):
        nonlocal call_count
        call_count += 1
        return real_token(data)

    monkeypatch.setattr("plugin.calc.python.function._worker_data_token", counting_token)

    python_function.execute_python_addin(ctx, "code_once", data=42, doc=doc)
    assert call_count == 1


def test_spill_registry_doc_key_uses_runtime_uid_ladder() -> None:
    class MethodVsPropStub:
        def getRuntimeUID(self) -> str:
            return "from-method"

        def getPropertyValue(self, name: str) -> str:
            if name == "RuntimeUID":
                return "from-prop"
            raise KeyError(name)

    assert python_function._spill_registry_doc_key(MethodVsPropStub()) == "from-method"

    int_stub = CalcDocStub(props={"RuntimeUID": 42})
    assert python_function._spill_registry_doc_key(int_stub) == "42"

    class NonStrIntObj:
        pass

    non_str_stub = CalcDocStub(url="file:///x.ods", props={"RuntimeUID": NonStrIntObj()})
    assert python_function._spill_registry_doc_key(non_str_stub) == "file:///x.ods"


def test_spill_registry_doc_key_stable_without_runtime_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.workbook_lifecycle import _forget_doc_lifecycle_key

    doc = CalcDocStub(url="", props={})
    counter = 0

    def sid(_doc: Any) -> str:
        nonlocal counter
        counter += 1
        return f"calc:unsaved:{counter}"

    monkeypatch.setattr("plugin.calc.python.workbook_lifecycle.calc_workbook_base_session_id", sid)

    key1 = ""
    try:
        key1 = python_function._spill_registry_doc_key(doc)
        assert key1 == "calc:unsaved:1"
        key2 = python_function._spill_registry_doc_key(doc)
        assert key2 == "calc:unsaved:1"
        assert counter == 1
    finally:
        if key1:
            _forget_doc_lifecycle_key(key1)



