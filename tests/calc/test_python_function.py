# WriterAgent - =PYTHON() return coercion tests

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import plugin.calc.python_function as python_function
from plugin.calc.python_function import finalize_python_return, to_calc_compatible


def test_to_calc_compatible_none_and_nan_become_empty() -> None:
    assert to_calc_compatible(None) == ""
    assert to_calc_compatible(float("nan")) == ""


def test_to_calc_compatible_finite_float_unchanged() -> None:
    assert to_calc_compatible(3.5) == 3.5


def test_to_calc_compatible_nan_in_nested_matrix() -> None:
    matrix = ((1.0, float("nan")), (3.0, 4.0))
    assert to_calc_compatible(matrix) == ((1.0, ""), (3.0, 4.0))


def test_finalize_python_return_scalar_nan_becomes_empty() -> None:
    class _Ctx:
        pass

    assert finalize_python_return(_Ctx(), "c", float("nan")) == ""


def test_finalize_python_return_list_nan_becomes_empty() -> None:
    class _Ctx:
        pass

    ctx = _Ctx()
    assert finalize_python_return(ctx, "c", [1.0, float("nan"), 3.0]) == 1.0
    assert finalize_python_return(ctx, "c", [1.0, float("nan"), 3.0]) == ""
    assert finalize_python_return(ctx, "c", [1.0, float("nan"), 3.0]) == 3.0


@pytest.mark.parametrize("nan_val", [math.nan, float("nan")])
def test_to_calc_compatible_various_nan_literals(nan_val: float) -> None:
    assert to_calc_compatible(nan_val) == ""


def test_insert_image_result_uses_merged_safe_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TmpFile:
        name = "/tmp/fake.png"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def write(self, _data):
            return None

    monkeypatch.setattr(python_function.tempfile, "NamedTemporaryFile", lambda **kwargs: _TmpFile())

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
