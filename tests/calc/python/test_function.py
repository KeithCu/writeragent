# WriterAgent - =PYTHON() return coercion tests

from __future__ import annotations

import logging
import math
import struct
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import plugin.calc.python.addin_values as _addin_values
import plugin.calc.python.image_egress as _image_egress
import plugin.calc.python.function as _function
import plugin.calc.python.spill as _spill
import plugin.calc.python.undo as _undo
from plugin.calc.python.function import build_cell_context, finalize_python_return
from plugin.scripting.payload_codec import is_multi_data


def _cell(ctx: Any, code: str, doc: Any = None, *, on_main: bool | None = None):
    from plugin.framework.queue_executor import main

    return build_cell_context(ctx, doc, code, on_main=main.is_current() if on_main is None else on_main)


from plugin.tests.testing_utils import CalcDocStub
from tests.calc.python.calc_workbook_test_access import SPILL_REGISTRY, ctx_with_doc as _ctx_with_doc, reset_calc_workbook_states


def _patch_immediate_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.workbook_state import Debounce

    def _immediate(self: Debounce, key: str, delay: float, fn: Any) -> None:
        fn()

    monkeypatch.setattr(Debounce, "schedule", _immediate)


def test_finalize_python_return_scalar_nan_becomes_error() -> None:
    """Scalar NaN from worker becomes a Calc error (not silent empty)."""

    class _Ctx:
        pass

    val = finalize_python_return(_cell(_Ctx(), "c"), float("nan"))
    assert math.isnan(val)


@pytest.fixture
def _image_egress_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub tempfile, uno, and com.sun.star.awt for image-egress unit tests."""
    import sys
    import plugin.scripting.payload_codec as payload_codec

    class _TmpFile:
        name = "/tmp/fake.png"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def write(self, _data):
            return None

    class _UnoModule:
        @staticmethod
        def systemPathToFileUrl(path: str) -> str:
            return f"file://{path}"

    monkeypatch.setattr(payload_codec.tempfile, "NamedTemporaryFile", lambda **kwargs: _TmpFile())
    monkeypatch.setitem(sys.modules, "uno", _UnoModule())
    awt_mod = SimpleNamespace(Size=lambda w, h: SimpleNamespace(Width=w, Height=h), Point=lambda x, y: SimpleNamespace(X=x, Y=y))
    monkeypatch.setitem(sys.modules, "com.sun.star.awt", awt_mod)


def test_insert_image_result_uses_merged_safe_geometry(monkeypatch: pytest.MonkeyPatch, _image_egress_env: None) -> None:
    doc = CalcDocStub(selection="C4")
    shape = MagicMock()
    doc._created["com.sun.star.drawing.GraphicObjectShape"] = shape
    sheet = doc.getSheets().getByName("Sheet1")
    cell = sheet.getCellByPosition(2, 3)
    cell.IsMerged = True

    pos = SimpleNamespace(X=111, Y=222)
    size = SimpleNamespace(Width=8000, Height=5000)
    monkeypatch.setattr(_image_egress, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    _image_egress.insert_image_result_on_sheet({"data": b"abc", "format": "png"}, doc=doc)

    shape.setPosition.assert_called_once_with(pos)
    shape.setSize.assert_any_call(size)
    shape.setPropertyValue.assert_any_call("Anchor", cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", True)


def test_insert_image_result_thin_merged_cell_preserves_default_size(monkeypatch: pytest.MonkeyPatch, _image_egress_env: None) -> None:
    """A 1-row thin merged block (e.g. A1:H1 banner) uses DEFAULT_CHART_SIZE and ResizeWithCell=False."""
    doc = CalcDocStub(selection="A1")
    shape = MagicMock()
    doc._created["com.sun.star.drawing.GraphicObjectShape"] = shape
    sheet = doc.getSheets().getByName("Sheet1")
    cell = sheet.getCellByPosition(0, 0)
    cell.IsMerged = True

    pos = SimpleNamespace(X=0, Y=0)
    # Thin 1-row banner: wide (20000) but short (1270 HMM / ~12.7 mm)
    size = SimpleNamespace(Width=20000, Height=1270)
    monkeypatch.setattr(_image_egress, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    _image_egress.insert_image_result_on_sheet({"data": b"abc", "format": "png"}, doc=doc)

    shape.setPosition.assert_called_once_with(pos)
    shape.setSize.assert_any_call(SimpleNamespace(Width=10000, Height=6000))
    shape.setPropertyValue.assert_any_call("Anchor", cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", False)


def test_insert_image_result_targets_formula_cell_sheet_when_another_sheet_active(monkeypatch: pytest.MonkeyPatch, _image_egress_env: None) -> None:
    """Issue #385: When Overview is active, plots for formulas on Viz_Gallery land on Viz_Gallery."""
    from plugin.tests.testing_utils import CalcSheetStub

    sheet_overview = CalcSheetStub("Overview")
    sheet_viz = CalcSheetStub("Viz_Gallery")

    doc = CalcDocStub(sheets=[sheet_overview, sheet_viz], active_sheet="Overview", selection="A1:H1")
    shape = MagicMock()
    doc._created["com.sun.star.drawing.GraphicObjectShape"] = shape

    # Formula cell is on Viz_Gallery!D7 (col 3, row 6)
    viz_cell = sheet_viz.getCellByPosition(3, 6)
    code_str = "plt.figure(); plt.plot([1, 2, 3]); plt.title('Sales Trend')"
    viz_cell.setFormula(f'=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("{code_str}")')
    viz_cell.IsMerged = True

    pos = SimpleNamespace(X=5000, Y=3000)
    size = SimpleNamespace(Width=9000, Height=6000)
    monkeypatch.setattr(_image_egress, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    _image_egress.insert_image_result_on_sheet({"data": b"abc", "format": "png"}, code=code_str, doc=doc)

    # Must be added to Viz_Gallery's DrawPage, NOT Overview's DrawPage
    sheet_viz.DrawPage.add.assert_called_once_with(shape)
    sheet_overview.DrawPage.add.assert_not_called()
    shape.setPropertyValue.assert_any_call("Anchor", viz_cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", True)
    shape.setSize.assert_any_call(size)


def test_insert_image_result_unmerged_single_cell_default_size(monkeypatch: pytest.MonkeyPatch, _image_egress_env: None) -> None:
    doc = CalcDocStub(selection="C4")
    shape = MagicMock()
    doc._created["com.sun.star.drawing.GraphicObjectShape"] = shape
    sheet = doc.getSheets().getByName("Sheet1")
    cell = sheet.getCellByPosition(2, 3)
    cell.IsMerged = False

    pos = SimpleNamespace(X=111, Y=222)
    size = SimpleNamespace(Width=333, Height=444)
    monkeypatch.setattr(_image_egress, "get_cell_geometry", lambda _sheet, _cell: (pos, size))

    _image_egress.insert_image_result_on_sheet({"data": b"abc", "format": "png"}, doc=doc)

    shape.setPosition.assert_called_once_with(pos)
    # For single unmerged cells, default size (10000, 6000) should be applied and not overwritten by cell size
    shape.setSize.assert_any_call(SimpleNamespace(Width=10000, Height=6000))
    shape.setPropertyValue.assert_any_call("Anchor", cell)
    shape.setPropertyValue.assert_any_call("ResizeWithCell", False)


def test_finalize_python_return_triggers_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a list result triggers deferred spilling when not in a matrix selection."""
    doc = CalcDocStub(url="file:///fake.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("test_code")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()

    _patch_immediate_debounce(monkeypatch)
    # Deferred spill posts to the main-thread queue; run immediately in unit tests.
    monkeypatch.setattr("plugin.framework.queue_executor.main.post", lambda fn, *a, **k: fn(*a, **k))

    result = [10.0, 20.0]  # 1D list, will be treated as shape (2, 1)
    val = finalize_python_return(_cell(ctx, "test_code", doc), result)

    assert val == 10.0
    # B2 is the formula cell (left alone); spill writes B3 via setDataArray.
    assert sheet.getCellByPosition(1, 2).getValue() == 20.0

    key = ("file:///fake.ods", sheet.getName(), 1, 1)
    assert key in SPILL_REGISTRY
    assert SPILL_REGISTRY[key].spills == [(2, 1)]


def test_finalize_python_return_spills_on_secondary_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-spill locates the formula cell and spills correctly even when a different sheet is active."""
    from plugin.tests.testing_utils import CalcSheetStub

    sheet1 = CalcSheetStub("Overview")
    sheet2 = CalcSheetStub("Viz_Gallery")
    doc = CalcDocStub(sheets=[sheet1, sheet2], url="file:///multi.ods", active_sheet="Overview", selection="A1")
    sheet2.getCellByPosition(1, 1).setFormula('=PYTHON("secondary_code")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()

    _patch_immediate_debounce(monkeypatch)
    monkeypatch.setattr("plugin.framework.queue_executor.main.post", lambda fn, *a, **k: fn(*a, **k))

    result = [100.0, 200.0]
    val = finalize_python_return(_cell(ctx, "secondary_code", doc), result)

    assert val == 100.0
    assert sheet2.getCellByPosition(1, 2).getValue() == 200.0
    key = ("file:///multi.ods", "Viz_Gallery", 1, 1)
    assert key in SPILL_REGISTRY
    assert SPILL_REGISTRY[key].spills == [(2, 1)]


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
    val = finalize_python_return(_cell(ctx, "test_code_matrix", None), result)

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

    reset_calc_workbook_states()

    val = finalize_python_return(_cell(ctx, "test_code_spill_blocked", doc), [[100], [200]])

    assert val == "#SPILL!"
    key = ("file:///fake.ods", "Sheet1", 1, 1)
    assert SPILL_REGISTRY.get(key) is None


def test_build_cell_context_off_main_skips_uno_and_init_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #402: off main, build_cell_context must not touch UNO; the caller doc is only passed through because its init script is UNO."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)

    def boom(_doc: object) -> dict[str, Any]:
        raise AssertionError("must not read document scripts off-main")

    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", boom)

    doc = MagicMock()
    doc.getURL.side_effect = AssertionError("getURL off main")
    for target_doc in (None, doc):
        cell = _cell(MagicMock(), "print('hello')", target_doc, on_main=False)
        assert cell.origin is None and cell.session_id is None and cell.is_matrix is False
        assert cell.init_kwargs == {}
    doc.getURL.assert_not_called()


def test_finalize_python_return_triggers_spill_2d(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a 2D result triggers block spills via setDataArray on appropriate ranges."""
    doc = CalcDocStub(url="file:///fake2d.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("test_code_2d")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()

    _patch_immediate_debounce(monkeypatch)
    monkeypatch.setattr("plugin.framework.queue_executor.main.post", lambda fn, *a, **k: fn(*a, **k))

    result = [[10.0, 20.0], [30.0, 40.0]]
    val = finalize_python_return(_cell(ctx, "test_code_2d", doc), result)

    assert val == 10.0
    assert sheet.getCellByPosition(2, 1).getValue() == 20.0  # C2
    assert sheet.getCellByPosition(1, 2).getValue() == 30.0  # B3
    assert sheet.getCellByPosition(2, 2).getValue() == 40.0  # C3

    key = ("file:///fake2d.ods", "Sheet1", 1, 1)
    assert key in SPILL_REGISTRY
    assert set(SPILL_REGISTRY[key].spills) == {(1, 2), (2, 1), (2, 2)}


def _install_immediate_spill_timer(monkeypatch: pytest.MonkeyPatch, *, on_main_after_start: dict[str, bool] | None = None) -> None:
    """Run the 0.1s spill debounce inline; optionally flip on_main when it fires."""
    _patch_immediate_debounce(monkeypatch)

    def _post(fn, *a, **k):
        if on_main_after_start is not None:
            on_main_after_start["value"] = True
        fn(*a, **k)

    monkeypatch.setattr("plugin.framework.queue_executor.main.post", _post)


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
    doc = CalcDocStub(sheets=[sheet], url="file:///SpillTest.ods", active_sheet="SpillTest", selection="A1")
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{spilltest_code}")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()
    from plugin.calc.python.workbook_state import WorkbookState

    WorkbookState.for_doc(doc)

    on_main = {"value": False}
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: on_main["value"])
    # The front window is never consulted; the caller doc is the only source.
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    val = finalize_python_return(_cell(ctx, spilltest_code, doc), [[11, 22], [33, 44]])

    assert val == 11
    assert sheet.getCellByPosition(1, 0).getValue() == 22  # B1
    assert sheet.getCellByPosition(0, 1).getValue() == 33  # A2
    assert sheet.getCellByPosition(1, 1).getValue() == 44  # B2
    key = ("file:///SpillTest.ods", "SpillTest", 0, 0)
    assert key in SPILL_REGISTRY
    assert set(SPILL_REGISTRY[key].spills) == {(0, 1), (1, 0), (1, 1)}


def test_finalize_python_return_spills_off_main_into_caller_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off-main finalize with the caller doc defers locate+write to the UI thread.

    Treating it as a matrix formula returned only grid[0][0] (SpillTest
    [[11,22],[33,44]] painted 11) with zero Spill: logs.
    """
    doc = CalcDocStub(url="file:///offmain-spill.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("off_main_spill")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()
    from plugin.calc.python.workbook_state import WorkbookState

    WorkbookState.for_doc(doc)

    on_main = {"value": False}
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: on_main["value"])
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    val = finalize_python_return(_cell(ctx, "off_main_spill", doc), [[11, 22], [33, 44]])

    assert val == 11
    assert sheet.getCellByPosition(2, 1).getValue() == 22  # C2
    assert sheet.getCellByPosition(1, 2).getValue() == 33  # B3
    assert sheet.getCellByPosition(2, 2).getValue() == 44  # C3
    key = ("file:///offmain-spill.ods", "Sheet1", 1, 1)
    assert key in SPILL_REGISTRY
    assert set(SPILL_REGISTRY[key].spills) == {(1, 2), (2, 1), (2, 2)}


def test_finalize_python_return_spills_dataframe_payload_off_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """DataFrame envelopes must spill header+body off-main, not only the first header."""
    doc = CalcDocStub(url="file:///offmain-df.ods", selection="A1")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PYTHON("df_spill")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()
    from plugin.calc.python.workbook_state import WorkbookState

    WorkbookState.for_doc(doc)

    on_main = {"value": False}
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: on_main["value"])
    _install_immediate_spill_timer(monkeypatch, on_main_after_start=on_main)

    payload = {"__wa_payload__": "dataframe", "columns": ["Region", "Channel"], "data": [["North", "Search"], ["South", "Email"]]}
    val = finalize_python_return(_cell(ctx, "df_spill", doc), payload)

    assert val == "Region"
    assert sheet.getCellByPosition(1, 0).getString() == "Channel"
    assert sheet.getCellByPosition(0, 1).getString() == "North"
    assert sheet.getCellByPosition(1, 1).getString() == "Search"
    assert sheet.getCellByPosition(0, 2).getString() == "South"
    assert sheet.getCellByPosition(1, 2).getString() == "Email"


def test_finalize_python_return_off_main_no_spill_without_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """No caller doc: do not guess which book to spill into (front window ignored)."""
    doc = CalcDocStub(url="file:///ambig.ods", selection="B2")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("ambig_spill")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    spill_started = {"n": 0}

    class _NoStartTimer:
        def __init__(self, interval, function, args=(), kwargs={}):
            self.function = function

        def start(self):
            spill_started["n"] += 1

        def cancel(self):
            return None

    monkeypatch.setattr(threading, "Timer", _NoStartTimer)

    val = finalize_python_return(_cell(ctx, "ambig_spill", None), [[11, 22], [33, 44]])

    assert val == 11
    assert spill_started["n"] == 0
    assert sheet.getCellByPosition(2, 1).getValue() in (0, 0.0, None, "")
    assert SPILL_REGISTRY.get(("file:///ambig.ods", "Sheet1", 1, 1)) is None


def test_finalize_python_return_off_main_skips_session_key_on_cached_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spill-disabled list results must not locate the cached model off-main."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    monkeypatch.setattr("plugin.calc.python.function.get_config", lambda _key: False)
    doc = MagicMock()
    doc.getURL.side_effect = AssertionError("getURL off main")
    val = finalize_python_return(_cell(MagicMock(), "c", doc, on_main=False), [10, 20, 30])
    assert val == 10.0
    doc.getURL.assert_not_called()


def test_calc_python_function_zero_event_pumping_invariant() -> None:
    """Static invariant: function.py must not import event loop pumping or UI draining functions.

    Pumping during recalc re-enters the formula engine (#VALUE!); see plugin/calc/AGENTS.md.
    """
    import ast

    banned_modules = {"async_stream", "vcl_pumping"}
    pump_names = {"processEventsToIdle", "process_events_to_idle", "run_async_worker_with_drain", "pump_vcl_events", "wait_while_pumping", "pump_secondary_idle_tick"}
    tree = ast.parse(Path(_function.__file__).read_text(encoding="utf-8"))
    flagged: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module.endswith(mod) for mod in banned_modules):
                flagged.append(node.module)
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if any(full.endswith(mod) for mod in banned_modules):
                    flagged.append(full)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.endswith(mod) for mod in banned_modules):
                    flagged.append(alias.name)
        if isinstance(node, ast.alias) and node.name in pump_names:
            flagged.append(node.name)
        elif isinstance(node, ast.Name) and node.id in pump_names:
            flagged.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in pump_names:
            flagged.append(node.attr)
    assert flagged == []


def test_function_module_avoids_document_helpers_import() -> None:
    """First =PY() must not load document_helpers → SheetAnalyzer or the dialog stack."""
    from tests.harness.import_guards import assert_no_toplevel_import

    addin_modules = (_function, _spill, _addin_values, _undo)
    forbidden = {"plugin.doc.document_helpers", "plugin.calc.analyzer", "plugin.scripting.document_scripts", "plugin.chatbot.dialogs"}
    for mod in addin_modules:
        assert_no_toplevel_import(mod, forbidden)


def test_execute_python_addin_maps_missing_venv_error(monkeypatch) -> None:
    _function.clear_python_addin_cache()
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "code": "VENV_NOT_FOUND", "message": "No python executable found under configured venv: '/missing'"})
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(_function, "workbook_session_id", lambda *_a, **_k: None)
    out = _function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "1+1")
    assert "Settings" in out


def test_execute_python_addin_maps_timeout_error(monkeypatch) -> None:
    _function.clear_python_addin_cache()
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "code": "EXECUTION_TIMEOUT", "message": "Code execution exceeded the maximum execution time of 10 seconds"})
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(_function, "workbook_session_id", lambda *_a, **_k: None)
    out = _function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "1+1")
    assert isinstance(out, str)
    assert out.startswith("Error:")
    assert "Settings" in out


def test_execute_python_addin_image_failure_still_processes_later_slots(monkeypatch) -> None:
    """A failed plot slot does not stop later slots; the first error is reported after all slots."""
    _function.clear_python_addin_cache()
    image = {"__wa_payload__": "image", "format": "png", "data": b"\x89PNG"}
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "ok", "result": [dict(image), dict(image), dict(image)]})
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(_function, "workbook_session_id", lambda *_a, **_k: None)

    seen: list[int] = []

    def fake_insert(_img, *, image_index, **_kwargs):
        seen.append(image_index)
        if image_index == 0:
            raise _image_egress.ImageEgressError("could not anchor plot to the formula cell")

    monkeypatch.setattr(_function, "insert_image_result_on_sheet", fake_insert)
    out = _function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "plots()")

    # Revert target: the try/except around each insert in the =PY() image loop
    assert seen == [0, 1, 2]
    assert "could not anchor plot" in out


def test_scalar_for_list_result_no_share_without_unique_origin() -> None:
    ctx = _ctx_with_doc(CalcDocStub())
    _function.clear_python_addin_cache()
    a = _function.scalar_for_list_result(_cell(ctx, "dup", None), [10, 20, 30])
    b = _function.scalar_for_list_result(_cell(ctx, "dup", None), [10, 20, 30])
    assert a == 10
    assert b == 10


def test_finalize_python_return_located_array_does_not_break_scalar_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unpacking locate_formula_cell_in_doc must not shadow the CellContext parameter."""

    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    sheet = doc.getSheets().getByName("Sheet1")
    formula_cell = sheet.getCellByPosition(0, 0)
    _function.clear_python_addin_cache()
    monkeypatch.setattr("plugin.calc.python.function.get_config", lambda _k: True)
    with patch("plugin.calc.python.function.locate_formula_cell_in_doc", return_value=(sheet, formula_cell, (0, 0))):
        monkeypatch.setattr(_function, "cell_is_matrix", lambda _s, _c: True)
        a = _function.finalize_python_return(_cell(ctx, "dup", doc), [10, 20, 30])
        b = _function.finalize_python_return(_cell(ctx, "dup", doc), [10, 20, 30])
    assert a == 10
    assert b == 20


def test_scalar_for_list_result_increments_with_unique_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    _function.clear_python_addin_cache()
    doc.getSheets().getByName("Sheet1").getCellByPosition(0, 0).setFormula('=PY("dup")')
    a = _function.scalar_for_list_result(_cell(ctx, "dup", doc), [10, 20, 30])
    b = _function.scalar_for_list_result(_cell(ctx, "dup", doc), [10, 20, 30])
    c = _function.scalar_for_list_result(_cell(ctx, "dup", doc), [10, 20, 30])
    assert a == 10
    assert b == 20
    assert c == 30


def _skip_if_release_bundle() -> None:
    from plugin.framework import thread_guard as tg

    # In release bundles, thread_guard is replaced by a no-op stub that lacks internal guard machinery
    if not hasattr(tg, "_violation_ui_lock"):
        pytest.skip("Thread-guard background tests require active dev thread_guard (stripped in release bundles)")


def test_execute_python_addin_from_background_thread_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #402: external PyUNO setFormula runs on a bridge thread (Dummy-N).

    Must run without triggering Layer A UNO thread violations or deadlocking against main thread.
    """
    _skip_if_release_bundle()
    from plugin.framework import thread_guard as tg

    monkeypatch.setattr(tg, "GUARD_ON", True)
    monkeypatch.setattr("plugin.scripting.session_manager.python_session_mode", lambda: "isolated")

    ctx = _ctx_with_doc(None)

    def fake_run(code, **kwargs):
        return {"status": "ok", "result": 2.0}

    monkeypatch.setattr(_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)

    result_holder: list[Any] = []
    exc_holder: list[BaseException] = []

    def worker():
        try:
            res = _function.execute_python_addin(ctx, "1+1")
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
    monkeypatch.setattr("plugin.scripting.session_manager.python_session_mode", lambda: "shared")

    # Simulate main.call timing out because main thread is blocked in UNO dispatch
    def fake_execute_on_main(fn, *args, **kwargs):
        raise TimeoutError("Main-thread execution timed out")

    monkeypatch.setattr("plugin.framework.queue_executor.main.call", fake_execute_on_main)

    ctx = _ctx_with_doc(None)

    def fake_run(code, session_id=None, **kwargs):
        # Even with timeout in shared mode, it falls back to isolated (session_id=None) and returns result
        return {"status": "ok", "result": 42.0}

    monkeypatch.setattr(_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)

    result_holder: list[Any] = []
    exc_holder: list[BaseException] = []

    def worker():
        try:
            res = _function.execute_python_addin(ctx, "6*7")
            result_holder.append(res)
        except BaseException as e:
            exc_holder.append(e)

    t = threading.Thread(target=worker, name="Dummy-3")
    t.start()
    t.join(timeout=3.0)

    assert not exc_holder
    assert result_holder == [42.0]


def test_matrix_cache_staleness_after_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 1: [1,2,3] gives 1; after the gap, the next call gives 1 again and the worker is called."""
    import time

    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    _function.clear_python_addin_cache()

    curr_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: curr_time[0])
    doc.getSheets().getByName("Sheet1").getCellByPosition(0, 0).setFormula('=PY("code_gap")')
    monkeypatch.setattr(_function, "cell_is_matrix", lambda _s, _c: True)

    worker_calls: list[int] = []

    def mock_run(*_a, **_k):
        worker_calls.append(1)
        return {"status": "ok", "result": [1, 2, 3]}

    monkeypatch.setattr(_function, "run_code_in_user_venv", mock_run)

    # 1. First call: worker is called, returns 1st element
    res1 = _function._execute_python_addin_impl(ctx, "code_gap", doc=doc)
    assert res1 == 1.0
    assert len(worker_calls) == 1

    # 2. Second call within gap: worker is NOT called, returns 2nd element
    curr_time[0] += 0.5
    res2 = _function._execute_python_addin_impl(ctx, "code_gap", doc=doc)
    assert res2 == 2.0
    assert len(worker_calls) == 1

    # 3. Third call after gap (> _PY_PASS_GAP_SEC = 2.0s):
    # Cache is treated as stale, worker is called again, returns 1st element
    curr_time[0] += 3.0
    res3 = _function._execute_python_addin_impl(ctx, "code_gap", doc=doc)
    assert res3 == 1.0
    assert len(worker_calls) == 2


def test_scalar_for_list_result_stale_after_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 1: scalar_for_list_result resets next_index to 0 after gap."""
    import time

    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    _function.clear_python_addin_cache()

    curr_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: curr_time[0])
    doc.getSheets().getByName("Sheet1").getCellByPosition(0, 0).setFormula('=PY("code_gap2")')

    a = _function.scalar_for_list_result(_cell(ctx, "code_gap2", doc), [1, 2, 3])
    assert a == 1.0
    curr_time[0] += 0.5
    b = _function.scalar_for_list_result(_cell(ctx, "code_gap2", doc), [1, 2, 3])
    assert b == 2.0

    curr_time[0] += 3.0  # > _PY_PASS_GAP_SEC
    c = _function.scalar_for_list_result(_cell(ctx, "code_gap2", doc), [1, 2, 3])
    assert c == 1.0


def test_text_single_cell_arg_spills_instead_of_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 2: Single cell text arg spills normally; numeric arg stays an index."""
    doc = CalcDocStub()
    doc.getSheets().getByName("Sheet1").getCellByPosition(0, 0).setFormula('=PY("[data]*3")')
    ctx = _ctx_with_doc(doc)
    _function.clear_python_addin_cache()

    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "ok", "result": ["hello", "world"]})
    spill_calls: list[int] = []
    monkeypatch.setattr(_function, "prepare_auto_spill", lambda *_a, **_k: _spill.PreparedSpill("Sheet1", 0, 0, sheet=doc.getSheets().getByName("Sheet1"), key="file:///u.ods", code="[data]*3"))
    monkeypatch.setattr(_function, "queue_deferred_spill_write", lambda *_a, **_k: spill_calls.append(1))

    # Text arg: A1 contains "text"
    res_text = _function._execute_python_addin_impl(ctx, "[data]*3", data=[["text"]], doc=doc)
    # Spills normally (returns corner "hello" and queued deferred spill write, does NOT error)
    assert res_text == "hello"
    assert len(spill_calls) == 1

    # Numeric arg: e.g. ROW()-1 (value 1.0) -> acts as index, returns 2nd element
    res_num = _function._execute_python_addin_impl(ctx, "[data]*3", data=[[1.0]], doc=doc)
    assert res_num == "world"


def test_build_cell_context_without_doc_does_not_guess_init_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """No caller document means no init kwargs; the front window is not consulted."""

    def boom(_ctx: object) -> object:
        raise AssertionError("must not look up the front window")

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.scripting.document_scripts.get_calc_document_from_ctx", boom)
    assert _cell(MagicMock(), "").init_kwargs == {}


def test_build_cell_context_registers_unload_listener_on_for_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///fake_lifecycle.ods")
    calls: list[tuple] = []

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda _doc: {"dummy": True})
    monkeypatch.setattr("plugin.calc.python.workbook_lifecycle.ensure_calc_workbook_unload_resets_python", lambda doc, *, state: calls.append((doc, state)))

    ctx = MagicMock()
    cell = build_cell_context(ctx, doc, "x", on_main=True)
    assert cell.init_kwargs == {"dummy": True}
    assert len(calls) == 1
    assert calls[0][0] is doc


def test_build_cell_context_survives_listener_install_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///fake_lifecycle_fail.ods")

    def _boom(_doc, *, state):
        raise RuntimeError("listener failed")

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda _doc: {"dummy": True})
    monkeypatch.setattr("plugin.calc.python.workbook_lifecycle.ensure_calc_workbook_unload_resets_python", _boom)
    cell = build_cell_context(MagicMock(), doc, "x", on_main=True)
    assert cell.init_kwargs == {"dummy": True}


def test_build_cell_context_scoped_dir_none_without_doc() -> None:
    assert _cell(MagicMock(), "").scoped_dir is None


def test_build_cell_context_scoped_dir_off_main_skips_document_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yellow / #402: off-main recalc must not call getURL() on the caller doc."""
    called: list[int] = []

    def boom(doc: object) -> str:
        called.append(1)
        raise AssertionError("must not touch UNO off-main")

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    monkeypatch.setattr("plugin.doc.document_research.get_document_directory", boom)
    assert _cell(MagicMock(), "", object(), on_main=False).scoped_dir is None
    assert called == []


def test_build_cell_context_scoped_dir_on_main_uses_document_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.doc.document_research.get_document_directory", lambda doc: "/tmp/workbook-dir")
    doc = CalcDocStub()
    assert _cell(MagicMock(), "", doc).scoped_dir == "/tmp/workbook-dir"


def test_execute_python_addin_binds_scoped_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    _function.clear_python_addin_cache()
    captured: dict[str, Any] = {}

    def fake_run(*_a: object, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "ok", "result": 1.0}

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.doc.document_research.get_document_directory", lambda doc: "/folder")
    monkeypatch.setattr(_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(_function, "workbook_session_id", lambda *_a, **_k: None)
    doc = CalcDocStub()
    out = _function.execute_python_addin(_ctx_with_doc(doc), "1+1", doc=doc)
    assert out == 1.0
    assert captured.get("bindings") == {"scoped_dir": "/folder"}


def test_execute_python_addin_reuses_session_key_sid(monkeypatch: pytest.MonkeyPatch) -> None:
    _function.clear_python_addin_cache()
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

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr(_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(_function, "workbook_session_id", mock_session_id)
    monkeypatch.setattr("plugin.scripting.session_manager.workbook_session_id", mock_session_id)

    doc = CalcDocStub()
    out = _function.execute_python_addin(_ctx_with_doc(doc), "1+1", doc=doc)
    assert out == 1.0
    assert session_calls == 1
    assert captured.get("session_id") == "calc:file:///once.ods"

    _function.clear_python_addin_cache()
    captured.clear()
    session_calls = 0
    current_sid = None
    out2 = _function.execute_python_addin(_ctx_with_doc(doc), "2+2", doc=doc)
    assert out2 == 1.0
    assert session_calls == 1
    assert captured.get("session_id") is None


def test_execute_python_addin_off_main_passes_doc_through_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off-main the caller doc yields no session id, init kwargs, or folder (all UNO)."""
    _function.clear_python_addin_cache()
    captured: dict[str, Any] = {}

    def fake_run(*_a: object, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "ok", "result": 1.0}

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not derive a session off-main")

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    monkeypatch.setattr(_function, "run_code_in_user_venv", fake_run)
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(_function, "workbook_session_id", boom)
    out = _function.execute_python_addin(_ctx_with_doc(None), "result=1", doc=CalcDocStub())
    assert out == 1.0
    assert captured.get("bindings") == {"scoped_dir": None}
    assert captured.get("session_id") is None
    assert "init_script" not in captured


def test_selection_does_not_suppress_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """CalcDocStub selection is a multi-cell range, the located formula cell is a single cell, finalize_python_return still spills. Fallback does not spill."""

    doc = CalcDocStub(url="file:///sel_spill.ods", selection="A1:D10")
    sheet = doc.getSheets().getByName("Sheet1")
    formula_cell = sheet.getCellByPosition(0, 0)
    formula_cell.setFormula('=PY("code_single")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()

    _install_immediate_spill_timer(monkeypatch)

    with patch("plugin.calc.python.function.locate_formula_cell_in_doc", return_value=(sheet, formula_cell, (0, 0))):
        val = _function.finalize_python_return(_cell(ctx, "code_single", doc), [10, 20, 30])

    assert val == 10
    assert sheet.getCellByPosition(0, 1).getValue() == 20
    key = ("file:///sel_spill.ods", "Sheet1", 0, 0)
    assert key in SPILL_REGISTRY
    assert SPILL_REGISTRY[key].spills == [(1, 0), (2, 0)]

    # Second case: locate returns None -> returns scalar without reading selection or queuing spill
    doc.getCurrentController = lambda: pytest.fail("selection must not be read")
    with patch("plugin.calc.python.function.locate_formula_cell_in_doc", return_value=None):
        val_fallback = _function.finalize_python_return(_cell(ctx, "code_fallback", doc), [10, 20, 30])
    assert val_fallback == 10


def test_indexed_multi_data_ignores_my_data() -> None:
    """_code_uses_indexed_multi_data('my_data[0]') is false; 'data[0]' and 'ranges[1]' and 'note = \"data[0]\"' are true."""
    assert not _function._code_uses_indexed_multi_data("my_data[0]")
    assert not _function._code_uses_indexed_multi_data("_data[0]")
    assert not _function._code_uses_indexed_multi_data("obj.data[0]")
    assert not _function._code_uses_indexed_multi_data("self.ranges[0]")
    assert _function._code_uses_indexed_multi_data("data[0]")
    assert _function._code_uses_indexed_multi_data("ranges[1]")
    assert _function._code_uses_indexed_multi_data('note = "data[0]"')
    assert _function._code_uses_indexed_multi_data("(data[0])")


def test_matrix_sessions_expire() -> None:
    """Plant an entry with timestamp older than _PY_PASS_GAP_SEC, call scalar_for_list_result for a different key, assert old key is gone."""
    import time

    from plugin.calc.python.workbook_state import WorkbookState

    _function.clear_python_addin_cache()
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    st = WorkbookState.for_doc(doc)
    assert st is not None
    old_time = time.monotonic() - (_function._PY_PASS_GAP_SEC + 5.0)

    stale_key = ("tid_old", ("file:///d.ods", "S", "sid", "c", "1,1"), "token_old")
    with _function._MATRIX_SCALAR_SESSIONS_LOCK:
        st.matrix[stale_key] = _function.WorkerResultSession([1], [1], timestamp=old_time)

    doc.getSheets().getByName("Sheet1").getCellByPosition(0, 0).setFormula('=PY("c_new")')
    _function.scalar_for_list_result(_cell(ctx, "c_new", doc), [10, 20])

    with _function._MATRIX_SCALAR_SESSIONS_LOCK:
        assert stale_key not in st.matrix


def test_execute_python_addin_worker_errors(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _function.clear_python_addin_cache()
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(_function, "workbook_session_id", lambda *_a, **_k: None)

    # 1. ValueError-shaped worker dict -> the 519 double, not a string
    rec_diag = MagicMock()
    monkeypatch.setattr(_function, "_record_py_diagnostic", rec_diag)
    err_msg = "Code execution failed at line 'bad' due to: ValueError: invalid literal"
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "message": err_msg})
    out = _function.execute_python_addin(ctx, "bad_code", doc=doc)
    assert isinstance(out, float)
    raw = struct.unpack("Q", struct.pack("d", out))[0]
    assert raw & 0xFFFF == 519
    rec_diag.assert_called_with("bad_code", {"status": "error", "message": err_msg}, status="error", message=err_msg, workbook_key="calc:test://calc")

    # 2. ZeroDivisionError-shaped dict -> 532
    div_msg = "Code execution failed at line '1/0' due to: ZeroDivisionError: division by zero"
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "message": div_msg})
    out_div = _function.execute_python_addin(ctx, "1/0", doc=doc)
    assert isinstance(out_div, float)
    raw_div = struct.unpack("Q", struct.pack("d", out_div))[0]
    assert raw_div & 0xFFFF == 532

    # 3. Existing timeout and venv-not-found tests still expect a string containing Settings
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "code": "VENV_NOT_FOUND", "message": "No python executable found under configured venv: '/missing'"})
    out_venv = _function.execute_python_addin(ctx, "venv_code", doc=doc)
    assert isinstance(out_venv, str) and "Settings" in out_venv

    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "code": "VENV_TIMEOUT", "message": "Python worker failed: timed out after 10 seconds"})
    out_to = _function.execute_python_addin(ctx, "to_code", doc=doc)
    assert isinstance(out_to, str) and "timed out" in out_to.lower()

    # 4. code="USER_STOPPED", message Stopped by user. -> a string, no ERROR log
    caplog.clear()
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user."})
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
        out_stop = _function.execute_python_addin(ctx, "stop_code", doc=doc)
    assert isinstance(out_stop, str)
    assert "Stopped" in out_stop or "USER_STOPPED" in out_stop
    assert caplog.text == ""

    # 5. Monkeypatch run_code_in_user_venv to raise RuntimeError("host blew up") -> still a string from format_addin_error_for_display
    def _raise_runtime(*_a, **_k):
        raise RuntimeError("host blew up")

    monkeypatch.setattr(_function, "run_code_in_user_venv", _raise_runtime)
    out_exc = _function.execute_python_addin(ctx, "exc_code", doc=doc)
    assert isinstance(out_exc, str)
    assert "host blew up" in out_exc

    # 5b. Missing-name worker error formatted as due to: InterpreterError: name 'bad' is not defined -> 519 double
    missing_msg = "Code execution failed at line 'bad' due to: InterpreterError: name 'bad' is not defined"
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "message": missing_msg})
    out_missing = _function.execute_python_addin(ctx, "bad", doc=doc)
    assert isinstance(out_missing, float)
    raw_missing = struct.unpack("Q", struct.pack("d", out_missing))[0]
    assert raw_missing & 0xFFFF == 519

    # 6. Successful result="#N/A" -> the 32767 double
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "ok", "result": "#N/A"})
    out_na = _function.execute_python_addin(ctx, "na_code", doc=doc)
    assert isinstance(out_na, float)
    raw_na = struct.unpack("Q", struct.pack("d", out_na))[0]
    assert raw_na & 0xFFFF == 32767

    # 7. Successful result=["#NUM!", 2] is a column spill; worker status: error does not write SPILL_REGISTRY
    reset_calc_workbook_states()
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "message": err_msg})
    _function.execute_python_addin(ctx, "spill_fail_code", doc=doc)
    assert len(SPILL_REGISTRY) == 0

    monkeypatch.setattr("plugin.framework.queue_executor.main.post", lambda fn, *a, **k: fn(*a, **k))
    doc_spill = CalcDocStub(url="file:///fake_col_addin.ods", selection="A1")
    sheet_spill = doc_spill.getSheets().getByName("Sheet1")
    sheet_spill.getCellByPosition(0, 0).setFormula('=PYTHON("num_spill_code")')
    ctx_spill = _ctx_with_doc(doc_spill)
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "ok", "result": ["#NUM!", 2]})
    out_spill = _function.execute_python_addin(ctx_spill, "num_spill_code", doc=doc_spill)
    assert isinstance(out_spill, float)
    assert struct.unpack("Q", struct.pack("d", out_spill))[0] & 0xFFFF == 503
    from plugin.calc.python.workbook_state import WorkbookState

    st_spill = WorkbookState.for_doc(doc_spill)
    assert st_spill is not None
    st_spill.debounce.flush_for_tests("spill:Sheet1:0:0")
    key_spill = ("file:///fake_col_addin.ods", "Sheet1", 0, 0)
    assert key_spill in SPILL_REGISTRY


def test_execute_python_addin_helper_error_dict(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _function.clear_python_addin_cache()
    doc = CalcDocStub()
    ctx = _ctx_with_doc(doc)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(_function, "workbook_session_id", lambda *_a, **_k: None)
    rec_diag = MagicMock()
    monkeypatch.setattr(_function, "_record_py_diagnostic", rec_diag)

    err = {"status": "error", "code": "UNKNOWN_COLUMN", "message": "Unknown columns: Region", "helper": "quick_stats"}
    worker = {"status": "ok", "result": err}
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: worker)
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
        out = _function.execute_python_addin(ctx, "helper_err", doc=doc)
    assert isinstance(out, float)
    assert struct.unpack("Q", struct.pack("d", out))[0] & 0xFFFF == 519
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1
    rec_diag.assert_called_once()
    assert rec_diag.call_args.kwargs.get("status") == "error"
    assert "UNKNOWN_COLUMN" in rec_diag.call_args.kwargs["message"]
    assert rec_diag.call_args.kwargs.get("workbook_key") == "calc:test://calc"

    # Message text that would stay a Settings string on the worker-status path is still #VALUE! here.
    caplog.clear()
    rec_diag.reset_mock()
    timed = {"status": "error", "code": "INVALID_PARAM", "message": "connection timed out", "helper": "describe_data"}
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "ok", "result": timed})
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
        out_timed = _function.execute_python_addin(ctx, "helper_timeout_word", doc=doc)
    assert isinstance(out_timed, float)
    assert struct.unpack("Q", struct.pack("d", out_timed))[0] & 0xFFFF == 519
    assert any(r.levelname == "ERROR" for r in caplog.records)

    # Ordinary dict stays text. Diagnostic is the ok record, not an error record.
    caplog.clear()
    rec_diag.reset_mock()
    plain = {"hello": "world"}
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "ok", "result": plain})
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
        out_plain = _function.execute_python_addin(ctx, "plain_dict", doc=doc)
    assert isinstance(out_plain, str)
    assert "hello" in out_plain
    assert caplog.text == ""
    rec_diag.assert_called_once_with("plain_dict", {"status": "ok", "result": plain}, status="ok", workbook_key="calc:test://calc")

    # Worker USER_STOPPED is still the text path (status on the envelope, not a helper result).
    caplog.clear()
    rec_diag.reset_mock()
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user."})
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
        out_stop = _function.execute_python_addin(ctx, "stop_still_text", doc=doc)
    assert isinstance(out_stop, str)
    assert "Stopped" in out_stop
    assert caplog.text == ""


def test_flat_result_equal_and_scalar_matrix_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two payload NaNs with the same bits compare equal
    nan519_a = _addin_values.calc_formula_error_double(519)
    nan519_b = _addin_values.calc_formula_error_double(519)
    nan532 = _addin_values.calc_formula_error_double(532)
    assert _addin_values.flat_result_equal((nan519_a,), (nan519_b,)) is True
    assert _addin_values.flat_result_equal((nan519_a,), (nan532,)) is False

    # float("nan") compares equal to float("nan")
    assert _addin_values.flat_result_equal((float("nan"),), (float("nan"),)) is True
    # 1.0 equals 1.0
    assert _addin_values.flat_result_equal((1.0,), (1.0,)) is True
    # #VALUE! mapped values of 519 equal each other
    val519_a = _addin_values.py_addin_cell_value("#VALUE!")
    val519_b = _addin_values.py_addin_cell_value("#VALUE!")
    assert _addin_values.flat_result_equal((val519_a,), (val519_b,)) is True
    val503 = _addin_values.py_addin_cell_value("#NUM!")
    assert _addin_values.flat_result_equal((val519_a,), (val503,)) is False

    # Monkeypatch session_key to ("u", "Sheet1", "sid", code, "0,0")
    # Call scalar_for_list_result twice on ["#VALUE!", "#DIV/0!"]
    code = "test_matrix_session_err"
    ctx = MagicMock()
    doc = CalcDocStub()
    doc.getSheets().getByName("Sheet1").getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    try:
        ret1 = _function.scalar_for_list_result(_cell(ctx, code, doc), ["#VALUE!", "#DIV/0!"])
        ret2 = _function.scalar_for_list_result(_cell(ctx, code, doc), ["#VALUE!", "#DIV/0!"])
        assert isinstance(ret1, float)
        assert struct.unpack("Q", struct.pack("d", ret1))[0] & 0xFFFF == 519
        assert isinstance(ret2, float)
        assert struct.unpack("Q", struct.pack("d", ret2))[0] & 0xFFFF == 532
    finally:
        _function.clear_python_addin_cache()


def test_spill_with_calc_error_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.doc_stubs import CalcRangeStub

    from plugin.calc.python.workbook_state import WorkbookState

    monkeypatch.setattr("plugin.framework.queue_executor.main.post", lambda fn, *a, **k: fn(*a, **k))
    reset_calc_workbook_states()

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
    ret_corner = _function.finalize_python_return(_cell(ctx, "col_err_test", doc), col_result)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.debounce.flush_for_tests("spill:Sheet1:0:0")
    assert ret_corner == 1.0
    assert sheet.getCellByPosition(0, 1).getFormula() == "=#ERR519!"
    assert sheet.getCellByPosition(0, 2).getFormula() == "=#ERR532!"
    # The #N/A slot in the recorded setDataArray data is None, and that cell is not given a formula
    assert sheet.getCellByPosition(0, 3).getFormula() == ""
    all_recorded_cells = [cell for call in recorded_set_data for row in call for cell in row]
    assert None in all_recorded_cells
    key_col = ("file:///fake_col.ods", sheet.getName(), 0, 0)
    assert key_col in SPILL_REGISTRY
    col_cells = SPILL_REGISTRY[key_col].cells
    assert len(col_cells) == 3
    assert col_cells[0].kind == "err" and col_cells[0].value == 519
    assert col_cells[1].kind == "err" and col_cells[1].value == 532
    assert col_cells[2].kind == "err" and col_cells[2].value == 32767

    # 2. 2x2 [[1, "#NUM!"], ["#REF!", "#NAME?"]]
    recorded_set_data.clear()
    reset_calc_workbook_states()
    doc2 = CalcDocStub(url="file:///fake_2x2.ods", selection="A1")
    sheet2 = doc2.getSheets().getByName("Sheet1")
    sheet2.getCellByPosition(0, 0).setFormula('=PYTHON("grid_2x2_test")')
    ctx2 = _ctx_with_doc(doc2)

    grid_result = [[1, "#NUM!"], ["#REF!", "#NAME?"]]
    ret_corner2 = _function.finalize_python_return(_cell(ctx2, "grid_2x2_test", doc2), grid_result)
    state2 = WorkbookState.for_doc(doc2)
    assert state2 is not None
    state2.debounce.flush_for_tests("spill:Sheet1:0:0")
    assert ret_corner2 == 1.0
    assert sheet2.getCellByPosition(1, 0).getFormula() == "=#ERR503!"
    assert sheet2.getCellByPosition(0, 1).getFormula() == "=#ERR524!"
    assert sheet2.getCellByPosition(1, 1).getFormula() == "=#ERR525!"
    recorded_cells_2x2 = [cell for call in recorded_set_data for row in call for cell in row]
    assert all(c == "" for c in recorded_cells_2x2)

    # 3. [[ "#NULL!" ]] (one cell): return value is the 521 double. No neighbor formula.
    reset_calc_workbook_states()
    doc3 = CalcDocStub(url="file:///fake_1x1.ods", selection="A1")
    sheet3 = doc3.getSheets().getByName("Sheet1")
    sheet3.getCellByPosition(0, 0).setFormula('=PYTHON("one_cell_test")')
    ctx3 = _ctx_with_doc(doc3)
    ret_corner3 = _function.finalize_python_return(_cell(ctx3, "one_cell_test", doc3), [["#NULL!"]])
    assert isinstance(ret_corner3, float)
    assert struct.unpack("Q", struct.pack("d", ret_corner3))[0] & 0xFFFF == 521
    assert sheet3.getCellByPosition(1, 0).getFormula() == ""
    assert sheet3.getCellByPosition(0, 1).getFormula() == ""

    # 4. Index path: finalize_python_return(_cell(..., ["a", None), "#NUM!"], index_arg=1 is the 503 double.)
    # index_arg=5 on a 2-element list is still the Error: index … string.
    val_idx = _function.finalize_python_return(_cell(ctx3, "idx_code", None), ["a", "#NUM!"], index_arg=1)
    assert isinstance(val_idx, float)
    assert struct.unpack("Q", struct.pack("d", val_idx))[0] & 0xFFFF == 503
    val_idx_err = _function.finalize_python_return(_cell(ctx3, "idx_code", None), ["a", "#NUM!"], index_arg=5)
    assert isinstance(val_idx_err, str)
    assert "Error: index 5 out of range" in val_idx_err


def test_finalize_python_return_empty_row_does_not_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///empty-row.ods", selection="A1")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PYTHON("empty_row")')
    ctx = _ctx_with_doc(doc)

    reset_calc_workbook_states()
    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    if st is not None:
        st.debounce.cancel_all()

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    val = finalize_python_return(_cell(ctx, "empty_row", doc), [[]])
    assert val == ""
    assert ("file:///empty-row.ods", "Sheet1", 0, 0) not in SPILL_REGISTRY
    assert st is not None
    assert "spill:Sheet1:0:0" not in st.debounce.pending_for_tests()

    # Repeat off the main thread
    queue_mock = MagicMock()
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    monkeypatch.setattr("plugin.calc.python.function.queue_off_main_auto_spill", queue_mock)
    val_off = finalize_python_return(_cell(ctx, "empty_row", doc), [[]])
    assert val_off == ""
    queue_mock.assert_not_called()


def test_scalar_for_list_result_uses_supplied_data_token(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub()
    doc.getSheets().getByName("Sheet1").getCellByPosition(0, 0).setFormula('=PY("code_token")')
    ctx = _ctx_with_doc(doc)
    _function.clear_python_addin_cache()

    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None
    res = _function.scalar_for_list_result(_cell(ctx, "code_token", doc), [10, 20], data_token="preset")
    assert res == 10
    with _function._MATRIX_SCALAR_SESSIONS_LOCK:
        matching_keys = [k for k in st.matrix if k[1][3] == "code_token"]
        assert len(matching_keys) == 1
        assert matching_keys[0][2] == "preset"


def test_execute_python_addin_hashes_worker_data_once(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub()
    doc.getSheets().getByName("Sheet1").getCellByPosition(1, 1).setFormula('=PY("code_once")')
    ctx = _ctx_with_doc(doc)
    _function.clear_python_addin_cache()

    monkeypatch.setattr("plugin.framework.config.get_config", lambda key, default=False: False if key == "scripting.python_auto_spill" else default)
    monkeypatch.setattr("plugin.calc.python.function.get_config", lambda key, default=False: False if key == "scripting.python_auto_spill" else default)
    monkeypatch.setattr("plugin.calc.python.function.run_code_in_user_venv", lambda *args, **kwargs: {"status": "ok", "result": [1, 2, 3]})
    call_count = 0
    real_token = _function._worker_data_token

    def counting_token(data):
        nonlocal call_count
        call_count += 1
        return real_token(data)

    monkeypatch.setattr("plugin.calc.python.function._worker_data_token", counting_token)

    _function.execute_python_addin(ctx, "code_once", data=42, doc=doc)
    assert call_count == 1


def test_execute_python_addin_string_result_returns_text(monkeypatch) -> None:
    _function.clear_python_addin_cache()
    monkeypatch.setattr(_function, "run_code_in_user_venv", lambda *_a, **_k: {"status": "ok", "result": "hi"})
    monkeypatch.setattr(_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(_function, "workbook_session_id", lambda *_a, **_k: None)
    assert _function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "'hi'", ((10.0,),)) == "hi"


def test_build_cell_context_carries_origin_and_is_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///origin_matrix.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PY("o")')
    ctx = _ctx_with_doc(doc)

    cell = _function.build_cell_context(ctx, doc, "o", on_main=True)
    assert cell.origin is not None
    assert (cell.origin.row, cell.origin.col) == (1, 1)
    assert cell.origin.sheet_name == "Sheet1"
    assert cell.is_matrix is False

    monkeypatch.setattr(_function, "cell_is_matrix", lambda _s, _c: True)
    matrix_cell = _function.build_cell_context(ctx, doc, "o", on_main=True)
    assert matrix_cell.origin is not None
    assert matrix_cell.is_matrix is True

    sheet.getCellByPosition(2, 2).setFormula('=PY("o")')
    dup_cell = _function.build_cell_context(ctx, doc, "o", on_main=True)
    assert dup_cell.origin is None
    assert dup_cell.is_matrix is False


def test_finalize_python_return_locates_once(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///locate_once.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(1, 1).setFormula('=PY("sp_once")')
    ctx = _ctx_with_doc(doc)
    reset_calc_workbook_states()
    _install_immediate_spill_timer(monkeypatch)

    calls: list[str] = []
    real_locate = _function.locate_formula_cell_in_doc

    def spy_locate(d: Any, c: str) -> Any:
        calls.append(c)
        return real_locate(d, c)

    monkeypatch.setattr(_function, "locate_formula_cell_in_doc", spy_locate)
    monkeypatch.setattr(_spill, "locate_formula_cell_in_doc", spy_locate)
    cell = _function.build_cell_context(ctx, doc, "sp_once", on_main=True)
    out = _function.finalize_python_return(cell, [10, 20, 30])

    assert out == 10.0
    assert len(calls) == 1
    assert sheet.getCellByPosition(1, 2).getValue() == 20.0
    assert ("file:///locate_once.ods", "Sheet1", 1, 1) in SPILL_REGISTRY


def test_build_cell_context_diag_key_uses_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///diag_owner.ods")
    ctx = _ctx_with_doc(doc)
    calls: list[Any] = []

    def fake_diag_key(d: Any) -> str:
        calls.append(d)
        return "calc:x"

    monkeypatch.setattr(_function, "diagnostics_workbook_key", fake_diag_key)
    on_main_cell = _function.build_cell_context(ctx, doc, "c", on_main=True)
    assert on_main_cell.diag_workbook_key == "calc:x"
    assert len(calls) == 1

    off_main_cell = _function.build_cell_context(ctx, doc, "c", on_main=False)
    assert off_main_cell.diag_workbook_key == "unknown"
    assert len(calls) == 1


def test_split_trailing_index() -> None:
    grid = ((1.0, 2.0), (3.0, 4.0))
    grid_wire = [[1.0, 2.0], [3.0, 4.0]]

    args, py_data, idx = _function._split_trailing_index("[data]*3", [grid, [[2.0]]], [grid_wire, [[2.0]]])
    assert idx == 2.0
    assert len(args) == 1
    assert py_data == grid_wire

    args_indexed, py_data_indexed, idx_indexed = _function._split_trailing_index("data[0]", [grid, [[2.0]]], [grid_wire, [[2.0]]])
    assert idx_indexed is None
    assert len(args_indexed) == 2
    assert py_data_indexed == [grid_wire, [[2.0]]]

    args_single, py_data_single, idx_single = _function._split_trailing_index("[data]*3", [grid], grid_wire)
    assert idx_single is None
    assert len(args_single) == 1
    assert py_data_single == grid_wire


def test_clear_workbook_matrix_cache_leaves_other_workbooks() -> None:
    from plugin.calc.python.workbook_state import WorkbookState

    doc_a = CalcDocStub(url="file:///wb-a.ods", props={"RuntimeUID": "uid-wb-a"})
    doc_b = CalcDocStub(url="file:///wb-b.ods", props={"RuntimeUID": "uid-wb-b"})
    doc_none = CalcDocStub(url="file:///wb-none.ods", props={"RuntimeUID": "uid-wb-none"})

    state_a = WorkbookState.for_doc(doc_a)
    state_b = WorkbookState.for_doc(doc_b)
    assert state_a is not None and state_b is not None
    state_a.matrix[("Sheet1", 0, 0, "c", "t")] = "sentinel_a"  # type: ignore[assignment]
    state_b.matrix[("Sheet1", 0, 0, "c", "t")] = "sentinel_b"  # type: ignore[assignment]

    _function.clear_workbook_matrix_cache(doc_a)
    assert state_a.matrix == {}
    assert state_b.matrix == {("Sheet1", 0, 0, "c", "t"): "sentinel_b"}

    # A doc with no state is a no-op.
    _function.clear_workbook_matrix_cache(doc_none)
    assert state_b.matrix == {("Sheet1", 0, 0, "c", "t"): "sentinel_b"}


def test_execute_python_addin_multi_range_uses_multi_envelope():
    ctx = SimpleNamespace()
    col_a = ((1.0,), (2.0,), (3.0,))
    col_b = ((4.0,), (5.0,))
    try:
        with patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": 15.0}
            res = _function.execute_python_addin(ctx, "result = float(np.sum(data[0])) + float(np.sum(data[1]))", (col_a, col_b))
            assert res == 15.0
            mock_run.assert_called_once()
            wire = mock_run.call_args.kwargs["data"]
            assert is_multi_data(wire)
            assert len(wire["items"]) == 2
    finally:
        _function.clear_python_addin_cache()


def test_execute_python_addin_single_range_unchanged():
    from plugin.scripting.calc_range import is_calc_range_payload

    ctx = SimpleNamespace()
    col = ((1.0,), (2.0,), (3.0,))
    try:
        with patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": 6.0}
            res = _function.execute_python_addin(ctx, "result = float(np.sum(data))", col)
            assert res == 6.0
            wire = mock_run.call_args.kwargs["data"]
            assert not is_multi_data(wire)
            assert is_calc_range_payload(wire)
            assert wire["shape"] == [3, 1]
            assert wire["data"] == [[1.0], [2.0], [3.0]]
            assert mock_run.call_args.kwargs.get("python_tool_domain") == ""
    finally:
        _function.clear_python_addin_cache()


def test_execute_python_addin_matrix_index_still_works():
    ctx = SimpleNamespace()
    try:
        with patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": [10, 20, 30]}
            res = _function.execute_python_addin(ctx, "code", 1.0)
            assert res == 20.0
    finally:
        _function.clear_python_addin_cache()


def test_execute_python_addin_wrapped_varargs_single_range():
    ctx = SimpleNamespace()
    col = ((1.0,), (2.0,), (3.0,))
    try:
        with patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": 6.0}
            res = _function.execute_python_addin(ctx, "result = float(np.sum(data))", (col,))
            assert res == 6.0
            wire = mock_run.call_args.kwargs["data"]
            assert not is_multi_data(wire)
    finally:
        _function.clear_python_addin_cache()


def test_execute_python_addin_two_single_cells_not_treated_as_index():
    """Spreadsheet import SUM(C7,C12) must keep both scalar precedents."""
    ctx = SimpleNamespace()
    cell_a = ((23750.0,),)
    cell_b = ((14000.0,),)
    try:
        with patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": 37750.0}
            res = _function.execute_python_addin(ctx, "ranges[0].values[0][0] + ranges[1].values[0][0]", (cell_a, cell_b))
            assert res == 37750.0
            wire = mock_run.call_args.kwargs["data"]
            assert is_multi_data(wire)
            assert len(wire["items"]) == 2
    finally:
        _function.clear_python_addin_cache()
