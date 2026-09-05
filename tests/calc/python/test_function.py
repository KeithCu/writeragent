# WriterAgent - =PYTHON() return coercion tests

from __future__ import annotations

import logging
import math
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
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h))
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

    python_function.insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"})

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
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h))
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

    python_function.insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"})

    shape.setPosition.assert_called_once_with(pos)
    shape.setSize.assert_any_call(("Size", 10000, 6000))
    shape.setPropertyValue.assert_any_call("Anchor", cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", False)


def test_insert_image_result_targets_formula_cell_sheet_when_another_sheet_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #385: When Overview is active, plots for formulas on Viz_Gallery land on Viz_Gallery."""
    from tests.testing_utils import CalcSheetStub

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
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h))
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

    python_function.insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"}, code=code_str)

    # Must be added to Viz_Gallery's DrawPage, NOT Overview's DrawPage
    sheet_viz.DrawPage.add.assert_called_once_with(shape)
    sheet_overview.DrawPage.add.assert_not_called()
    shape.setPropertyValue.assert_any_call("Anchor", viz_cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", True)
    shape.setSize.assert_any_call(size)


def test_locate_formula_cell_in_doc_finds_on_secondary_sheet() -> None:
    """locate_formula_cell_in_doc locates formula cell on non-active sheet."""
    from tests.testing_utils import CalcSheetStub

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
    awt_mod = SimpleNamespace(Size=lambda w, h: ("Size", w, h))
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

    python_function.insert_image_result_on_sheet(ctx, {"data": b"abc", "format": "png"})

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
    val = finalize_python_return(ctx, "test_code", result)

    assert val == 10.0
    # B2 is the formula cell (left alone); spill writes B3 via setDataArray.
    assert sheet.getCellByPosition(1, 2).getValue() == 20.0

    key = ("file:///fake.ods", sheet.getName(), 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[key] == [(2, 1)]


def test_finalize_python_return_spills_on_secondary_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-spill locates the formula cell and spills correctly even when a different sheet is active."""
    from tests.testing_utils import CalcSheetStub

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
    val = finalize_python_return(ctx, "secondary_code", result)

    assert val == 100.0
    assert sheet2.getCellByPosition(1, 2).getValue() == 200.0
    key = ("file:///multi.ods", "Viz_Gallery", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[key] == [(2, 1)]


def test_finalize_python_return_matrix_formula_does_not_spill() -> None:
    """Test that a matrix selection (e.g. B2:C3) does not trigger spilling, but returns standard scalar instead."""
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

    val = finalize_python_return(ctx, "test_code_spill_blocked", [[100], [200]])

    assert val == "#SPILL!"
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

    monkeypatch.setattr("plugin.doc.udprops.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", mock_set_prop)

    doc = CalcDocStub(url="file:///fake_doc.ods")

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()

    python_function.load_spill_registry_for_doc(doc)
    key = ("file:///fake_doc.ods", "Sheet1", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert python_function.SPILL_REGISTRY[key] == [(2, 1), (3, 1)]

    python_function.SPILL_REGISTRY[key] = [(2, 1), (3, 1), (4, 1)]
    python_function.save_spill_registry_for_doc(doc)

    assert saved_payload is not None
    data = json.loads(saved_payload)
    assert data["Sheet1:1,1"] == [[2, 1], [3, 1], [4, 1]]


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


def test_get_python_init_kwargs_off_main_empty_when_two_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.scripting import session_manager as sm

    sm.record_active_calc_session("calc:file:///a.ods", {"init": "a"})
    sm.record_active_calc_session("calc:file:///b.ods", {"init": "b"})
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    try:
        assert python_function.get_python_init_kwargs(MagicMock()) == {}
    finally:
        sm.clear_active_calc_session()


def test_get_python_init_kwargs_registers_unload_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///fake_lifecycle.ods")
    calls: list[tuple] = []

    monkeypatch.setattr("plugin.scripting.document_scripts.get_calc_document_from_ctx", lambda ctx: doc)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda _doc: {"dummy": True})
    monkeypatch.setattr(
        "plugin.calc.python.workbook_lifecycle.ensure_calc_workbook_unload_resets_python",
        lambda ctx, workbook: calls.append((ctx, workbook)),
    )

    ctx = MagicMock()
    kwargs = python_function.get_python_init_kwargs(ctx)
    assert kwargs == {"dummy": True}
    assert calls == [(ctx, doc)]


def test_get_python_init_kwargs_survives_listener_install_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///fake_lifecycle_fail.ods")

    def _boom(_ctx, _doc):
        raise RuntimeError("listener failed")

    monkeypatch.setattr("plugin.scripting.document_scripts.get_calc_document_from_ctx", lambda ctx: doc)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda _doc: {"dummy": True})
    monkeypatch.setattr(
        "plugin.calc.python.workbook_lifecycle.ensure_calc_workbook_unload_resets_python",
        _boom,
    )

    kwargs = python_function.get_python_init_kwargs(MagicMock())
    assert kwargs == {"dummy": True}


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
    val = finalize_python_return(ctx, "test_code_2d", result)

    assert val == 10.0
    assert sheet.getCellByPosition(2, 1).getValue() == 20.0  # C2
    assert sheet.getCellByPosition(1, 2).getValue() == 30.0  # B3
    assert sheet.getCellByPosition(2, 2).getValue() == 40.0  # C3

    key = ("file:///fake2d.ods", "Sheet1", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert set(python_function.SPILL_REGISTRY[key]) == {(1, 2), (2, 1), (2, 2)}


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
    with zero ``Spill:`` lines. Off-main finalize must resolve the cached
    document and deferred-spill the neighbors.
    """
    from plugin.scripting import session_manager as sm
    from tests.testing_utils import CalcSheetStub

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
    sm.clear_active_calc_session()
    sm.record_active_calc_session("calc:file:///SpillTest.ods", doc=doc)

    on_main = {"value": False}
    monkeypatch.setattr(
        "plugin.framework.thread_guard.on_main_thread",
        lambda: on_main["value"],
    )
    # Desktop resolve must not be required once the session cached the model.
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: None)
    assert python_function._spill_target_doc(ctx, None) is doc
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    try:
        val = finalize_python_return(ctx, spilltest_code, [[11, 22], [33, 44]], doc=None)
    finally:
        sm.clear_active_calc_session()

    assert val == 11
    assert sheet.getCellByPosition(1, 0).getValue() == 22  # B1
    assert sheet.getCellByPosition(0, 1).getValue() == 33  # A2
    assert sheet.getCellByPosition(1, 1).getValue() == 44  # B2
    key = ("file:///SpillTest.ods", "SpillTest", 0, 0)
    assert key in python_function.SPILL_REGISTRY
    assert set(python_function.SPILL_REGISTRY[key]) == {(0, 1), (1, 0), (1, 1)}


def test_finalize_python_return_spills_off_main_when_doc_unambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off-main finalize used to treat missing target_doc as a matrix formula.

    That returned only grid[0][0] (SpillTest [[11,22],[33,44]] painted 11) with
    zero Spill: logs. When at most one Calc session is recorded, deferred
    locate+write must still paint the full grid.
    """
    from plugin.scripting import session_manager as sm

    doc = CalcDocStub(url="file:///offmain-spill.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("off_main_spill")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()
    sm.clear_active_calc_session()

    on_main = {"value": False}
    monkeypatch.setattr(
        "plugin.framework.thread_guard.on_main_thread",
        lambda: on_main["value"],
    )
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: doc)
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    try:
        val = finalize_python_return(ctx, "off_main_spill", [[11, 22], [33, 44]], doc=None)
    finally:
        sm.clear_active_calc_session()

    assert val == 11
    assert sheet.getCellByPosition(2, 1).getValue() == 22  # C2
    assert sheet.getCellByPosition(1, 2).getValue() == 33  # B3
    assert sheet.getCellByPosition(2, 2).getValue() == 44  # C3
    key = ("file:///offmain-spill.ods", "Sheet1", 1, 1)
    assert key in python_function.SPILL_REGISTRY
    assert set(python_function.SPILL_REGISTRY[key]) == {(1, 2), (2, 1), (2, 2)}


def test_finalize_python_return_spills_dataframe_payload_off_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DataFrame envelopes must spill header+body off-main, not only the first header."""
    from plugin.scripting import session_manager as sm

    doc = CalcDocStub(url="file:///offmain-df.ods", selection="A1")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PYTHON("df_spill")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()
    sm.clear_active_calc_session()
    sm.record_active_calc_session("calc:file:///offmain-df.ods", {})

    on_main = {"value": False}
    monkeypatch.setattr(
        "plugin.framework.thread_guard.on_main_thread",
        lambda: on_main["value"],
    )
    monkeypatch.setattr(python_function, "_get_calc_doc", lambda _ctx: doc)
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    payload = {
        "__wa_payload__": "dataframe",
        "columns": ["Region", "Channel"],
        "data": [["North", "Search"], ["South", "Email"]],
    }
    try:
        val = finalize_python_return(ctx, "df_spill", payload, doc=None)
    finally:
        sm.clear_active_calc_session()

    assert val == "Region"
    assert sheet.getCellByPosition(1, 0).getString() == "Channel"
    assert sheet.getCellByPosition(0, 1).getString() == "North"
    assert sheet.getCellByPosition(1, 1).getString() == "Search"
    assert sheet.getCellByPosition(0, 2).getString() == "South"
    assert sheet.getCellByPosition(1, 2).getString() == "Email"


def test_finalize_python_return_off_main_no_spill_when_two_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two recorded workbooks: do not guess which book to spill into."""
    from plugin.scripting import session_manager as sm

    doc = CalcDocStub(url="file:///ambig.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("ambig_spill")')
    ctx = _ctx_with_doc(doc)

    python_function.SPILL_REGISTRY.clear()
    python_function.LOADED_DOCUMENTS.clear()
    sm.clear_active_calc_session()
    sm.record_active_calc_session("calc:file:///a.ods", {"init": "a"})
    sm.record_active_calc_session("calc:file:///b.ods", {"init": "b"})

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

    try:
        val = finalize_python_return(ctx, "ambig_spill", [[11, 22], [33, 44]], doc=None)
    finally:
        sm.clear_active_calc_session()

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
    python_function.SPILL_REGISTRY[key] = [(2, 1)]

    cell_B2 = MagicMock()
    cell_B3 = MagicMock()

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
    cell_B3.clearContents.assert_called_once_with(23)
    assert len(saved) == 1


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
    from pathlib import Path

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
    from tests.strip_bundle import module_source_contains

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
    key = (tid, python_function.session_key(ctx, code), repr(worker_data))
    python_function._MATRIX_SCALAR_SESSIONS[key] = python_function.WorkerResultSession(
        [1.0, 2.0, 3.0], [1.0, 2.0, 3.0]
    )
    called: list[int] = []

    def _should_not_run(*_a, **_k):
        called.append(1)
        return {"status": "ok", "result": 99}

    monkeypatch.setattr(python_function, "run_code_in_user_venv", _should_not_run)
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    out = python_function.execute_python_addin(ctx, code)
    assert called == []
    assert out == 1.0
    from tests.strip_bundle import module_source_contains

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


def test_py_scoped_dir_bindings_none_doc() -> None:
    from plugin.scripting import session_manager

    session_manager.clear_active_calc_session()
    try:
        assert python_function._py_scoped_dir_bindings(None) == {"scoped_dir": None}
    finally:
        session_manager.clear_active_calc_session()


def test_py_scoped_dir_bindings_off_main_skips_document_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yellow / #402: off-main recalc must not call getURL() on a cached model."""
    from plugin.scripting import session_manager

    called: list[int] = []

    def boom(doc: object) -> str:
        called.append(1)
        raise AssertionError("must not touch UNO off-main")

    session_manager.clear_active_calc_session()
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr("plugin.doc.document_research.get_document_directory", boom)
    try:
        assert python_function._py_scoped_dir_bindings(object()) == {"scoped_dir": None}
        assert called == []
    finally:
        session_manager.clear_active_calc_session()


def test_py_scoped_dir_bindings_off_main_uses_cached_file_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """File-based run_sql needs the document folder even when recalc is off-main."""
    from plugin.scripting import session_manager

    workbook = tmp_path / "python_showcase_demo.xlsx"
    workbook.write_bytes(b"pk")
    session_manager.clear_active_calc_session()
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)

    def boom(doc: object) -> str:
        raise AssertionError("must not touch UNO off-main")

    monkeypatch.setattr("plugin.doc.document_research.get_document_directory", boom)
    try:
        session_manager.record_active_calc_session(f"calc:{workbook.as_uri()}")
        assert python_function._py_scoped_dir_bindings(None) == {"scoped_dir": str(tmp_path)}
    finally:
        session_manager.clear_active_calc_session()


def test_py_scoped_dir_bindings_on_main_uses_document_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugin.scripting import session_manager

    session_manager.clear_active_calc_session()
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(
        "plugin.doc.document_research.get_document_directory",
        lambda doc: "/tmp/workbook-dir",
    )
    try:
        assert python_function._py_scoped_dir_bindings(object()) == {"scoped_dir": "/tmp/workbook-dir"}
    finally:
        session_manager.clear_active_calc_session()


def test_execute_python_addin_binds_scoped_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.scripting import session_manager

    python_function.clear_python_addin_cache()
    session_manager.clear_active_calc_session()
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
    try:
        # Pass doc= — CalcDocStub is not a UNO Calc model, so desktop lookup is None.
        out = python_function.execute_python_addin(_ctx_with_doc(doc), "1+1", doc=doc)
        assert out == 1.0
        assert captured.get("bindings") == {"scoped_dir": "/folder"}
    finally:
        session_manager.clear_active_calc_session()


def test_execute_python_addin_off_main_injects_cached_scoped_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Host must inject the document folder for file-based run_sql, not None."""
    from plugin.scripting import session_manager

    workbook = tmp_path / "demo.xlsx"
    workbook.write_bytes(b"pk")
    python_function.clear_python_addin_cache()
    session_manager.clear_active_calc_session()
    captured: dict[str, Any] = {}

    def fake_run(*_a: object, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "ok", "result": 1.0}

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr(python_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(
        python_function,
        "workbook_session_id",
        lambda *_a, **_k: f"calc:{workbook.as_uri()}",
    )
    try:
        session_manager.record_active_calc_session(f"calc:{workbook.as_uri()}")
        out = python_function.execute_python_addin(_ctx_with_doc(None), "result=1")
        assert out == 1.0
        assert captured.get("bindings") == {"scoped_dir": str(tmp_path)}
    finally:
        session_manager.clear_active_calc_session()


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


