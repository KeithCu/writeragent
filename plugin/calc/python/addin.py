# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO Calc add-in for =PY() / =PYTHON() (no LLM imports at module load)."""

from __future__ import annotations

import os
import sys

# --- Minimal stdlib-only bootstrap (MUST be before any "from plugin..." import) ---
# unopkg writeRegistryInfo loads this file before the extension root is on sys.path.
_this = os.path.abspath(__file__)
for __ in range(4):  # plugin/calc/python/addin.py → plugin/calc/python/ → plugin/calc/ → plugin/ → extension root
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

import logging
from typing import Any

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(
    __file__,
    levels_up=4,
    also_add_plugin_dir=True,
    also_add_lib=True,
    also_add_vendor=True,
)

import uno  # noqa: E402
import unohelper  # noqa: E402

from plugin.calc.addin_common import CalcFunctionSpec, SingleFunctionAddInBase  # noqa: E402
from plugin.calc.python.function import execute_python_addin  # noqa: E402

log = logging.getLogger(__name__)

_PYTHON_ARGS = (
    "The Python code to execute. Assign output to 'result'.",
    "Optional one or more ranges injected as data (single range: flat/2D; "
    "multiple ranges: data[0], data[1], …), or a single-cell index for "
    "matrix formulas (e.g. ROW(A1)-ROW($A$1)).",
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
_PYTHON_FUNCTION_SPECS = (_PY_SPEC, _PYTHON_SPEC)

try:
    from org.extension.writeragent.PythonFunction import (  # type: ignore
        XPythonFunction as _XPythonFunctionBase,
    )
except ImportError:

    class _XPythonFunctionStub(unohelper.Base):
        pass

    _XPythonFunctionBase = _XPythonFunctionStub


class PythonFunction(SingleFunctionAddInBase, _XPythonFunctionBase):  # pyright: ignore[reportGeneralTypeIssues]  # pyrefly: ignore[invalid-inheritance]
    """Calc add-in: org.extension.writeragent.PythonFunction (=PY / =PYTHON)."""

    def __init__(self, ctx: Any) -> None:
        log.debug("=== PythonFunction.__init__ ===")
        super().__init__(ctx, _PYTHON_FUNCTION_SPECS)
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
        except Exception as e:
            log.debug("Failed to map localized booleans via UNO: %s", e)

        return true_strs, false_strs

    def python(self, code: str, data: Any = None) -> Any:
        return execute_python_addin(self.ctx, code, data, self._true_strings, self._false_strings)

    def py(self, code: str, data: Any = None) -> Any:
        return self.python(code, data)

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
