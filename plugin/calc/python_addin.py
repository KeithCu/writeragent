# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO Calc add-in for =PYTHON() only (no LLM imports at module load)."""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.addin_common import ensure_addin_paths

ensure_addin_paths()

import unohelper  # noqa: E402

from plugin.calc.addin_common import CalcFunctionSpec, SingleFunctionAddInBase  # noqa: E402
from plugin.calc.python_function import execute_python_addin  # noqa: E402

log = logging.getLogger(__name__)

_PYTHON_SPEC = CalcFunctionSpec(
    display_name="PYTHON",
    programmatic_name="python",
    description="Executes Python code in the configured venv and returns the result.",
    arg_names=("code", "data"),
    arg_descriptions=(
        "The Python code to execute. Assign output to 'result'.",
        "Optional range injected as data, or a single-cell index for matrix "
        "formulas (e.g. ROW(A1)-ROW($A$1)).",
    ),
    optional_from=1,
)

try:
    from org.extension.writeragent.PythonFunction import (  # type: ignore
        XPythonFunction as _XPythonFunctionBase,
    )
except ImportError:

    class _XPythonFunctionStub(unohelper.Base):
        pass

    _XPythonFunctionBase = _XPythonFunctionStub


class PythonFunction(SingleFunctionAddInBase, _XPythonFunctionBase):  # pyright: ignore[reportGeneralTypeIssues]  # pyrefly: ignore[invalid-inheritance]
    """Calc add-in: org.extension.writeragent.PythonFunction (=PYTHON)."""

    def __init__(self, ctx: Any) -> None:
        log.debug("=== PythonFunction.__init__ ===")
        super().__init__(ctx, _PYTHON_SPEC)

    def python(self, code: str, data: Any = None) -> Any:
        return execute_python_addin(self.ctx, code, data)

    def getImplementationName(self) -> str:
        return "org.extension.writeragent.PythonFunction"


# Back-compat alias from the split refactor.
PythonAddIn = PythonFunction

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    PythonFunction,
    "org.extension.writeragent.PythonFunction",
    ("com.sun.star.sheet.AddIn",),
)
