# WriterAgent - Calc PY/PYTHON add-in metadata tests
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for =PY() / =PYTHON() add-in name registration (no LibreOffice)."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from plugin.calc.addin_common import (
    ADDIN_SERVICE,
    CalcAddInBase,
    CalcFunctionSpec,
)

_PY_SPEC = CalcFunctionSpec(
    display_name="PY",
    programmatic_name="py",
    description="Executes Python code in the configured venv and returns the result.",
    arg_names=("code", "data"),
    arg_descriptions=("code desc", "data desc"),
    optional_from=1,
)
_PYTHON_SPEC = CalcFunctionSpec(
    display_name="PYTHON",
    programmatic_name="python",
    description="Executes Python code in the configured venv and returns the result.",
    arg_names=("code", "data"),
    arg_descriptions=("code desc", "data desc"),
    optional_from=1,
)


class _DualPythonAddIn(CalcAddInBase):
    pass


class _SinglePythonAddIn(CalcAddInBase):
    pass


def test_python_addin_metadata_both_names():
    addin = _DualPythonAddIn(MagicMock(), (_PY_SPEC, _PYTHON_SPEC))
    assert addin.getProgrammaticFunctionName("PY") == "py"
    assert addin.getProgrammaticFunctionName("PYTHON") == "python"
    assert addin.getDisplayFunctionName("py") == "PY"
    assert addin.getDisplayFunctionName("python") == "PYTHON"
    assert addin.getArgumentDescription("python", 0) == "code desc"
    assert addin.getArgumentDescription("python", 1) == "data desc"
    assert addin.getFunctionDescription("python") == "Executes Python code in the configured venv and returns the result."
    assert addin.getProgrammaticCategoryName("python") == "Add-In"
    assert addin.getDisplayCategoryName("python") == "Add-In"
    assert addin.supportsService(ADDIN_SERVICE) is True
    assert addin.supportsService("com.sun.star.sheet.OtherService") is False
    assert addin.getSupportedServiceNames() == (ADDIN_SERVICE,)


def test_single_spec_constructor():
    addin = _SinglePythonAddIn(MagicMock(), _PY_SPEC)
    assert addin.getProgrammaticFunctionName("PY") == "py"
    assert addin.getDisplayFunctionName("py") == "PY"


def test_accessors_unknown_name():
    addin = _DualPythonAddIn(MagicMock(), (_PY_SPEC, _PYTHON_SPEC))
    assert addin.getProgrammaticFunctionName("UNKNOWN") == ""
    assert addin.getDisplayFunctionName("unknown") == ""
    assert addin.getFunctionDescription("unknown") == ""
    assert addin.getArgumentDescription("unknown", 0) == ""
    assert addin.getProgrammaticCategoryName("unknown") == ""
    assert addin.getDisplayCategoryName("unknown") == ""


@pytest.mark.parametrize("index,expected_desc", [
    (-1, ""),
    (0, "code desc"),
    (1, "data desc"),
    (2, ""),
    (10, ""),
])
def test_argument_bounds(index: int, expected_desc: str):
    addin = _DualPythonAddIn(MagicMock(), (_PY_SPEC, _PYTHON_SPEC))
    assert addin.getArgumentDescription("python", index) == expected_desc


def test_duplicate_names_raise():
    dup_spec = CalcFunctionSpec(
        display_name="PY",
        programmatic_name="py_dup",
        description="desc",
        arg_names=(),
        arg_descriptions=(),
        optional_from=0,
    )
    with pytest.raises(ValueError, match="Duplicate display_name or programmatic_name"):
        _DualPythonAddIn(MagicMock(), (_PY_SPEC, dup_spec))

    dup_prog_spec = CalcFunctionSpec(
        display_name="PY2",
        programmatic_name="py",
        description="desc",
        arg_names=(),
        arg_descriptions=(),
        optional_from=0,
    )
    with pytest.raises(ValueError, match="Duplicate display_name or programmatic_name"):
        _DualPythonAddIn(MagicMock(), (_PY_SPEC, dup_prog_spec))


@pytest.mark.parametrize("args", [
    {"display_name": "", "programmatic_name": "p", "description": "d", "arg_names": (), "arg_descriptions": (), "optional_from": 0},
    {"display_name": "D", "programmatic_name": "", "description": "d", "arg_names": (), "arg_descriptions": (), "optional_from": 0},
    {"display_name": "D", "programmatic_name": "p", "description": "d", "arg_names": ("a",), "arg_descriptions": (), "optional_from": 0},
    {"display_name": "D", "programmatic_name": "p", "description": "d", "arg_names": ("a",), "arg_descriptions": ("desc",), "optional_from": -1},
    {"display_name": "D", "programmatic_name": "p", "description": "d", "arg_names": ("a",), "arg_descriptions": ("desc",), "optional_from": 2},
])
def test_calc_function_spec_validation(args: dict):
    with pytest.raises(ValueError):
        CalcFunctionSpec(**args)
