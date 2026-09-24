# WriterAgent - Calc PY/PYTHON add-in metadata tests
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for =PY() / =PYTHON() add-in name registration (no LibreOffice)."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.calc.addin_common import CalcFunctionSpec, SingleFunctionAddInBase

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


class _DualPythonAddIn(SingleFunctionAddInBase):
    pass


def test_python_addin_metadata_both_names():
    addin = _DualPythonAddIn(MagicMock(), (_PY_SPEC, _PYTHON_SPEC))
    assert addin.getProgrammaticFunctionName("PY") == "py"
    assert addin.getProgrammaticFunctionName("PYTHON") == "python"
    assert addin.getDisplayFunctionName("py") == "PY"
    assert addin.getDisplayFunctionName("python") == "PYTHON"
    assert addin.getArgumentCount("py") == 2
    assert addin.getArgumentCount("python") == 2
    assert addin.getArgumentName("python", 0) == "code"
    assert addin.getArgumentIsOptional("python", 1) is True
    assert addin.getProgrammaticFunctionName("PROMPT") == ""
