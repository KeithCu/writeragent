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

import logging
from typing import Any

import uno
import unohelper

from plugin.calc.addin_common import CalcFunctionSpec, SingleFunctionAddInBase
from plugin.calc.python.function import execute_python_addin

log = logging.getLogger(__name__)

IMPL_NAME = "org.extension.writeragent.PythonFunction"

_PYTHON_ARGS = (
    "The Python code to execute. Assign output to 'result'.",
    "Optional one or more ranges injected as data (one range: data is that "
    "CalcRange; several: data is the ranges list — use data[i] or ranges[i]), "
    "or a single-cell index for matrix formulas (e.g. ROW(A1)-ROW($A$1)).",
)
_PYTHON_SPEC = CalcFunctionSpec(
    display_name="PYTHON",
    programmatic_name="python",
    description="Executes Python code in the configured venv and returns the result.",
    arg_names=("code", "data"),
    arg_descriptions=_PYTHON_ARGS,
    optional_from=1,
)
_PY_SPEC = CalcFunctionSpec(
    display_name="PY",
    programmatic_name="py",
    description="Executes Python code in the configured venv and returns the result.",
    arg_names=("code", "data"),
    arg_descriptions=_PYTHON_ARGS,
    optional_from=1,
)
PYTHON_FUNCTION_SPECS = (_PY_SPEC, _PYTHON_SPEC)

try:
    from org.extension.writeragent.PythonFunction import (  # type: ignore
        XPythonFunction as _XPythonFunctionBase,
    )
except ImportError:

    class _XPythonFunctionStub(unohelper.Base):
        pass

    _XPythonFunctionBase = _XPythonFunctionStub


class PythonFunction(SingleFunctionAddInBase, _XPythonFunctionBase):  # pyright: ignore[reportGeneralTypeIssues, reportUntypedBaseClass]  # pyrefly: ignore[invalid-inheritance]
    """Calc add-in: org.extension.writeragent.PythonFunction (=PY / =PYTHON)."""

    doc: Any
    _true_strings: set[str]
    _false_strings: set[str]

    def __init__(self, ctx: Any, doc: Any | None = None) -> None:
        log.debug("=== PythonFunction.__init__ ===")
        super().__init__(ctx, PYTHON_FUNCTION_SPECS)
        self.doc = doc
        self._true_strings, self._false_strings = self._get_localized_booleans()

    def _get_localized_booleans(self) -> tuple[set[str], set[str]]:
        """Discover localized boolean function names (e.g. WAHR, VRAI) via OpCodeMapper.

        Returns two sets of uppercase strings including English and native variants.
        """
        # Always include English and Python defaults as a safety baseline
        true_strs = {"=TRUE()", "TRUE", "True"}
        false_strs = {"=FALSE()", "FALSE", "False"}
        try:
            smgr = self.ctx.getServiceManager()
            mapper = smgr.createInstanceWithContext("com.sun.star.sheet.FormulaOpCodeMapper", self.ctx)
            if mapper:
                english = uno.getConstantByName("com.sun.star.sheet.FormulaLanguage.ENGLISH")
                native = uno.getConstantByName("com.sun.star.sheet.FormulaLanguage.NATIVE")

                # Map English labels to internal OpCodes
                mappings = mapper.getMappings(["TRUE", "FALSE"], english)
                opcodes = [m.Token.OpCode for m in mappings]

                # Map OpCodes to the user's NATIVE (localized) UI symbols
                localized = mapper.getAvailableSymbolTokens(opcodes, native)
                if len(localized) >= 2:
                    for i, symbol_token in enumerate(localized[:2]):
                        name = symbol_token.Symbol.upper()
                        target_set = true_strs if i == 0 else false_strs
                        target_set.add(f"={name}()")
                        target_set.add(name)
                        target_set.add(name.capitalize())
        except Exception:
            log.debug("Failed to map localized booleans via UNO", exc_info=True)

        return true_strs, false_strs

    def python(self, code: str, data: Any = None) -> Any:
        return execute_python_addin(self.ctx, code, data, self._true_strings, self._false_strings, doc=self.doc)

    def py(self, code: str, data: Any = None) -> Any:
        return self.python(code, data)

    def getImplementationName(self) -> str:
        return IMPL_NAME
