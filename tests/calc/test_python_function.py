# WriterAgent - =PYTHON() return coercion tests

from __future__ import annotations

import math

import pytest

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
