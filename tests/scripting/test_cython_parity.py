# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Parity testing for Cython accelerator vs Pure Python implementation.

Every parity test compares the results of the same operation with the Cython
accelerator enabled versus disabled (Pure Python).
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings, example, assume, HealthCheck

from plugin.scripting.payload_codec import fast_flatten_grid_2d
from tests.scripting.payload_codec_test_support import MIXED_WITH_ZIP, NUMERIC_4X4
from tests.scripting.serialization_ab_support import (
    VENV_CODE_ECHO,
    VENV_CODE_SUM,
    AbGridCase,
    VenvTransformCase,
    all_codec_ab_cases,
    assert_cython_vs_python_parity,
    prepare_grid,
    venv_expected_cases,
    venv_transform_cases,
    rectangular_grid,
    numeric_rectangular_grid,
    hypothesis_grid_ok,
)

pytestmark = pytest.mark.skipif(
    fast_flatten_grid_2d is None,
    reason="Cython accelerator not available"
)

def _case_id(case: AbGridCase) -> str:
    return case.id


def _cases() -> list[AbGridCase]:
    return all_codec_ab_cases()


@pytest.mark.parametrize("case", _cases(), ids=_case_id)
def test_cython_echo_parity(case: AbGridCase) -> None:
    """Cython vs Pure Python: echo parity on full fixture corpus."""
    grid = prepare_grid(case)
    assert_cython_vs_python_parity(grid, VENV_CODE_ECHO, label=case.id)


@pytest.mark.parametrize("case", venv_transform_cases(), ids=lambda c: c.id)
def test_cython_transform_parity(case: VenvTransformCase) -> None:
    """Cython vs Pure Python: worker transforms must agree."""
    assert_cython_vs_python_parity(
        case.grid,
        case.code,
        label=case.id,
    )


@given(grid=rectangular_grid())
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@example([[1.0, 2.0], [3.0, 4.0]])
@example(MIXED_WITH_ZIP)
@example([[42.0]])
@example([["02138"]])
@example(["1", "2", "3"])
@example([1, 2, 3.5])
def test_hypothesis_cython_echo_parity(grid: list[Any] | list[list[Any]]) -> None:
    """Fuzz: Cython vs Pure Python echo parity."""
    assume(hypothesis_grid_ok(grid))
    assert_cython_vs_python_parity(grid, VENV_CODE_ECHO, label="hypothesis echo")


@given(grid=numeric_rectangular_grid())
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
def test_hypothesis_cython_sum_parity(grid: list[Any] | list[list[Any]]) -> None:
    """Fuzz: Cython vs Pure Python sum parity."""
    assume(hypothesis_grid_ok(grid))
    assert_cython_vs_python_parity(grid, VENV_CODE_SUM, label="hypothesis sum")


def test_custom_object_parity():
    """Ensure that custom objects that fail float coercion behave identically in Python vs Cython."""
    class CustomObject:
        def __str__(self):
            return "custom_str"
            
    grid = [[CustomObject(), 1.0], [2.5, CustomObject()]]
    assert_cython_vs_python_parity(grid, VENV_CODE_ECHO, label="custom object parity")

