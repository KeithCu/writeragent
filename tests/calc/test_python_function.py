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
