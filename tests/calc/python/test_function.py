# WriterAgent - =PYTHON() return coercion tests

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import plugin.calc.python.function as python_function
from plugin.calc.python.function import finalize_python_return, to_calc_compatible
from plugin.tests.testing_utils import CalcDocStub


def _ctx_with_doc(doc: CalcDocStub):
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    return SimpleNamespace(ServiceManager=smgr)


def test_to_calc_compatible_none_becomes_empty_nan_becomes_error() -> None:
    """None (from text/mixed or explicit) becomes empty cell; NaN is returned raw (Calc shows cascading error)."""
    import math
    assert to_calc_compatible(None) == ""
    assert math.isnan(to_calc_compatible(float("nan")))


def test_to_calc_compatible_finite_float_unchanged() -> None:
    assert to_calc_compatible(3.5) == 3.5


def test_to_calc_compatible_bool_passthrough() -> None:
    assert to_calc_compatible(True) is True
    assert to_calc_compatible(False) is False


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

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)

    doc = CalcDocStub(url="file:///fake_recursion.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet._name = "SheetTest"

    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda ctx: doc)
    monkeypatch.setattr("plugin.scripting.document_scripts.get_calc_document_from_ctx", lambda ctx: doc)
    monkeypatch.setattr("plugin.scripting.document_scripts.build_python_eval_init_kwargs", lambda doc: {"dummy": True})

    ctx = MagicMock()
    key = python_function.session_key(ctx, "print('hello')")
    assert key == ("file:///fake_recursion.ods", "SheetTest", "print('hello')")

    kwargs = python_function.get_python_init_kwargs(ctx)
    assert kwargs == {"dummy": True}


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
    python_function.MATRIX_SCALAR_SESSIONS.sessions = {}
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {
            "status": "error",
            "message": "No python executable found under configured venv: '/missing'",
        },
    )
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda _ctx: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda _ctx: None)
    out = python_function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "1+1")
    assert "Settings" in out
    assert "Test" in out
    assert "venv" in out.lower()


def test_execute_python_addin_maps_timeout_error(monkeypatch) -> None:
    python_function.MATRIX_SCALAR_SESSIONS.sessions = {}
    monkeypatch.setattr(
        python_function,
        "run_code_in_user_venv",
        lambda *_a, **_k: {
            "status": "error",
            "message": "Python worker failed: timed out after 10 seconds",
        },
    )
    monkeypatch.setattr(python_function, "_record_py_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(python_function, "get_python_init_kwargs", lambda _ctx: {})
    monkeypatch.setattr(python_function, "workbook_session_id", lambda _ctx: None)
    out = python_function.execute_python_addin(_ctx_with_doc(CalcDocStub()), "1+1")
    assert out.startswith("Error:")
    assert "timed out" in out.lower()
    assert "Settings" in out

