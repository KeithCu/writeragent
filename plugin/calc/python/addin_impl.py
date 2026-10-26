# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared =PY() / =PYTHON() Calc add-in class (WriterAgent and LibrePy UNO entry files).

UNO still loads ``addin.py`` or ``addin_librepy.py`` from each OXT manifest. Those
files only bootstrap ``sys.path`` and register this class. Implementation name stays
``org.extension.writeragent.PythonFunction`` so saved formulas stay portable.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import unohelper

from plugin.calc.addin_common import CalcAddInBase, CalcFunctionSpec
from plugin.calc.python.function import execute_python_addin

log = logging.getLogger(__name__)

IMPL_NAME = "org.extension.writeragent.PythonFunction"

_PYTHON_ARGS = (
    "The Python code to execute. Assign output to 'result'.",
    (
        "Optional one or more ranges injected as data (one range: data is that CalcRange; "
        "several: data is the ranges list — use data[i] or ranges[i]), or a single-cell "
        "index for matrix formulas (e.g. ROW(A1)-ROW($A$1))."
    ),
)
_PYTHON_SPEC = CalcFunctionSpec(
    display_name="PYTHON",
    programmatic_name="python",
    description="Executes Python code in the configured venv and returns the result.",
    arg_names=("code", "data"),
    arg_descriptions=_PYTHON_ARGS,
    optional_from=1,
)
_PY_SPEC = dataclasses.replace(
    _PYTHON_SPEC,
    display_name="PY",
    programmatic_name="py",
)
PYTHON_FUNCTION_SPECS = (_PY_SPEC, _PYTHON_SPEC)
# Returned when LibreOffice passes no calling document. Never guess one.
MISSING_CALLER_ERROR = "Error: no calling document"

try:
    from org.extension.writeragent.PythonFunction import (  # type: ignore
        XPythonFunction as _XPythonFunctionBase,
    )
except ImportError:

    class _XPythonFunctionStub(unohelper.Base):
        pass

    _XPythonFunctionBase = _XPythonFunctionStub


class PythonFunction(CalcAddInBase, _XPythonFunctionBase):  # pyright: ignore[reportGeneralTypeIssues, reportUntypedBaseClass]  # pyrefly: ignore[invalid-inheritance]
    """Calc add-in: org.extension.writeragent.PythonFunction (=PY / =PYTHON)."""

    # Logical string coercion targets. Static baseline sets are used instead of
    # XFormulaOpCodeMapper discovery: XFormulaOpCodeMapper in LibreOffice does
    # not expose getAvailableSymbolTokens (only getMappings/getAvailableMappings)
    # and FormulaToken lacks a .Token attribute, so dynamic mapping was dead code
    # that always failed at runtime and fell back to these English defaults.
    _true_strings: set[str] = {"=TRUE()", "TRUE", "True"}
    _false_strings: set[str] = {"=FALSE()", "FALSE", "False"}

    def __init__(self, ctx: Any) -> None:
        log.debug("=== PythonFunction.__init__ ===")
        super().__init__(ctx, PYTHON_FUNCTION_SPECS)

    def python(self, caller: Any, code: str, data: Any = None) -> Any:
        # *caller* is LibreOffice's hidden SC_ADDINARG_CALLER argument (the
        # XPropertySet first parameter in XPythonFunction.idl): the document
        # whose cell is being calculated. It is the only document source.
        # Do not call UNO on it here; this can run off the main thread.
        if caller is None:
            return MISSING_CALLER_ERROR
        return execute_python_addin(self.ctx, code, data, self._true_strings, self._false_strings, doc=caller)

    def py(self, caller: Any, code: str, data: Any = None) -> Any:
        return self.python(caller, code, data)

    def getImplementationName(self) -> str:
        return IMPL_NAME
